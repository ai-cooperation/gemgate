"""NotebookLM Podcast provider — Firefox + Cookie injection.

v3: Migrated from Chrome CDP to Firefox.
Phase 1 (execute): Create notebook, add source, start Audio Overview → return immediately
Phase 2 (check_and_download): Periodically check if audio ready, download if so

Cookie-based auth replaces auto-login. Download uses Playwright native download
instead of Chrome download dir workaround.
"""
import asyncio
import logging
import time
from pathlib import Path

from providers.base import BaseProvider, JobResult
from config import OUTPUT_DIRS

logger = logging.getLogger("ai-hub.notebooklm")

NLM_URL = "https://notebooklm.google.com/"
SCREENSHOT_DIR = Path("/opt/gemgate/state/screenshots")


class NotebookLMProvider(BaseProvider):
    name = "notebooklm"
    category = "podcast"
    chrome_profile = "firefox-notebooklm"  # Queue key for job serialization
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py

    async def _dismiss_overlays(self, page):
        """Dismiss ONLY known overlay dialogs (feature tours, cookie consent).

        CRITICAL: Do NOT dismiss generic "Close" or "OK" buttons — they may be
        part of legitimate UI (source dialog close, notebook confirmation).
        """
        for selector in [
            'button:has-text("Got it")',
            'button:has-text("Dismiss")',
            'button:has-text("Skip")',
            'button:has-text("知道了")',
        ]:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(1)
                    logger.info(f"Dismissed overlay: {selector}")
                    break
            except Exception:
                continue

    async def _force_click(self, page, element, label="element"):
        """Click with force=True, fall back to JS click if intercepted."""
        try:
            await element.click(force=True, timeout=5000)
            logger.info(f"Clicked: {label}")
            return True
        except Exception as e1:
            logger.warning(f"Force click failed on {label}: {e1}")
            try:
                await page.evaluate("(el) => el.click()", element)
                logger.info(f"JS-clicked: {label}")
                return True
            except Exception as e2:
                logger.error(f"All click methods failed on {label}: {e2}")
                return False

    async def _screenshot(self, page, name: str):
        """Save debug screenshot. Keep only last 20."""
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"nlm-{name}-{int(time.time())}.png"
            await page.screenshot(path=str(path))
            logger.info(f"Screenshot saved: {path.name}")
            screenshots = sorted(SCREENSHOT_DIR.glob("nlm-*.png"), key=lambda f: f.stat().st_mtime)
            while len(screenshots) > 20:
                screenshots.pop(0).unlink()
        except Exception as e:
            logger.warning(f"Screenshot failed: {e}")

    async def _wait_for(self, page, selector: str, timeout: int = 10, label: str = ""):
        """Wait for a selector to appear. Returns element or None."""
        for _ in range(timeout):
            el = await page.query_selector(selector)
            if el:
                return el
            await asyncio.sleep(1)
        logger.warning(f"Timeout waiting for {label or selector} ({timeout}s)")
        return None

    async def _find_generate_btn(self, page):
        """Find a visible Generate button, filtering out 'Generating...' indicators."""
        for _gsel in [
            'button:has-text("Generate")',
            'button:has-text("產生")',
            'button:has-text("生成")',
        ]:
            candidates = await page.query_selector_all(_gsel)
            for c in candidates:
                try:
                    t = await c.inner_text()
                    if "正在" in t or "Generating" in t:
                        continue
                    if await c.is_visible():
                        return c
                except Exception:
                    continue
        return None

    async def _click_audio_overview(self, page, notebook_url_before, label=""):
        """Find and click Audio Overview card. Returns True if succeeded."""
        await self._dismiss_overlays(page)
        await asyncio.sleep(1)
        for wait_i in range(15):
            for sel in [
                '[aria-label="Audio Overview"]',
                'button:has-text("Audio Overview")',
                '[aria-label="語音摘要"]',
                'button:has-text("語音摘要")',
            ]:
                el = await page.query_selector(sel)
                if el:
                    clicked = await self._force_click(page, el, f"Audio Overview{label}")
                    if clicked:
                        await asyncio.sleep(2)
                        if "notebook/" in page.url:
                            return True
                        else:
                            await page.goto(notebook_url_before)
                            await asyncio.sleep(3)
                            await self._dismiss_overlays(page)
                            st = await page.query_selector('[role="tab"]:has-text("Studio")') or \
                                 await page.query_selector('[role="tab"]:has-text("工作室")')
                            if st:
                                await self._force_click(page, st, "Studio (re-nav)")
                                await asyncio.sleep(2)
                            continue
            if wait_i % 5 == 0:
                logger.info(f"Waiting for Audio Overview{label}... ({wait_i+1}/15)")
            await asyncio.sleep(1)
        return False

    # ── Phase 1: Execute (State Machine) ──

    async def execute(self, params: dict) -> JobResult:
        """Phase 1: Create notebook + start Audio Overview."""
        sources = params.get("sources", [])
        topic = params.get("topic", "")

        if not sources:
            return JobResult(False, "At least one source required", provider=self.name)
        if not self._firefox_mgr:
            return JobResult(False, "FirefoxManager not initialized", provider=self.name)

        start = time.time()

        try:
            # Get authenticated NotebookLM page
            page = await self._firefox_mgr.get_page("firefox-notebooklm", NLM_URL)

            # First attempt
            result = await self._execute_flow(page, sources, topic)

            if not result.success:
                # Retry once from clean state
                logger.warning(f"First attempt failed: {result.message}. Retrying...")
                await page.goto(NLM_URL, wait_until="domcontentloaded", timeout=30000)
                for _rl in range(30):
                    _loading = await page.query_selector('text=正在載入') or await page.query_selector('text=Loading')
                    if not _loading:
                        break
                    await asyncio.sleep(1)
                await asyncio.sleep(3)
                await self._dismiss_overlays(page)
                result = await self._execute_flow(page, sources, topic, is_retry=True)

            if result.success:
                result.generation_time = time.time() - start
                # Keep Firefox alive for 45 min so audio can finish generating
                self._firefox_mgr.keep_alive("firefox-notebooklm", 2700)
                # DON'T close page — audio is generating in this tab

            return result

        except Exception as e:
            logger.error(f"NotebookLM error: {e}")
            return JobResult(False, str(e), provider=self.name)

    async def _execute_flow(self, page, sources: list, topic: str, is_retry: bool = False) -> JobResult:
        """Main execution flow with verification at each step."""
        attempt_label = "retry" if is_retry else "first"
        logger.info(f"Starting execute flow ({attempt_label})")

        # ── Step 1: Navigate to NotebookLM home ──
        await page.goto(NLM_URL, wait_until="domcontentloaded", timeout=30000)
        # Wait for loading spinner to disappear (正在載入筆記本...)
        for _load_wait in range(60):
            loading = await page.query_selector('text=正在載入') or                       await page.query_selector('text=Loading')
            if not loading:
                break
            if _load_wait % 5 == 0:
                logger.info(f'Waiting for page load... ({_load_wait}s)')
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # Cookie auth check — if redirected to Google sign-in, cookies expired
        if "accounts.google.com" in page.url:
            logger.error("NotebookLM: cookies expired (redirected to Google sign-in)")
            await self._screenshot(page, "cookies-expired")
            return JobResult(
                False,
                "Firefox cookies expired. Please re-login in Firefox snap browser.",
                provider=self.name,
            )

        await self._dismiss_overlays(page)

        # VERIFY: on homepage
        if "/notebook/" in page.url:
            logger.info("Still in a notebook, navigating home...")
            await page.goto(NLM_URL)
            await asyncio.sleep(3)
            await self._dismiss_overlays(page)

        logger.info(f"[Step 1] On homepage: {page.url}")

        # ── Step 2: Click "Create new" ──
        new_btn = None
        for sel in [
            'button:has-text("Create new")',
            'button:has-text("New notebook")',
            'button:has-text("Create")',
            'button:has-text("新建")',
            'button:has-text("建立")',
            'button:has-text("新增筆記本")',
            'button:has-text("建立新筆記本")',
        ]:
            new_btn = await page.query_selector(sel)
            if new_btn:
                break

        if not new_btn:
            await self._screenshot(page, "no-create-btn")
            return JobResult(False, "Cannot find Create new button", provider=self.name)

        await self._dismiss_overlays(page)
        if not await self._force_click(page, new_btn, "Create new"):
            await self._screenshot(page, "create-click-fail")
            return JobResult(False, "Cannot click Create new button", provider=self.name)

        # VERIFY: URL changed to /notebook/ (wait for /creating to resolve)
        for _ in range(15):
            await asyncio.sleep(1)
            url = page.url
            if "/notebook/" in url and "/creating" not in url:
                break
        else:
            if "/notebook/" not in page.url:
                await self._screenshot(page, "no-notebook-url")
                return JobResult(False, "Create new did not open a notebook", provider=self.name)

        logger.info(f"[Step 2] Notebook page: {page.url[:80]}")
        await self._dismiss_overlays(page)

        # ── Step 3: Pre-fetch URL sources, then add all via "Copied text" ──
        processed_sources = []
        for i, source in enumerate(sources[:3]):
            if isinstance(source, dict):
                source_type = source.get("type", "url")
                source_content = source.get("content", "")
            else:
                source_content = str(source)
                source_type = "url" if source_content.startswith("http") else "text"

            if source_type == "url":
                logger.info(f"[Step 3] Fetching URL: {source_content[:80]}...")
                try:
                    import httpx
                    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                        resp = await client.get(source_content)
                        resp.raise_for_status()
                        html = resp.text

                    import re
                    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                    text = re.sub(r'<[^>]+>', ' ', html)
                    text = re.sub(r'\s+', ' ', text).strip()
                    text = text[:200000]

                    if len(text) > 100:
                        logger.info(f"[Step 3] Fetched {len(text)} chars from URL")
                        processed_sources.append(text)
                    else:
                        logger.warning(f"[Step 3] URL returned too little text ({len(text)} chars)")
                        processed_sources.append(source_content)
                except Exception as e:
                    logger.warning(f"[Step 3] URL fetch failed: {e}, using URL as text")
                    processed_sources.append(f"Source URL: {source_content}")
            else:
                processed_sources.append(source_content)

        for i, source_content in enumerate(processed_sources):
            logger.info(f"[Step 3] Adding source {i}: {len(source_content)} chars")

            # Step 3a: Ensure source dialog is open
            source_dialog_open = False
            for attempt in range(5):
                copied_btn = None
                for _detect_sel in ['button:has-text("Copied text")', 'button:has-text("複製的文字")']:
                    copied_btn = await page.query_selector(_detect_sel)
                    if copied_btn:
                        try:
                            if await copied_btn.is_visible():
                                source_dialog_open = True
                                break
                        except Exception:
                            pass
                if source_dialog_open:
                    break

                for add_sel in [
                    'button:has-text("Upload a source")',
                    'button:has-text("Add source")',
                    'button[aria-label*="upload source"]',
                    'button[aria-label*="Add source"]',
                    'button:has-text("上傳來源")',
                    'button:has-text("新增來源")',
                    'button:has-text("來源")',
                ]:
                    add_btn = await page.query_selector(add_sel)
                    if add_btn:
                        await self._force_click(page, add_btn, f"Open source dialog (attempt {attempt+1})")
                        await asyncio.sleep(2)
                        break
                else:
                    await asyncio.sleep(2)

            if not source_dialog_open:
                await self._screenshot(page, "no-source-dialog")
                return JobResult(False, "Source dialog never opened", provider=self.name)

            logger.info(f"[Step 3a] Source dialog open (source {i+1})")

            # Step 3b: Click "Copied text"
            paste_btn = None
            for _cp_sel in [
                'button:has-text("Copied text")',
                'button:has-text("Paste text")',
                'button:has-text("複製的文字")',
                'button:has-text("貼上文字")',
            ]:
                paste_btn = await page.query_selector(_cp_sel)
                if paste_btn:
                    break
            if not paste_btn:
                await self._screenshot(page, "no-copied-text")
                return JobResult(False, "Cannot find Copied text button", provider=self.name)

            if not await self._force_click(page, paste_btn, "Copied text"):
                await self._screenshot(page, "copied-text-click-fail")
                return JobResult(False, "Cannot click Copied text", provider=self.name)
            await asyncio.sleep(2)

            # Step 3b VERIFY: textarea exists
            textarea = None
            for _ in range(10):
                textarea = None
                for _ta_sel in [
                    'textarea[aria-label="Pasted text"]',
                    'textarea[placeholder="Paste text here"]',
                    'textarea[aria-label="貼上的文字"]', 'textarea[aria-label="已貼上的文字"]',
                    'textarea[placeholder*="貼上"]',
                    'textarea',
                ]:
                    textarea = await page.query_selector(_ta_sel)
                    if textarea:
                        break
                if textarea:
                    break
                await asyncio.sleep(1)

            if not textarea:
                await self._screenshot(page, "no-textarea")
                return JobResult(False, "Paste textarea not found", provider=self.name)

            logger.info(f"[Step 3b] Textarea found")

            # Step 3c: Fill textarea with content
            try:
                await textarea.focus()
                await asyncio.sleep(0.3)
            except Exception:
                try:
                    await textarea.click(force=True, timeout=5000)
                except Exception:
                    await page.evaluate('(el) => el.focus()', textarea)
            await asyncio.sleep(0.3)
            await textarea.fill(source_content)
            await asyncio.sleep(0.5)
            await page.keyboard.press("End")
            await page.keyboard.type(" ")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(1)

            # VERIFY: textarea has content
            actual_len = await textarea.evaluate("el => el.value.length")
            if actual_len < 10:
                logger.error(f"Textarea verification failed: only {actual_len} chars")
                await self._screenshot(page, "fill-verify-fail")
                await textarea.fill("")
                await asyncio.sleep(0.3)
                await textarea.fill(source_content)
                await asyncio.sleep(0.5)
                await page.keyboard.press("End")
                await page.keyboard.type(" ")
                await asyncio.sleep(0.3)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(1)
                actual_len = await textarea.evaluate("el => el.value.length")
                if actual_len < 10:
                    return JobResult(False, f"Textarea fill failed ({actual_len} chars)", provider=self.name)

            logger.info(f"[Step 3c] Filled {actual_len} chars (verified)")

            # Step 3d: Wait for Insert button to be enabled
            insert_btn = None
            for attempt in range(20):
                insert_btn = None
                for _ins_sel in ['button:has-text("Insert")', 'button:has-text("插入")']:
                    insert_btn = await page.query_selector(_ins_sel)
                    if insert_btn:
                        break
                if insert_btn:
                    disabled = await insert_btn.get_attribute("disabled")
                    if disabled is None:
                        break
                    if attempt % 5 == 0:
                        logger.info(f"Insert disabled, waiting... ({attempt+1}/20)")
                    insert_btn = None
                await asyncio.sleep(1)

            if not insert_btn:
                await self._screenshot(page, "insert-disabled")
                return JobResult(False, "Insert button stayed disabled for 20s", provider=self.name)

            # Step 3e: Click Insert
            if not await self._force_click(page, insert_btn, "Insert"):
                await self._screenshot(page, "insert-click-fail")
                return JobResult(False, "Insert click failed", provider=self.name)
            await asyncio.sleep(5)

            # VERIFY: Insert button gone (source was added)
            await asyncio.sleep(3)
            still_insert = await page.query_selector('button:has-text("Insert")') or \
                           await page.query_selector('button:has-text("插入")')
            if still_insert:
                disabled = await still_insert.get_attribute("disabled")
                if disabled is None:
                    logger.info("[Step 3e] Insert button still visible (may be for next source)")
                else:
                    logger.info("[Step 3e] Insert disabled again = source accepted")
            else:
                logger.info("[Step 3e] Insert button gone = source accepted")

        logger.info("[Step 3] All sources processed")

        # ── Step 3f: Verify source + wait for indexing ──
        await asyncio.sleep(3)

        for wait_i in range(20):
            url = page.url
            if "/notebook/" in url and "/creating" not in url:
                logger.info(f"[Step 3f] Notebook ready: {url[:80]}")
                break
            if wait_i % 5 == 0:
                logger.info(f"[Step 3f] Waiting for notebook creation... ({url[:60]})")
            await asyncio.sleep(1)
        else:
            await self._screenshot(page, "notebook-still-creating")
            logger.warning("[Step 3f] Notebook still in creating state after 20s")

        # Source verification — works in both tab and three-column layouts
        # Try clicking Sources tab first (tab layout), then check directly (three-column)
        sources_tab = None
        for _st_sel in ['[role="tab"]:has-text("Sources")', '[role="tab"]:has-text("來源")']:
            sources_tab = await page.query_selector(_st_sel)
            if sources_tab:
                break

        if sources_tab:
            await self._force_click(page, sources_tab, "Sources tab (verify)")
            await asyncio.sleep(2)

        source_verified = False
        for poll_i in range(15):
            has_source = await page.query_selector('text=選取所有來源') or \
                         await page.query_selector('text=Select all sources')
            no_source = await page.query_selector('text=新增來源即可開始使用') or \
                        await page.query_selector('text=Add sources to get started')
            if has_source and not no_source:
                logger.info(f"[Step 3f] Source verified (after {poll_i*2}s)")
                source_verified = True
                break
            items = await page.query_selector_all('[role="listitem"]')
            if items:
                logger.info(f"[Step 3f] Source verified: {len(items)} items (after {poll_i*2}s)")
                source_verified = True
                break
            if poll_i % 5 == 0:
                logger.info(f"[Step 3f] Source waiting... ({poll_i*2}s/30s)")
            await asyncio.sleep(2)

        if not source_verified:
            await self._screenshot(page, "no-source-after-insert")
            logger.warning("[Step 3f] Source not confirmed — proceeding anyway")

        logger.info("[Step 3f] Waiting for source indexing...")
        await asyncio.sleep(10)
        await self._dismiss_overlays(page)

        # ── Step 4: Navigate to Studio (tab layout) or verify it's visible (three-column) ──
        await asyncio.sleep(2)
        await self._dismiss_overlays(page)

        # Check if Studio content is already visible (three-column layout)
        studio_visible = False
        for check_sel in [
            '[aria-label="Audio Overview"]',
            '[aria-label="語音摘要"]',
            'button:has-text("Audio Overview")',
            'button:has-text("語音摘要")',
        ]:
            el = await page.query_selector(check_sel)
            if el:
                try:
                    if await el.is_visible():
                        studio_visible = True
                        logger.info("[Step 4] Studio panel already visible (three-column layout)")
                        break
                except Exception:
                    pass

        if not studio_visible:
            # Try clicking Studio tab (older tab layout)
            studio_tab = None
            for sel in [
                '[role="tab"]:has-text("Studio")',
                'button:has-text("Studio")',
                '[role="tab"]:has-text("工作室")',
                'button:has-text("工作室")',
            ]:
                studio_tab = await page.query_selector(sel)
                if studio_tab:
                    break

            if studio_tab:
                if not await self._force_click(page, studio_tab, "Studio tab"):
                    await self._screenshot(page, "studio-click-fail")
                    return JobResult(False, "Cannot click Studio tab", provider=self.name)
                await asyncio.sleep(3)
                logger.info("[Step 4] Studio tab clicked")
            else:
                # Last resort: check for Studio panel header text
                studio_header = await page.query_selector('text=工作室') or \
                                await page.query_selector('text=Studio')
                if not studio_header:
                    await self._screenshot(page, "no-studio-tab")
                    return JobResult(False, "Studio panel not found", provider=self.name)
                logger.info("[Step 4] Studio header found")
        notebook_url_before = page.url

        # ── Step 5a: English Audio (open customize dialog) ──
        customize_clicked = False
        for _edit_sel in [
            '[aria-label="Audio Overview"] button',
            '[aria-label="語音摘要"] button',
            '.create-card:first-child button[aria-label*="edit"]',
            '.create-card:first-child button[aria-label*="編輯"]',
            '.create-card:first-child button[aria-label*="custom"]',
            '.create-card:first-child button[aria-label*="自訂"]',
        ]:
            edit_btn = await page.query_selector(_edit_sel)
            if edit_btn:
                try:
                    if await edit_btn.is_visible():
                        await self._force_click(page, edit_btn, "Audio customize icon (EN)")
                        customize_clicked = True
                        await asyncio.sleep(2)
                        break
                except Exception:
                    pass

        if not customize_clicked:
            audio_clicked = await self._click_audio_overview(page, notebook_url_before, " (EN)")
            if not audio_clicked:
                await self._screenshot(page, "no-audio-overview")
                return JobResult(False, "Audio Overview not found", provider=self.name)
            customize_clicked = True
            await asyncio.sleep(2)

        # Fill customization textarea
        en_prompt = topic if topic else "Conversational style, approximately 15 minutes long. Speak in English."
        custom_textarea = None
        for _cw in range(5):
            for _ct_sel in [
                'textarea[aria-label*="custom"]',
                'textarea[aria-label*="自訂"]',
                'textarea[placeholder*="instruct"]',
                'textarea[placeholder*="指示"]',
                '.cdk-overlay-container textarea',
                'mat-dialog-container textarea',
                '[role="dialog"] textarea',
            ]:
                custom_textarea = await page.query_selector(_ct_sel)
                if custom_textarea:
                    try:
                        if await custom_textarea.is_visible():
                            break
                    except Exception:
                        pass
                    custom_textarea = None
            if custom_textarea:
                break
            await asyncio.sleep(1)

        if custom_textarea:
            try:
                await custom_textarea.focus()
            except Exception:
                await page.evaluate('(el) => el.focus()', custom_textarea)
            await asyncio.sleep(0.3)
            await custom_textarea.fill(en_prompt)
            await asyncio.sleep(0.5)
            await page.keyboard.press("End")
            await page.keyboard.type(" ")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(1)
            logger.info(f"[Step 5a] EN customization filled: {en_prompt[:80]}...")

            gen_btn = None
            for _gen_wait in range(10):
                gen_btn = await self._find_generate_btn(page)
                if gen_btn:
                    break
                await asyncio.sleep(1)
            if gen_btn:
                await self._force_click(page, gen_btn, "Generate (Audio EN)")
                await asyncio.sleep(5)
                logger.info("[Step 5a] English Audio generation started with customization")
            else:
                logger.warning("[Step 5a] Generate button not found")
                await self._screenshot(page, "no-audio-en-generate")
        else:
            logger.warning("[Step 5a] Customize textarea not found, checking if generation started")
            gen_started = await page.query_selector('text=正在生成語音摘要') or \
                          await page.query_selector('text=Generating Audio Overview')
            if gen_started:
                logger.info("[Step 5a] Audio generation started directly (no customization)")
            else:
                gen_btn = None
                for _gen_wait in range(5):
                    gen_btn = await self._find_generate_btn(page)
                    if gen_btn:
                        break
                    await asyncio.sleep(1)
                if gen_btn:
                    await self._force_click(page, gen_btn, "Generate (Audio EN fallback)")
                    await asyncio.sleep(5)
                    logger.info("[Step 5a] Audio started via Generate button (no customization)")
                else:
                    await self._screenshot(page, "no-audio-en-generate")
                    logger.warning("[Step 5a] Could not start audio generation")

        # ── Step 6: Verify audio generation ──
        await asyncio.sleep(2)
        generating = False
        for _ in range(10):
            gen_indicator = await page.query_selector('text=正在生成語音摘要') or \
                            await page.query_selector('text=Generating Audio Overview')
            if gen_indicator:
                generating = True
                break
            play_btn = await page.query_selector('[aria-label="Play"]') or \
                       await page.query_selector('[aria-label="播放"]')
            if play_btn:
                generating = True
                break
            await asyncio.sleep(1)

        if generating:
            logger.info("[Step 6] Audio generation confirmed")
        else:
            logger.warning("[Step 6] Audio generation indicator not found, proceeding anyway")

        # ── Step 7: Click Video Overview (independent from audio) ──
        video_clicked = False
        await asyncio.sleep(3)

        dialog_close = await page.query_selector('.cdk-overlay-container button[aria-label="Close"]') or \
                        await page.query_selector('.cdk-overlay-container button[aria-label="關閉"]')
        if dialog_close:
            try:
                if await dialog_close.is_visible():
                    await dialog_close.click(force=True)
                    logger.info("[Step 7] Closed lingering customization dialog")
                    await asyncio.sleep(2)
            except Exception:
                pass

        await self._dismiss_overlays(page)
        await asyncio.sleep(1)

        for wait_i in range(15):
            for sel in [
                'text=Video Overview',
                'span.create-label-container:has-text("Video Overview")',
                '[aria-label="Video Overview"]',
                'text=影片摘要',
                'span.create-label-container:has-text("影片摘要")',
                '[aria-label="影片摘要"]',
            ]:
                el = await page.query_selector(sel)
                if el:
                    logger.info(f"[Step 7] Found Video Overview via {sel}, clicking...")
                    clicked = await self._force_click(page, el, "Video Overview")
                    if clicked:
                        await asyncio.sleep(2)
                        current_url = page.url
                        if "notebook/" in current_url:
                            video_clicked = True
                            break
                        else:
                            logger.warning(f"[Step 7] URL changed after Video click! Going back...")
                            await page.goto(notebook_url_before)
                            await asyncio.sleep(3)
                            await self._dismiss_overlays(page)
                            st = await page.query_selector('[role="tab"]:has-text("Studio")') or \
                                 await page.query_selector('[role="tab"]:has-text("工作室")')
                            if st:
                                await self._force_click(page, st, "Studio (re-nav for video)")
                                await asyncio.sleep(2)
                                await self._dismiss_overlays(page)
                            continue
            if video_clicked:
                break
            if wait_i % 5 == 0:
                logger.info(f"[Step 7] Waiting for Video Overview... ({wait_i+1}/15)")
            await asyncio.sleep(1)

        if video_clicked:
            await asyncio.sleep(3)
            video_gen = await page.query_selector('text=正在生成影片摘要') or \
                        await page.query_selector('text=Generating Video Overview')
            if video_gen:
                logger.info("[Step 7] Video generation started directly (no dialog)")
            else:
                limit_el = await page.query_selector('text=reached your daily Video Overview limits') or \
                           await page.query_selector('text=已達到每日影片摘要上限')
                if limit_el:
                    logger.warning("[Step 7] Video Overview daily limit reached")
                else:
                    gen_btn = None
                    for _gw in range(5):
                        gen_btn = await self._find_generate_btn(page)
                        if gen_btn:
                            break
                        await asyncio.sleep(1)
                    if gen_btn:
                        await self._force_click(page, gen_btn, "Generate (Video)")
                        await asyncio.sleep(3)
                        logger.info("[Step 7] Video Overview generation initiated via Generate button")
                    else:
                        video_gen2 = await page.query_selector('text=正在生成影片摘要') or \
                                     await page.query_selector('text=Generating Video Overview')
                        if video_gen2:
                            logger.info("[Step 7] Video generation confirmed")
                        else:
                            await self._screenshot(page, "video-no-generate")
                            logger.warning("[Step 7] Video generation status unclear")
        else:
            await self._screenshot(page, "no-video-overview")
            logger.warning("[Step 7] Video Overview not found — continuing without video")

        notebook_url = page.url
        logger.info(f"Execute complete. Notebook: {notebook_url[:60]}")

        video_msg = " + Video" if video_clicked else " (video skipped)"
        return JobResult(
            success=True,
            message=f"Podcast generation started{video_msg}",
            output_path=notebook_url,
            provider=self.name,
            metadata={"video_overview_started": video_clicked},
        )

    # ── Phase 2: Check & Download ──

    async def check_and_download(self, notebook_url: str, save_path: str) -> tuple:
        """Phase 2: Check if audio is ready and download it.

        Returns: (is_ready: bool, audio_path: str or None, error: str or None)
        """
        if not self._firefox_mgr:
            return (False, None, "FirefoxManager not initialized")

        page = None
        try:
            # Get a page navigated to the notebook
            page = await self._firefox_mgr.get_page("firefox-notebooklm", notebook_url)
            await asyncio.sleep(3)
            await self._dismiss_overlays(page)
            logger.info(f"Checking notebook: {page.url}")

            # Cookie check
            if "accounts.google.com" in page.url:
                return (False, None, "Firefox cookies expired")

            # Go to Studio tab
            studio_tab = await page.query_selector('[role="tab"]:has-text("Studio")') or \
                         await page.query_selector('[role="tab"]:has-text("工作室")')
            if studio_tab:
                await self._force_click(page, studio_tab, "Studio tab (Phase 2)")
                await asyncio.sleep(2)

            # Check if still generating
            generating = await page.query_selector('button:has-text("Generating")') or \
                         await page.query_selector('button:has-text("正在產生")') or \
                         await page.query_selector('button:has-text("產生中")') or \
                         await page.query_selector('text=正在生成語音摘要') or \
                         await page.query_selector('text=Generating Audio Overview')
            if generating:
                logger.info("Audio still generating...")
                return (False, None, None)

            # Click audio card to expand player controls
            await page.evaluate("""() => {
                const icons = document.querySelectorAll('mat-icon');
                for (const icon of icons) {
                    if (icon.textContent.trim() === 'audio_magic_eraser' && icon.offsetParent !== null) {
                        let el = icon;
                        for (let i = 0; i < 5; i++) {
                            el = el.parentElement;
                            if (!el) break;
                            if (el.tagName === 'BUTTON') { el.click(); return; }
                        }
                        if (icon.parentElement && icon.parentElement.parentElement)
                            icon.parentElement.parentElement.click();
                        return;
                    }
                }
            }""")
            await asyncio.sleep(5)

            # Check for Play button (audio ready)
            play_btn = await page.query_selector('[aria-label="Play"]') or                        await page.query_selector('[aria-label="播放"]')
            if not play_btn:
                has_audio = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('mat-icon'))
                        .some(ic => ic.textContent.trim() === 'audio_magic_eraser' && ic.offsetParent !== null);
                }""")
                if not has_audio:
                    return (False, None, "No Audio Overview on page")
                return (False, None, None)

            # Find more_vert paired with play button (same y, right panel x>1000)
            more_btn_handle = await page.evaluate_handle("""() => {
                const btns = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null);
                const plays = btns.filter(b => ['播放','Play'].includes(b.getAttribute('aria-label')));
                const mores = btns.filter(b => {
                    const ic = b.querySelector('mat-icon');
                    return ic && ic.textContent.trim() === 'more_vert' && b.getBoundingClientRect().x > 1000;
                });
                for (const play of plays) {
                    const py = play.getBoundingClientRect().y;
                    for (const more of mores) {
                        if (Math.abs(py - more.getBoundingClientRect().y) < 20) return more;
                    }
                }
                return null;
            }""")
            more_btn = None
            if more_btn_handle:
                try:
                    el = more_btn_handle.as_element()
                    if el and await el.is_visible():
                        more_btn = el
                except Exception:
                    pass
            if not more_btn:
                return (True, None, "Audio player more_vert not found")

            logger.info("Audio ready, downloading...")


            save_file = Path(save_path)
            save_file.parent.mkdir(parents=True, exist_ok=True)

            # Click more_vert to open menu
            await more_btn.click(force=True)
            await asyncio.sleep(1)

            # Click download in menu (search by mat-icon text)
            dl_clicked = False
            dl_handle = await page.evaluate_handle("""() => {
                const items = document.querySelectorAll('[role="menuitem"]');
                for (const item of items) {
                    const icons = item.querySelectorAll('mat-icon, [class*="icon"]');
                    for (const ic of icons) {
                        const t = ic.textContent.trim();
                        if ((t === 'download' || t === 'save_alt') && item.offsetParent !== null)
                            return item;
                    }
                }
                for (const item of items) {
                    const t = item.innerText.trim();
                    if ((t.includes('下載') || t.includes('Download')) && item.offsetParent !== null)
                        return item;
                }
                return null;
            }""")
            if dl_handle:
                try:
                    dl_el = dl_handle.as_element()
                    if dl_el:
                        dl_clicked = True
                except Exception:
                    pass

            if not dl_clicked:
                return (True, None, "Download button not found in menu")

            try:
                async with page.expect_download(timeout=120000) as dl_info:
                    await dl_el.click()
                download = await dl_info.value
                await download.save_as(str(save_file))

                size = save_file.stat().st_size
                size_mb = size / (1024 * 1024)
                if size < 1000:
                    return (True, None, f"Downloaded file too small ({size} bytes)")

                logger.info(f"Audio downloaded: {size_mb:.1f}MB → {save_file}")

                # Re-mux to fix DASH format (ensures duration metadata for all players)
                try:
                    import subprocess
                    fixed = save_file.with_suffix('.fixed' + save_file.suffix)
                    subprocess.run(
                        ['ffmpeg', '-i', str(save_file), '-c', 'copy',
                         '-movflags', '+faststart', str(fixed), '-y'],
                        capture_output=True, timeout=60,
                    )
                    if fixed.exists() and fixed.stat().st_size > 1000:
                        fixed.replace(save_file)
                        logger.info(f"Re-muxed with faststart: {save_file}")
                except Exception as e:
                    logger.warning(f"Re-mux failed (original kept): {e}")

                return (True, str(save_file), None)
            except Exception as e:
                logger.error(f"Download failed: {type(e).__name__}: {e}")
                return (True, None, f"Download error: {e}")

        except Exception as e:
            logger.error(f"check_and_download error: {e}")
            return (False, None, str(e))
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-notebooklm")
        return False
