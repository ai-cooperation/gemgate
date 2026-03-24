"""Google Flow Image provider — browser automation via Chrome CDP.

Uses Google Flow (labs.google/flow) with Nano Banana 2 model for free
image generation. Shares Chrome instance with Gemini (port 9222) to
leverage cached assets and Google login session.

Queue: firefox-gemini (shared with gemini_image for serial execution)
"""
import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from providers.base import BaseProvider, JobResult
from config import OUTPUT_DIRS

logger = logging.getLogger("ai-hub.flow_image")

FLOW_URL = "https://labs.google/fx/zh/tools/flow"


class FlowImageProvider(BaseProvider):
    name = "flow_image"
    category = "image"
    # Share queue with gemini_image — they use the same Chrome, run serially
    chrome_profile = "firefox-gemini"
    requires_chrome = True

    _firefox_mgr = None  # ChromeManager, set by main.py

    async def execute(self, params: dict) -> JobResult:
        prompt = params.get("prompt", "")
        timeout = params.get("timeout", 120)
        skip_base64 = params.get("skip_base64", False)

        if not prompt:
            return self._fail("Prompt is required")

        if not self._firefox_mgr:
            return self._fail("ChromeManager not initialized")

        start = time.time()
        file_id = str(uuid.uuid4())[:8]
        image_path = Path(OUTPUT_DIRS["images"]) / f"flow_{file_id}.png"

        page = None
        try:
            # Get Chrome instance (shared with Gemini on port 9222)
            inst = await self._firefox_mgr.get_instance(self.chrome_profile)
            context = inst.context

            # Open new tab for Flow (don't touch existing Gemini tabs)
            page = await context.new_page()

            # Navigate to Flow workspace
            try:
                await page.goto(FLOW_URL,
                              wait_until="domcontentloaded", timeout=30000)
            except Exception:
                # Slow page — wait for commit at least
                logger.warning("Flow slow load, continuing...")

            await asyncio.sleep(3)

            # Check login
            if "accounts.google" in page.url:
                logger.info("Flow login needed, auto-login...")
                ok = await self._firefox_mgr._auto_login(
                    self.chrome_profile, page
                )
                if ok:
                    try:
                        await page.goto(FLOW_URL,
                                      wait_until="domcontentloaded",
                                      timeout=30000)
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                else:
                    return self._fail("Flow login failed")

            # Wait for workspace to load
            for _ in range(15):
                body = await page.inner_text("body")
                if "新建项目" in body:
                    break
                await asyncio.sleep(1)
            else:
                return self._fail("Flow workspace not loaded")

            # Create new project
            new_btn = await page.query_selector('button:has-text("新建项目")')
            if not new_btn:
                return self._fail("Cannot find 新建项目 button")

            await new_btn.click()
            await asyncio.sleep(4)

            if "/project/" not in page.url:
                return self._fail(f"Project creation failed: {page.url[:80]}")

            logger.info(f"Flow project: {page.url}")

            # Find prompt textbox
            prompt_box = await page.query_selector(
                'div[role="textbox"][contenteditable="true"]'
            )
            if not prompt_box:
                return self._fail("Cannot find prompt textbox")

            # Type prompt
            await prompt_box.click()
            await asyncio.sleep(0.3)
            await page.keyboard.type(prompt)
            await asyncio.sleep(0.5)

            # Click submit
            submit = await self._find_submit_button(page)
            if not submit:
                return self._fail("Cannot find submit button")

            await submit.click()
            logger.info(f"Flow prompt sent: {prompt[:60]}...")
            await asyncio.sleep(3)

            # Wait for generation
            remaining = max(timeout - (time.time() - start), 30)
            image_found = await self._wait_for_image(page, remaining)
            if not image_found:
                return self._fail(f"No image generated in {timeout}s")

            # Extract image
            dl_ok = await self._download_image(page, image_path)
            if not dl_ok:
                return self._fail("Image download failed")

            elapsed = round(time.time() - start, 1)
            file_size = image_path.stat().st_size
            logger.info(
                f"Flow image: {image_path} ({file_size:,} bytes, {elapsed}s)"
            )

            img_b64 = None
            if not skip_base64:
                img_b64 = base64.b64encode(image_path.read_bytes()).decode()

            return JobResult(
                success=True,
                message=f"Flow image in {elapsed}s ({file_size:,} bytes)",
                output_path=str(image_path),
                output_base64=img_b64,
                generation_time=elapsed,
                provider=self.name,
            )

        except Exception as e:
            logger.error(f"Flow error: {type(e).__name__}: {e}")
            if page:
                try:
                    err_path = (
                        Path(OUTPUT_DIRS["images"]) / f"flow_error_{file_id}.png"
                    )
                    await page.screenshot(path=str(err_path))
                except Exception:
                    pass
            return self._fail(str(e))

        finally:
            # Always close the Flow tab to not interfere with Gemini
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _find_submit_button(self, page) -> Optional[object]:
        """Find the arrow_forward submit button at bottom."""
        for b in await page.query_selector_all("button"):
            try:
                text = (await b.inner_text()).strip()
                box = await b.bounding_box()
                if "arrow_forward" in text and box and box["y"] > 500:
                    return b
            except Exception:
                continue
        return None

    async def _wait_for_image(self, page, remaining: float) -> bool:
        """Poll until generated image appears."""
        deadline = time.time() + remaining

        for i in range(int(remaining / 2)):
            if time.time() > deadline:
                break
            await asyncio.sleep(2)

            try:
                status = await page.evaluate("""() => {
                    const body = document.body.innerText || '';
                    const pcts = (body.match(/(\\d+)%/g) || []).map(p => parseInt(p));
                    let loaded = 0;
                    for (const img of document.querySelectorAll('img')) {
                        const r = img.getBoundingClientRect();
                        if (r.width > 150 && r.height > 150 &&
                            r.y > 50 && r.y < 650 &&
                            img.complete && img.naturalWidth > 0)
                            loaded++;
                    }
                    return { pcts, loaded };
                }""")

                loaded = status.get("loaded", 0)
                pcts = status.get("pcts", [])

                if i % 5 == 0:
                    logger.info(f"Flow progress: pcts={pcts} loaded={loaded}")

                if loaded > 0:
                    logger.info(f"Flow image loaded ({loaded} images)")
                    await asyncio.sleep(1)
                    return True

            except Exception as e:
                err_str = str(e).lower()
                if "closed" in err_str or "target" in err_str:
                    logger.warning(f"Flow page lost: {e}")
                    return False
                continue

        return False

    async def _download_image(self, page, image_path: Path) -> bool:
        """Extract first generated image from page."""
        try:
            img_result = await page.evaluate("""() => {
                for (const img of document.querySelectorAll('img')) {
                    const r = img.getBoundingClientRect();
                    if (r.width > 150 && r.height > 150 &&
                        r.y > 50 && r.y < 650 &&
                        img.complete && img.naturalWidth > 0) {
                        try {
                            const c = document.createElement('canvas');
                            c.width = img.naturalWidth;
                            c.height = img.naturalHeight;
                            c.getContext('2d').drawImage(img, 0, 0);
                            return {
                                type: 'b64',
                                data: c.toDataURL('image/png').split(',')[1]
                            };
                        } catch(e) {
                            return {type: 'url', data: img.src};
                        }
                    }
                }
                return null;
            }""")

            if not img_result:
                logger.error("No image element found")
                return False

            if img_result["type"] == "b64":
                image_path.write_bytes(base64.b64decode(img_result["data"]))
                return True
            else:
                url = img_result["data"]
                logger.info(f"Downloading from: {url[:80]}")
                resp = await page.request.get(url)
                data = await resp.body()
                image_path.write_bytes(data)
                return len(data) > 1000

        except Exception as e:
            logger.error(f"Flow download error: {e}")
            return False

    async def health_check(self) -> bool:
        if not self._firefox_mgr:
            return False
        return await self._firefox_mgr.is_ready(self.chrome_profile)

    def _fail(self, message: str) -> JobResult:
        logger.error(f"Flow: {message}")
        return JobResult(
            success=False,
            message=message,
            provider=self.name,
        )

    @staticmethod
    def is_flow_exhausted() -> bool:
        """Check daily exhaustion flag."""
        flag = Path(OUTPUT_DIRS["images"]).parent / "state" / "flow_exhausted.flag"
        if not flag.exists():
            return False
        import datetime
        mtime = datetime.datetime.fromtimestamp(flag.stat().st_mtime)
        if mtime.date() < datetime.date.today():
            flag.unlink(missing_ok=True)
            return False
        return True
