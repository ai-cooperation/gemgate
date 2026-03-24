"""Gemini Chat provider — LLM + Vision via Firefox + Cookie injection.

v3: Migrated from Chrome CDP to Firefox.
Uses dedicated Firefox instance (firefox-gemini-chat) separate from
image/video (firefox-gemini) to avoid queue contention.
Backup for Groq LLM. Also handles vision (image understanding) tasks.
Always uses 思考型 model for better reasoning and web search.
"""
import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path

from providers.base import BaseProvider, JobResult

logger = logging.getLogger("ai-hub.gemini-chat")

GEMINI_URL = "https://gemini.google.com/app"

INPUT_SELECTORS = [
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
    '.ql-editor.textarea',
]

SEND_SELECTORS = [
    'button:has([data-mat-icon-name="send"])',
    'button[aria-label*="傳送"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="submit"]',
]

RESP_SELECTORS = '.response-container .markdown, .model-response-text'


class GeminiChatProvider(BaseProvider):
    name = "gemini_chat"
    category = "llm"
    chrome_profile = "firefox-gemini-chat"
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py

    async def execute(self, params: dict) -> JobResult:
        prompt = params.get("prompt", "")
        image_base64 = params.get("image_base64", "")

        if not prompt:
            return JobResult(False, "Prompt is required", provider=self.name)
        if not self._firefox_mgr:
            return JobResult(False, "FirefoxManager not initialized", provider=self.name)

        start = time.time()
        page = None

        try:
            # Get authenticated Gemini page
            page = await self._firefox_mgr.get_or_reuse_page("firefox-gemini-chat", GEMINI_URL)

            # Switch to 思考型 model
            model_ok = await self._ensure_thinking_model(page)
            if not model_ok:
                logger.warning("Could not switch to 思考型 model, proceeding anyway")

            # Wait for input box
            input_el = None
            for sel in INPUT_SELECTORS:
                try:
                    input_el = await page.wait_for_selector(sel, state="visible", timeout=10000)
                    if input_el:
                        break
                except Exception:
                    continue

            if not input_el:
                return JobResult(False, "Gemini input not ready after 10s", provider=self.name)

            # If image provided, upload it first
            image_uploaded = False
            if image_base64:
                img_data = base64.b64decode(image_base64)
                tmp_path = f"/tmp/vision_{uuid.uuid4().hex[:8]}.png"
                Path(tmp_path).write_bytes(img_data)

                try:
                    await input_el.click()
                    await page.wait_for_timeout(1000)

                    plus_btn = page.locator('[aria-label="開啟上傳檔案選單"]')
                    await plus_btn.first.wait_for(state="visible", timeout=5000)
                    await plus_btn.first.click()
                    await page.wait_for_timeout(1000)

                    upload_item = page.locator('button[role="menuitem"]').filter(has_text="上傳檔案").first
                    async with page.expect_file_chooser(timeout=10000) as fc_info:
                        await upload_item.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(tmp_path)
                    await page.wait_for_timeout(3000)
                    image_uploaded = True
                    logger.info("File uploaded via 3-step menu flow")
                except Exception as e:
                    logger.warning(f"File upload failed: {type(e).__name__}: {e}")

                Path(tmp_path).unlink(missing_ok=True)

                if not image_uploaded:
                    return JobResult(False, "Image upload failed, aborting", provider=self.name)

            # Re-query input (avoid detachment from upload/DOM changes)
            input_el = None
            for sel in INPUT_SELECTORS:
                input_el = await page.query_selector(sel)
                if input_el:
                    try:
                        if await input_el.is_visible():
                            break
                    except Exception:
                        input_el = None
                        continue
                input_el = None

            if not input_el:
                return JobResult(False, "Input detached, cannot type", provider=self.name)

            # Type prompt
            await input_el.click()
            await page.wait_for_timeout(200)
            await page.keyboard.press("Control+a")
            await page.wait_for_timeout(100)
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(200)
            await input_el.fill(prompt)
            await page.wait_for_timeout(500)

            # Count existing responses BEFORE sending
            old_count = await page.locator(RESP_SELECTORS).count()

            # Wait for send button
            send_el = None
            for sel in SEND_SELECTORS:
                try:
                    send_el = await page.wait_for_selector(sel, state="visible", timeout=5000)
                    if send_el:
                        break
                except Exception:
                    continue

            if not send_el:
                return JobResult(False, "Gemini send button not found", provider=self.name)

            await send_el.click()

            # Wait for NEW response
            new_response_appeared = False
            for wait_i in range(90):
                await page.wait_for_timeout(1000)
                current_count = await page.locator(RESP_SELECTORS).count()
                if current_count > old_count:
                    new_response_appeared = True
                    break

            if not new_response_appeared:
                mr = await page.query_selector_all('model-response')
                if len(mr) > old_count:
                    new_response_appeared = True

            if not new_response_appeared:
                return JobResult(False, "Gemini: no new response after 90s", provider=self.name)

            # Wait for stop button to disappear
            for _ in range(90):
                loading = await page.query_selector('[aria-label*="Stop"], [aria-label*="停止"]')
                if not loading:
                    break
                await page.wait_for_timeout(1000)

            await page.wait_for_timeout(2000)

            # Extract response with stability check
            text = ""
            prev_len = -1
            for _stab in range(10):
                responses = page.locator(RESP_SELECTORS)
                count = await responses.count()
                if count > 0:
                    text = await responses.nth(count - 1).inner_text()
                else:
                    mc = await page.query_selector_all('message-content')
                    if mc:
                        text = await mc[-1].inner_text()
                if len(text) > 0 and len(text) == prev_len:
                    break
                prev_len = len(text)
                await page.wait_for_timeout(1000)

            elapsed = time.time() - start

            if not text:
                return JobResult(False, "No response from Gemini", provider=self.name)

            logger.info(f"Gemini chat done: {len(text)} chars, {elapsed:.1f}s")

            return JobResult(
                success=True,
                message=text,
                generation_time=elapsed,
                provider=self.name,
            )

        except Exception as e:
            logger.error(f"Gemini chat error: {e}")
            return JobResult(False, str(e), provider=self.name)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _ensure_thinking_model(self, page) -> bool:
        """Switch Gemini to 思考型 model."""
        try:
            for attempt in range(10):
                current = await page.evaluate("""() => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        const t = b.textContent.trim();
                        if (['快捷', '思考型', 'Pro'].includes(t)) return t;
                    }
                    return null;
                }""")
                if current == '思考型':
                    logger.info("Already on 思考型 model")
                    return True
                if current is not None:
                    break
                if attempt < 4:
                    await asyncio.sleep(2)
            if current is None:
                logger.warning("Model selector not found after 10 retries")
                return False

            await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    if (['快捷', '思考型', 'Pro'].includes(t)) { b.click(); return; }
                }
            }""")
            await asyncio.sleep(1.5)

            selected = await page.evaluate("""() => {
                const item = document.querySelector('[data-test-id="bard-mode-option-思考型"]');
                if (item) { item.click(); return true; }
                const items = document.querySelectorAll('[role="menuitemradio"]');
                for (const i of items) {
                    if (i.textContent.includes('思考')) { i.click(); return true; }
                }
                return false;
            }""")
            await asyncio.sleep(1)
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

            if selected:
                logger.info("Switched to 思考型 model")
            return bool(selected)
        except Exception as e:
            logger.warning(f"Model switch error: {type(e).__name__}: {e}")
            return False

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-gemini-chat")
        return False
