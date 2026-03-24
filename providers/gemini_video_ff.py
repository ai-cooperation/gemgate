"""Gemini Veo video provider — Firefox + Cookie injection.

v4.1: Fixed model selector (PRO badge false positive) + correct video chip name.
Uses same Firefox instance as gemini_image (firefox-gemini queue key).
Video download: blob URL fetch via page context, or direct HTTP download.
"""
import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path

from providers.base import BaseProvider, JobResult
from config import OUTPUT_DIRS

logger = logging.getLogger("ai-hub.gemini_video")

GEMINI_URL = "https://gemini.google.com/app"

# Input selectors (locale-independent, ordered by priority)
INPUT_SELECTORS = [
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
    '.ql-editor.textarea',
]

# Send button selectors (ordered by priority)
SEND_SELECTORS = [
    'button:has([data-mat-icon-name="send"])',
    'button[aria-label*="傳送"]',
    'button[aria-label*="Send"]',
]

# "Create video" chip selectors (EN and zh-TW)
VIDEO_CHIP_SELECTORS = [
    'button[aria-label*="製作影片"]',
    'button[aria-label*="建立影片"]',
    'button[aria-label*="Create video"]',
    'button[aria-label*="Generate video"]',
]


class GeminiVideoProvider(BaseProvider):
    name = "gemini_video"
    category = "video"
    chrome_profile = "firefox-gemini"  # Queue key for job serialization
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py

    async def execute(self, params: dict) -> JobResult:
        prompt = params.get("prompt", "")
        timeout = params.get("timeout", 900)

        if not prompt:
            return JobResult(False, "Prompt is required", provider=self.name)

        if not self._firefox_mgr:
            return JobResult(False, "FirefoxManager not initialized", provider=self.name)

        start = time.time()
        file_id = str(uuid.uuid4())[:8]
        video_path = Path(OUTPUT_DIRS["videos"]) / f"veo_{file_id}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)

        page = None
        try:
            # --- Phase 1: Get authenticated Gemini page ---
            page = await self._firefox_mgr.get_or_reuse_page("firefox-gemini", GEMINI_URL)

            # Dismiss overlays (conservative)
            await self._dismiss_overlays(page)

            # Debug: screenshot + page text after load
            try:
                await page.screenshot(path="/opt/gemgate/state/screenshots/veo_debug_01_loaded.png")
                body_text = await page.inner_text("body")
                logger.info(f"Page text after load (first 300): {body_text[:300]}")
                logger.info(f"Page URL: {page.url}")
            except Exception as e:
                logger.warning(f"Debug screenshot failed: {e}")

            # --- Login check ---
            try:
                page_text = await page.inner_text('body')
                if 'Sign in to connect' in page_text or (
                    'Sign in' in page_text and 'Sign out' not in page_text
                    and '登入' not in page_text
                ):
                    sign_in_btn = await page.query_selector('a:has-text("Sign in"), button:has-text("Sign in")')
                    if sign_in_btn and await sign_in_btn.is_visible():
                        logger.error('Not logged in to Google — cookies expired.')
                        return JobResult(False, 'Not logged in. Cookies expired.', provider=self.name)
            except Exception as e:
                logger.warning(f'Login check error: {e}')

            # --- Phase 2: Select model (thinking/思考型) ---
            model_ok = await self._ensure_thinking_model(page)
            if not model_ok:
                logger.warning("Cannot switch to thinking model, using current")

            # --- Phase 3: Activate video mode (製作影片 chip) ---
            chip_ok = await self._activate_video_mode(page)
            if not chip_ok:
                logger.warning("Video chip unavailable, proceeding with text prompt")

            # Count existing video elements before sending prompt
            existing_video_srcs = await self._collect_video_srcs(page)
            logger.info(f"Existing videos before prompt: {len(existing_video_srcs)}")

            # --- Phase 4: Type prompt and send ---
            input_ok = await self._type_prompt(page, prompt)
            if not input_ok:
                return JobResult(False, "Cannot find input field", provider=self.name)

            await self._click_send(page)
            logger.info(f"Video prompt sent: {prompt[:50]}...")

            # Debug: screenshot after send
            await asyncio.sleep(3)
            try:
                await page.screenshot(path="/opt/gemgate/state/screenshots/veo_debug_02_sent.png")
                body_text = await page.inner_text("body")
                logger.info(f"Page text after send (first 500): {body_text[:500]}")
            except Exception as e:
                logger.warning(f"Debug screenshot after send failed: {e}")

            # --- Phase 5: Poll for new video element ---
            waited = 0
            new_video_src = None
            while waited < timeout:
                await asyncio.sleep(5)
                waited += 5

                current_srcs = await self._collect_video_srcs(page)
                new_srcs = current_srcs - existing_video_srcs

                if waited == 30:
                    try:
                        resp_text = await page.inner_text('body')
                        if 'Sign in to connect' in resp_text:
                            return JobResult(False, 'Not logged in.', provider=self.name)
                        if "couldn't do that" in resp_text.lower() or 'try again later' in resp_text.lower():
                            logger.error(f'Gemini refused: {resp_text[:200]}')
                            return JobResult(False, 'Gemini refused request', provider=self.name)
                    except Exception:
                        pass
                if new_srcs:
                    new_video_src = next(iter(new_srcs))
                    logger.info(f"New video detected: {new_video_src[:80]}...")
                    break

                if waited % 30 == 0:
                    logger.info(f"Waiting for video... ({waited}s/{timeout}s)")
                if waited in (60, 180, 300):
                    try:
                        await page.screenshot(path=f"/opt/gemgate/state/screenshots/veo_debug_poll_{waited}s.png")
                    except Exception:
                        pass

            if not new_video_src:
                try:
                    await page.screenshot(path="/opt/gemgate/state/screenshots/veo_debug_timeout.png")
                except Exception:
                    pass
                return JobResult(False, f"No video generated after {timeout}s", provider=self.name)

            # --- Phase 6: Download video ---
            dl_ok = await self._download_video(page, new_video_src, str(video_path))
            if not dl_ok:
                return JobResult(False, "Video download failed", provider=self.name)

            elapsed = time.time() - start
            return JobResult(
                success=True,
                message=f"Video generated in {elapsed:.1f}s",
                output_path=str(video_path),
                generation_time=elapsed,
                provider=self.name,
            )

        except Exception as e:
            logger.error(f"Gemini video error: {type(e).__name__}: {e}")
            return JobResult(False, str(e), provider=self.name)
        finally:
            pass

    # ── Model selector ──

    async def _ensure_thinking_model(self, page) -> bool:
        """Ensure Gemini is on thinking/Pro model (思考型).
        
        IMPORTANT: The page may contain a 'PRO' badge/label that is NOT the model selector.
        The actual model selector button shows: 快捷 / 思考型 / Pro
        We must find the REAL model selector, not the PRO subscription badge.
        """
        try:
            # Find the actual model selector — it's the small button near the input area
            # that shows the current mode name (快捷, 思考型, etc.)
            # Exclude buttons that are just badges or large UI elements
            current = await page.evaluate("""() => {
                const MODEL_NAMES = ['快捷', '思考型', 'Flash', 'Deep Think', '深度思考'];
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    // Only match exact model names (not PRO badge)
                    if (MODEL_NAMES.includes(t)) return t;
                }
                return null;
            }""")

            if current is None:
                # Retry a few times
                for attempt in range(10):
                    await asyncio.sleep(2)
                    current = await page.evaluate("""() => {
                        const MODEL_NAMES = ['快捷', '思考型', 'Flash', 'Deep Think', '深度思考'];
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            const t = b.textContent.trim();
                            if (MODEL_NAMES.includes(t)) return t;
                        }
                        return null;
                    }""")
                    if current is not None:
                        break
                    logger.debug(f"Model selector not found, retry {attempt+1}/10")

            if current is None:
                logger.warning("Model selector not found after retries")
                return False

            logger.info(f"Current model: {current}")

            if current in ('思考型', 'Deep Think'):
                logger.info("Already on thinking model")
                return True

            # Click the model selector button to open dropdown
            await page.evaluate("""() => {
                const MODEL_NAMES = ['快捷', '思考型', 'Flash', 'Deep Think', '深度思考'];
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    if (MODEL_NAMES.includes(t)) { b.click(); return; }
                }
            }""")
            await asyncio.sleep(1.5)

            # Log dropdown items for debugging
            dropdown_items = await page.evaluate("""() => {
                const results = [];
                const items = document.querySelectorAll('[role="menuitemradio"]');
                for (const i of items) results.push('radio:' + i.textContent.trim().substring(0, 40));
                const testIds = document.querySelectorAll('[data-test-id*="mode"]');
                for (const t of testIds) results.push('testid:' + t.getAttribute('data-test-id') + '=' + t.textContent.trim().substring(0, 30));
                return results;
            }""")
            logger.info(f"Dropdown items: {dropdown_items}")

            # Select thinking/Pro option from dropdown
            selected = await page.evaluate("""() => {
                const items = document.querySelectorAll('[role="menuitemradio"]');
                for (const i of items) {
                    const t = i.textContent.trim();
                    if (t.startsWith('思考') || t.includes('Deep Think')) { i.click(); return true; }
                }
                // Fallback: try Pro
                for (const i of items) {
                    const t = i.textContent.trim();
                    if (t.startsWith('Pro') || t.startsWith('PRO') || t.includes('Pro')) { i.click(); return true; }
                }
                return false;
            }""")
            await asyncio.sleep(1)
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

            if selected:
                logger.info("Switched to thinking model")
            else:
                logger.warning("Failed to select thinking model")
            return bool(selected)
        except Exception as e:
            logger.warning(f"Model switch error: {type(e).__name__}: {e}")
            return False

    # ── Video mode activation ──

    async def _activate_video_mode(self, page) -> bool:
        """Click the video chip (製作影片 / Create video)."""
        # Try aria-label selectors first
        for chip_sel in VIDEO_CHIP_SELECTORS:
            try:
                btn = await page.wait_for_selector(chip_sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(2)
                    logger.info("Video mode activated: " + chip_sel)
                    return True
            except Exception:
                continue

        # JS fallback: search button text for 製作影片 / 建立影片 / Create video
        clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const t = b.textContent.trim();
                if (t.includes('製作影片') || t.includes('建立影片') || t.includes('Create video')) {
                    b.click();
                    return true;
                }
            }
            // Also try non-button elements (chips can be divs)
            const chips = document.querySelectorAll('[role="button"], [class*="chip"]');
            for (const c of chips) {
                const t = c.textContent.trim();
                if (t.includes('製作影片') || t.includes('建立影片') || t.includes('Create video')) {
                    c.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(2)
            logger.info("Video mode activated via JS fallback")
        else:
            logger.warning("Video chip not found in DOM")
        return clicked

    # ── Input helpers ──

    async def _type_prompt(self, page, prompt: str) -> bool:
        input_el = None
        for _ in range(15):
            for sel in INPUT_SELECTORS:
                input_el = await page.query_selector(sel)
                if input_el and await input_el.is_visible():
                    break
                input_el = None
            if input_el:
                break
            await asyncio.sleep(1)
        if not input_el:
            return False
        await input_el.click()
        await asyncio.sleep(0.5)
        await page.keyboard.press("Control+a")
        await page.keyboard.type(prompt)
        await asyncio.sleep(0.5)
        return True

    async def _click_send(self, page):
        for sel in SEND_SELECTORS:
            try:
                btn = await page.wait_for_selector(sel, state="visible", timeout=5000)
                if btn:
                    await btn.click()
                    return
            except Exception:
                continue
        await page.keyboard.press("Enter")

    # ── Overlay dismissal ──

    async def _dismiss_overlays(self, page):
        for sel in [
            'button:has-text("Not now")',
            'button:has-text("Got it")',
            'button:has-text("Skip")',
            'button:has-text("Dismiss")',
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.5)
            except Exception:
                continue

    # ── Video detection + download ──

    async def _collect_video_srcs(self, page) -> set:
        srcs = set()
        try:
            videos = await page.query_selector_all("video")
            for vid in videos:
                src = await vid.get_attribute("src") or ""
                if src:
                    srcs.add(src)
                sources = await vid.query_selector_all("source")
                for s in sources:
                    s_src = await s.get_attribute("src") or ""
                    if s_src:
                        srcs.add(s_src)
        except Exception:
            pass
        return srcs

    async def _download_video(self, page, video_src: str, save_path: str) -> bool:
        try:
            # Method 1: context.request
            try:
                response = await page.context.request.get(video_src)
                if response.ok:
                    ct = response.headers.get('content-type') or ''
                    if 'text/html' not in ct:
                        body = await response.body()
                        with open(save_path, "wb") as f:
                            f.write(body)
                        size = Path(save_path).stat().st_size
                        logger.info(f"Video downloaded (context.request): {save_path} ({size} bytes)")
                        if size > 1000:
                            return True
            except Exception as e:
                logger.warning(f"context.request download failed: {type(e).__name__}: {e}")

            # Method 2: expect_download
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    await page.evaluate("""(url) => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = 'video.mp4';
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""", video_src)
                download = await dl_info.value
                await download.save_as(save_path)
                size = Path(save_path).stat().st_size
                logger.info(f"Video downloaded (expect_download): {save_path} ({size} bytes)")
                if size > 1000:
                    return True
            except Exception as e:
                logger.warning(f"expect_download failed: {type(e).__name__}: {e}")

            # Method 3: in-page fetch
            try:
                b64_data = await page.evaluate("""
                    async (url) => {
                        try {
                            const resp = await fetch(url);
                            if (!resp.ok) return null;
                            const ct = resp.headers.get('content-type') || '';
                            if (ct.includes('text/html')) return null;
                            const blob = await resp.blob();
                            return new Promise((resolve) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                reader.readAsDataURL(blob);
                            });
                        } catch(e) { return null; }
                    }
                """, video_src)
                if b64_data:
                    with open(save_path, "wb") as f:
                        f.write(base64.b64decode(b64_data))
                    size = Path(save_path).stat().st_size
                    logger.info(f"Video downloaded (page fetch): {save_path} ({size} bytes)")
                    if size > 1000:
                        return True
            except Exception as e:
                logger.warning(f"page.evaluate fetch failed: {type(e).__name__}: {e}")

            logger.error("All 3 download methods failed")
            return False
        except Exception as e:
            logger.error(f"Video download failed: {type(e).__name__}: {e}")
            return False

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-gemini")
        return False
