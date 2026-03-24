"""Gemini Image provider — browser automation via Firefox + Cookie injection.

v3: Migrated from Chrome CDP to Firefox (Chrome 145 CDP hang bug workaround)
- Uses FirefoxManager for browser lifecycle + Google cookie authentication
- All DOM interaction code unchanged (selectors work in both Chrome and Firefox)
- Pro model auto-switch with retries + abort on failure
- Post-gen vision verification safety net (async, non-blocking)
"""
import asyncio
import base64
import logging
import os
import subprocess
import time
import re
import uuid
import urllib.request
from pathlib import Path
from typing import Optional

from providers.base import BaseProvider, JobResult
from config import OUTPUT_DIRS

logger = logging.getLogger("ai-hub.gemini_image")

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

# "Create image" chip selectors (EN and zh-TW)
IMAGE_CHIP_SELECTORS = [
    'button[aria-label*="Create image"]',
    'button[aria-label*="生成圖片"]',
    'button[aria-label*="Generate image"]',
]


def _send_telegram(msg: str):
    """Send Telegram notification (non-blocking, fire-and-forget)."""
    try:
        subprocess.Popen(
            ["/usr/local/bin/telegram-notify.sh", msg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class GeminiImageProvider(BaseProvider):
    name = "gemini_image"
    category = "image"
    chrome_profile = "firefox-gemini"  # Queue key for job serialization
    requires_chrome = False  # No Chrome CDP needed

    _firefox_mgr = None  # Set by main.py

    async def execute(self, params: dict) -> JobResult:
        prompt = params.get("prompt", "")
        timeout = params.get("timeout", 90)
        skip_base64 = params.get("skip_base64", False)
        expected_title = params.get("expected_title", "")
        if not expected_title:
            m = re.search(r'「(.+?)」', prompt) or re.search(r'Chinese title text "(.+?)"', prompt)
            if m:
                expected_title = m.group(1)

        model_choice = params.get("model", "auto")
        if model_choice != "fast" and timeout <= 90:
            timeout = 150

        if not prompt:
            return self._fail("Prompt is required")

        if not self._firefox_mgr:
            return self._fail("FirefoxManager not initialized")

        start = time.time()
        file_id = str(uuid.uuid4())[:8]
        image_path = Path(OUTPUT_DIRS["images"]) / f"gemini_{file_id}.png"

        page = None
        try:
            # --- Phase 1: Get authenticated Gemini page ---
            page = await self._firefox_mgr.get_or_reuse_page("firefox-gemini", GEMINI_URL)

            # Dismiss overlays (conservative - only known tour/promo buttons)
            await self._dismiss_overlays(page)

            # --- Phase 2: Select model (pro/fast) ---
            if model_choice == "fast":
                model_ok = await self._ensure_fast_model(page)
                if not model_ok:
                    logger.warning("Cannot switch to fast model, using current")
            else:
                model_ok = await self._ensure_thinking_model(page)
                if not model_ok:
                    return self._fail("Cannot switch to Pro model")

            # --- Phase 3: Activate image mode and generate ---
            chip_ok = await self._activate_image_mode(page)
            if not chip_ok:
                logger.warning("Image chip unavailable, proceeding without image mode")

            input_ok = await self._type_prompt(page, prompt)
            if not input_ok:
                return self._fail("Cannot find or fill input field")

            existing_srcs = await self._collect_existing_images(page)
            await self._click_send(page)
            logger.info("Prompt sent: " + prompt[:50] + "...")

            new_img_src = await self._wait_for_image(page, existing_srcs, timeout)
            if not new_img_src:
                return self._fail(f"No image generated after {timeout}s")

            # Download image
            dl_ok = await self._download_image(page, new_img_src, image_path)
            if not dl_ok:
                return self._fail("Image download failed")

            # --- Phase 4: Build result ---
            elapsed = time.time() - start
            img_b64 = None
            if not skip_base64:
                with open(image_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")

            result = JobResult(
                success=True,
                message=f"Generated in {elapsed:.1f}s",
                output_path=str(image_path),
                output_base64=img_b64,
                generation_time=elapsed,
                provider=self.name,
            )

            # --- Phase 5: Vision verification ---
            if expected_title and img_b64 and model_choice != "fast":
                verify_ok = await self._vision_verify(img_b64, expected_title)
                if not verify_ok:
                    self._mark_pro_exhausted()
                    _send_telegram(
                        f"⚠️ Gemini Pro 額度耗盡!\n"
                        f"Vision verify 偵測到亂碼\n"
                        f"預期: {expected_title[:20]}\n"
                        f"今日後續 Pro 請求返回 503"
                    )
                    return self._fail("Vision verify failed: CJK garbled (Pro quota exhausted)")
                logger.info(f"Vision verify passed for: {expected_title[:20]}")

            return result

        except Exception as e:
            logger.error(f"Gemini image error: {e}")
            return self._fail(str(e))
        finally:
            # Don't close the page — reuse it for next job (avoids slow Gemini JS re-init)
            pass

    def _fail(self, message: str) -> JobResult:
        logger.error(f"Image generation failed: {message}")
        return JobResult(False, message, provider=self.name)

    # ── Model management (unchanged — uses standard Playwright selectors) ──

    async def _ensure_thinking_model(self, page) -> bool:
        """Ensure Gemini is on 思考型 model using data-test-id selectors."""
        try:
            # Step 1: Check current model via menu button text
            for attempt in range(10):
                current = await page.evaluate("""() => {
                    const btn = document.querySelector('[data-test-id="bard-mode-menu-button"]');
                    return btn ? btn.innerText.split(String.fromCharCode(10))[0].trim() : null;
                }""")
                if current:
                    break
                logger.debug(f"Model menu button not found, retry {attempt+1}/10")
                await asyncio.sleep(2)

            if not current:
                logger.warning("Model menu button not found after 10 retries")
                return False

            logger.info(f"Current model button text: {current!r}")
            if current in ('思考型', 'Pro', 'PRO'):
                logger.info(f"Already on Pro/thinking model (current={current})")
                return True

            # Step 2: Click menu button to open dropdown
            await page.click('[data-test-id="bard-mode-menu-button"]')
            await asyncio.sleep(1.5)

            # Step 3: Click 思考型 option via data-test-id
            selected = await page.evaluate("""() => {
                // Primary: 思考型
                const item = document.querySelector('[data-test-id="bard-mode-option-思考型"]');
                if (item) { item.click(); return '思考型-testid'; }
                // Fallback: Pro option
                const pro = document.querySelector('[data-test-id="bard-mode-option-pro"]');
                if (pro) { pro.click(); return 'pro-testid'; }
                // Fallback: role=menuitem
                const items = document.querySelectorAll('[role="menuitem"]');
                for (const i of items) {
                    const t = i.textContent.trim();
                    if (t.startsWith('思考型') || t.startsWith('Pro')) { i.click(); return 'menuitem'; }
                }
                return null;
            }""")
            await asyncio.sleep(1)

            if selected:
                logger.info(f"Switched to thinking model via {selected}")
                return True
            else:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                logger.warning("Failed to select thinking model")
                return False
        except Exception as e:
            logger.warning(f"Model switch error: {type(e).__name__}: {e}")
            return False

    async def _ensure_fast_model(self, page) -> bool:
        """Switch Gemini to fast model using data-test-id selectors."""
        try:
            # Step 1: Check current model via menu button text
            for attempt in range(10):
                current = await page.evaluate("""() => {
                    const btn = document.querySelector('[data-test-id="bard-mode-menu-button"]');
                    if (!btn) return null;
                    // Get first line of button text (model name without description)
                    const parts = btn.innerText.split(String.fromCharCode(10));
                    return parts[0].trim();
                }""")
                if current:
                    break
                logger.debug(f"Model menu button not found, retry {attempt+1}/10")
                await asyncio.sleep(2)

            if not current:
                logger.warning("Model menu button not found after 10 retries")
                return False

            logger.info(f"Current model button text: {current!r}")
            if current == '快捷':
                logger.info("Already on fast model")
                return True

            # Step 2: Click menu button to open dropdown
            await page.click('[data-test-id="bard-mode-menu-button"]')
            await asyncio.sleep(1.5)

            # Step 3: Click 快捷 option via data-test-id
            selected = await page.evaluate("""() => {
                const item = document.querySelector('[data-test-id="bard-mode-option-快捷"]');
                if (item) { item.click(); return 'testid'; }
                const items = document.querySelectorAll('[role="menuitem"]');
                for (const i of items) {
                    const t = i.textContent.trim();
                    if (t.startsWith('快捷') || t.includes('Flash')) { i.click(); return 'menuitem'; }
                }
                return null;
            }""")
            await asyncio.sleep(1)

            if selected:
                logger.info(f"Switched to fast model via {selected}")
                return True
            else:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                logger.warning("Failed to select fast model")
                return False
        except Exception as e:
            logger.warning(f"Fast model switch error: {type(e).__name__}: {e}")
            return False

    # ── Image generation helpers (unchanged) ──

    async def _activate_image_mode(self, page) -> bool:
        for chip_sel in IMAGE_CHIP_SELECTORS:
            try:
                btn = await page.wait_for_selector(chip_sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(2)
                    logger.info("Image mode activated: " + chip_sel)
                    return True
            except Exception:
                continue

        clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                if (b.textContent.includes('生成圖片')) { b.click(); return true; }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(2)
            logger.info("Image mode activated via JS fallback")
        return clicked

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

    async def _collect_existing_images(self, page) -> set:
        srcs = set()
        for img in await page.query_selector_all("img"):
            src = await img.get_attribute("src") or ""
            if "googleusercontent.com" in src:
                srcs.add(src)
        return srcs

    async def _wait_for_image(self, page, existing_srcs: set, timeout: int) -> Optional[str]:
        waited = 0
        while waited < timeout:
            await asyncio.sleep(2)
            waited += 2
            for img in await page.query_selector_all("img"):
                src = await img.get_attribute("src") or ""
                if "googleusercontent.com" in src and src not in existing_srcs:
                    logger.info("New image detected")
                    return src
        return None

    async def _download_image(self, page, src: str, path: Path) -> bool:
        """Download image. Tries context.request (cookies, no CORS), then urllib, then screenshot."""
        # Method 1: Playwright context.request — carries browser cookies, no CORS
        try:
            response = await page.context.request.get(src)
            if response.ok:
                ct = response.headers.get("content-type") or ""
                if "text/html" not in ct:
                    body = await response.body()
                    with open(str(path), "wb") as f:
                        f.write(body)
                    if path.stat().st_size > 1000:
                        logger.info(f"Image downloaded (context.request): {path.stat().st_size} bytes")
                        return True
                else:
                    logger.warning("context.request got HTML, trying urllib")
            else:
                logger.warning(f"context.request status {response.status}, trying urllib")
        except Exception as e:
            logger.warning(f"context.request failed: {e}, trying urllib")

        # Method 2: direct HTTP download (works if URL has embedded token)
        try:
            urllib.request.urlretrieve(src, str(path))
            if path.stat().st_size > 1000:
                logger.info(f"Image downloaded (urllib): {path.stat().st_size} bytes")
                return True
        except Exception:
            pass

        # Method 3 (last resort): element screenshot — low resolution
        try:
            img_el = await page.evaluate("""(targetSrc) => {
                const imgs = document.querySelectorAll('img');
                for (const img of imgs) {
                    if (img.src === targetSrc) {
                        img.id = '__dl_target__';
                        return true;
                    }
                }
                return false;
            }""", src)
            if img_el:
                el = await page.query_selector('#__dl_target__')
                if el:
                    await el.screenshot(path=str(path))
                    if path.exists() and path.stat().st_size > 1000:
                        logger.warning("Image downloaded via screenshot (low res)")
                        return True
        except Exception as e:
            logger.error(f"Image download failed: {e}")

        return False

    # ── Vision verification (unchanged) ──

    async def _vision_verify(self, img_b64: str, expected_title: str) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "http://127.0.0.1:8760/api/vision/analyze",
                    json={
                        "image_base64": img_b64,
                        "prompt": "這張圖片上的中文標題文字是什麼？只回傳標題文字，不要其他描述。",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(f"Vision verify HTTP {resp.status_code}")
                    return True

                data = resp.json()
                detected = data.get("content", "").strip()
                if not detected:
                    logger.warning("Vision verify: no text detected")
                    return True

                match_count = sum(1 for c in expected_title if c in detected)
                ratio = match_count / max(len(expected_title), 1)

                if ratio >= 0.5:
                    logger.info(f"Vision OK ({ratio:.0%}): {detected[:30]}")
                    return True
                else:
                    logger.warning(
                        f"Vision FAIL ({ratio:.0%}): expected={expected_title}, detected={detected[:30]}"
                    )
                    return False
        except Exception as e:
            logger.warning(f"Vision verify error: {type(e).__name__}: {e}")
            return True

    def _mark_pro_exhausted(self):
        from datetime import date
        flag_path = Path("/opt/gemgate/state/pro_exhausted_today")
        try:
            flag_path.write_text(str(date.today()))
            logger.warning(f"Pro exhaustion flag set for {date.today()}")
        except Exception as e:
            logger.error(f"Failed to write exhaustion flag: {e}")

    @staticmethod
    def is_pro_exhausted() -> bool:
        from datetime import date
        flag_path = Path("/opt/gemgate/state/pro_exhausted_today")
        try:
            if flag_path.exists():
                return flag_path.read_text().strip() == str(date.today())
        except Exception:
            pass
        return False

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

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-gemini")
        return False
