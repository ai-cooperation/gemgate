"""Gemini Audio (music) provider — browser automation via Firefox.

Uses Gemini Pro's music generation feature (建立音樂 / Create music chip).
Same Firefox instance as gemini_image/video (firefox-gemini queue key).
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

logger = logging.getLogger("ai-hub.gemini_audio")

GEMINI_URL = "https://gemini.google.com/app"

# Input selectors (same as image/video providers)
INPUT_SELECTORS = [
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
    '.ql-editor.textarea',
]

# Send button selectors
SEND_SELECTORS = [
    'button:has([data-mat-icon-name="send"])',
    'button[aria-label*="傳送"]',
    'button[aria-label*="Send"]',
]

# "Create music" chip selectors (EN and zh-TW)
MUSIC_CHIP_SELECTORS = [
    'button[aria-label*="創作音樂"]',
    'button[aria-label*="建立音樂"]',
    'button[aria-label*="Create music"]',
    'button[aria-label*="Make music"]',
]


class GeminiAudioProvider(BaseProvider):
    name = "gemini_audio"
    category = "audio"
    chrome_profile = "firefox-gemini-audio"  # Shares queue with image/video
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py

    async def execute(self, params: dict) -> JobResult:
        prompt = params.get("prompt", "")
        timeout = params.get("timeout", 120)

        if not prompt:
            return JobResult(False, "Prompt is required", provider=self.name)

        if not self._firefox_mgr:
            return JobResult(False, "FirefoxManager not initialized", provider=self.name)

        start = time.time()
        file_id = str(uuid.uuid4())[:8]
        audio_path = Path(OUTPUT_DIRS["audio"]) / f"gemini_music_{file_id}.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)

        page = None
        try:
            # --- Phase 1: Get authenticated Gemini page ---
            page = await self._firefox_mgr.get_or_reuse_page("firefox-gemini-audio", GEMINI_URL)
            await self._dismiss_overlays(page)

            # Login check
            try:
                page_text = await page.inner_text('body')
                if 'Sign in to connect' in page_text:
                    return JobResult(False, 'Not logged in. Cookies expired.', provider=self.name)
            except Exception:
                pass

            # --- Phase 2: Select model (Pro/thinking) ---
            model_ok = await self._ensure_thinking_model(page)
            if not model_ok:
                logger.warning("Cannot switch to Pro model, using current")

            # --- Phase 3: Activate music mode ---
            chip_ok = await self._activate_music_mode(page)
            if not chip_ok:
                logger.warning("Music chip unavailable, proceeding with text prompt")

            # Collect existing audio srcs before sending
            existing_audio_srcs = await self._collect_audio_srcs(page)
            logger.info(f"Existing audio elements before prompt: {len(existing_audio_srcs)}")

            # --- Phase 4: Type prompt and send ---
            input_ok = await self._type_prompt(page, prompt)
            if not input_ok:
                return JobResult(False, "Cannot find input field", provider=self.name)

            await self._click_send(page)
            logger.info(f"Audio prompt sent: {prompt[:80]}...")

            # --- Phase 5: Wait for new audio element ---
            new_audio_src = await self._wait_for_audio(page, existing_audio_srcs, timeout)
            if not new_audio_src:
                # Try screenshot for debug
                try:
                    await page.screenshot(
                        path="/opt/gemgate/state/screenshots/audio_debug_timeout.png"
                    )
                except Exception:
                    pass
                return JobResult(False, f"No audio generated after {timeout}s", provider=self.name)

            # --- Phase 6: Download audio ---
            dl_ok = await self._download_audio(page, new_audio_src, str(audio_path))
            if not dl_ok:
                return JobResult(False, "Audio download failed", provider=self.name)

            elapsed = time.time() - start

            # Read base64
            audio_b64 = None
            try:
                with open(str(audio_path), "rb") as f:
                    audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                pass

            return JobResult(
                success=True,
                message=f"Audio generated in {elapsed:.1f}s",
                output_path=str(audio_path),
                output_base64=audio_b64,
                generation_time=elapsed,
                provider=self.name,
            )

        except Exception as e:
            logger.error(f"Gemini audio error: {type(e).__name__}: {e}")
            return JobResult(False, str(e), provider=self.name)

    # ── Model selector (same as video provider) ──

    async def _ensure_thinking_model(self, page) -> bool:
        """Ensure Gemini is on Pro/thinking model."""
        try:
            current = None
            for attempt in range(10):
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
                await asyncio.sleep(2)

            if current is None:
                logger.warning("Model selector not found after retries")
                return False

            if current in ('思考型', 'Deep Think', 'Pro', 'PRO'):
                logger.info(f"Already on Pro model (current={current})")
                return True

            # Click model selector
            await page.evaluate("""() => {
                const MODEL_NAMES = ['快捷', '思考型', 'Flash', 'Deep Think', '深度思考'];
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (MODEL_NAMES.includes(b.textContent.trim())) { b.click(); return; }
                }
            }""")
            await asyncio.sleep(1.5)

            # Select Pro/thinking from dropdown
            selected = await page.evaluate("""() => {
                const items = document.querySelectorAll('[role="menuitemradio"]');
                for (const i of items) {
                    const t = i.textContent.trim();
                    if (t.startsWith('思考') || t.includes('Deep Think') ||
                        t.startsWith('Pro') || t.startsWith('PRO')) {
                        i.click(); return true;
                    }
                }
                return false;
            }""")
            await asyncio.sleep(1)
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
            return bool(selected)
        except Exception as e:
            logger.warning(f"Model switch error: {type(e).__name__}: {e}")
            return False

    # ── Music mode activation ──

    async def _activate_music_mode(self, page) -> bool:
        """Click the music chip (建立音樂 / Create music)."""
        for chip_sel in MUSIC_CHIP_SELECTORS:
            try:
                btn = await page.wait_for_selector(chip_sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(2)
                    logger.info(f"Music mode activated: {chip_sel}")
                    return True
            except Exception:
                continue

        # JS fallback
        clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const t = b.textContent.trim();
                if (t.includes('創作音樂') || t.includes('建立音樂') || t.includes('Create music')) {
                    b.click(); return true;
                }
            }
            const chips = document.querySelectorAll('[role="button"], [class*="chip"]');
            for (const c of chips) {
                const t = c.textContent.trim();
                if (t.includes('創作音樂') || t.includes('建立音樂') || t.includes('Create music')) {
                    c.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(2)
            logger.info("Music mode activated via JS fallback")
        else:
            logger.warning("Music chip not found in DOM")
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

    # ── Audio detection + download ──

    async def _collect_audio_srcs(self, page) -> set:
        """Collect ALL existing media URLs on page (audio + googleusercontent + blob)."""
        srcs = set()
        try:
            result = await page.evaluate("""() => {
                const urls = new Set();
                // <audio> elements
                document.querySelectorAll('audio').forEach(a => {
                    if (a.src) urls.add(a.src);
                    a.querySelectorAll('source').forEach(s => { if (s.src) urls.add(s.src); });
                });
                // All googleusercontent URLs (to exclude pre-existing ones)
                document.querySelectorAll('[src*="googleusercontent"]').forEach(el => {
                    if (el.src) urls.add(el.src);
                });
                // All blob: URLs
                document.querySelectorAll('[src^="blob:"]').forEach(el => {
                    if (el.src) urls.add(el.src);
                });
                // Download links
                document.querySelectorAll('a[download]').forEach(l => {
                    if (l.href) urls.add(l.href);
                });
                return [...urls];
            }""")
            srcs = set(result or [])
        except Exception:
            pass
        return srcs

    async def _wait_for_audio(self, page, existing_srcs: set, timeout: int) -> Optional[str]:
        """Wait for Gemini to generate music, click play to load audio, then extract URL.

        Gemini's music player lazy-loads audio: play buttons appear in DOM but no <audio>
        elements until you actually click play. Strategy:
        1. Count initial play buttons (from previous conversations)
        2. Wait for generation to complete (stop button gone + new play buttons appear)
        3. Click a new play button to trigger audio loading
        4. Wait for <audio> element or blob URL to appear
        5. Return the audio URL for download
        """
        waited = 0
        generation_started = False
        generation_done = False
        played = False

        while waited < timeout:
            await asyncio.sleep(3)
            waited += 3

            # Check for login redirect or refusal
            if waited in (15, 30):
                try:
                    resp_text = await page.inner_text('body')
                    if 'Sign in to connect' in resp_text:
                        return None
                    if "couldn't do that" in resp_text.lower() or 'try again later' in resp_text.lower():
                        logger.error(f'Gemini refused: {resp_text[:200]}')
                        return None
                except Exception:
                    pass

            # Track stop button for generation state
            stop_visible = await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const label = b.getAttribute('aria-label') || '';
                    if (label.includes('停止') || label.includes('Stop')) return true;
                }
                return false;
            }""")

            if stop_visible:
                generation_started = True
            elif generation_started and not generation_done:
                generation_done = True
                logger.info(f"Generation completed at {waited}s")
                await asyncio.sleep(3)
                waited += 3

            # Count play buttons in current DOM
            play_count = await self._count_play_buttons(page)

            if waited % 15 == 0:
                status = "generating" if stop_visible else "scanning"
                logger.info(f"Audio gen: {waited}s/{timeout}s [{status}] plays={play_count}")

            if waited in (30, 60, 90):
                try:
                    await page.screenshot(
                        path=f"/opt/gemgate/state/screenshots/audio_debug_{waited}s.png"
                    )
                except Exception:
                    pass

            # After generation completes and play buttons exist, click one to load audio
            if play_count > 0 and (generation_done or waited >= 60) and not played:
                played = True
                logger.info(f"Found {play_count} music clips after generation, clicking first play...")

                audio_url = await self._click_play_and_extract(page, 0)
                if audio_url:
                    return audio_url
                else:
                    logger.warning("Failed to extract audio after clicking play")
                    try:
                        await page.screenshot(
                            path="/opt/gemgate/state/screenshots/audio_debug_after_play.png"
                        )
                    except Exception:
                        pass

            # Also check for direct <audio>/<video> elements
            audio_src = await page.evaluate("""() => {
                const audios = document.querySelectorAll('audio');
                for (const a of audios) {
                    if (a.src) return a.src;
                    const source = a.querySelector('source');
                    if (source && source.src) return source.src;
                }
                // Gemini puts audio in <video> tags
                const videos = document.querySelectorAll('video');
                for (const v of videos) {
                    if (v.src && v.src.includes('usercontent.google')) return v.src;
                }
                return null;
            }""")
            if audio_src and audio_src not in existing_srcs:
                logger.info(f"Direct audio element found: {audio_src[:80]}...")
                return audio_src

        return None

    async def _count_play_buttons(self, page) -> int:
        """Count play buttons (播放或暫停音訊預覽) in the page."""
        try:
            return await page.evaluate("""() => {
                return document.querySelectorAll('button[aria-label*="播放"], button[aria-label*="Play"]')
                    .length;
            }""")
        except Exception:
            return 0

    async def _click_play_and_extract(self, page, skip_count: int) -> Optional[str]:
        """Click the first new play button and extract the resulting audio URL.

        Args:
            page: Playwright page
            skip_count: Number of initial play buttons to skip (from previous conversations)
        """
        try:
            # Set up network request interception to catch audio URLs
            audio_urls = []

            async def handle_response(response):
                ct = response.headers.get('content-type', '')
                url = response.url
                if 'audio' in ct or '.mp3' in url or '.wav' in url or '.m4a' in url or 'audio' in url:
                    audio_urls.append(url)
                    logger.info(f"Intercepted audio response: {url[:100]} (type={ct})")

            page.on('response', handle_response)

            # Click the first NEW play button (skip existing ones)
            clicked = await page.evaluate("""(skipCount) => {
                const btns = document.querySelectorAll('button[aria-label*="播放"], button[aria-label*="Play"]');
                if (btns.length > skipCount) {
                    btns[skipCount].click();
                    return true;
                }
                return false;
            }""", skip_count)

            if not clicked:
                logger.warning("Could not click new play button")
                page.remove_listener('response', handle_response)
                return None

            logger.info("Clicked play button, waiting for audio to load...")

            # Wait for audio to appear (up to 15 seconds)
            for _ in range(30):
                await asyncio.sleep(0.5)

                # Check intercepted URLs
                if audio_urls:
                    page.remove_listener('response', handle_response)
                    url = audio_urls[0]
                    logger.info(f"Got audio URL from network: {url[:100]}")
                    return url

                # Check for <audio>/<video> elements with media URLs
                audio_src = await page.evaluate("""() => {
                    // Standard <audio> elements
                    const audios = document.querySelectorAll('audio');
                    for (const a of audios) {
                        if (a.src) return a.src;
                        const source = a.querySelector('source');
                        if (source && source.src) return source.src;
                    }
                    // Gemini puts audio in <video> tags with usercontent URLs
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        if (v.src && v.src.includes('usercontent.google')) return v.src;
                    }
                    // Blob URLs (non-video, non-image)
                    const blobs = document.querySelectorAll('[src^="blob:"]');
                    for (const b of blobs) {
                        if (b.tagName !== 'IMG') return b.src;
                    }
                    return null;
                }""")
                if audio_src:
                    page.remove_listener('response', handle_response)
                    logger.info(f"Found audio element after play: {audio_src[:80]}")
                    return audio_src

            page.remove_listener('response', handle_response)
            logger.warning("No audio URL found after clicking play (15s)")

            # Last resort: dump DOM info for debugging
            debug = await page.evaluate("""() => {
                const info = [];
                document.querySelectorAll('audio, video, [src^="blob:"], [class*="audio"], [class*="player"]').forEach(el => {
                    info.push(el.tagName + ' class=' + (el.className||'').substring(0,50) + ' src=' + (el.src||'').substring(0,80));
                });
                return info;
            }""")
            logger.info(f"Post-play DOM debug: {debug}")

            return None
        except Exception as e:
            logger.error(f"Click play error: {type(e).__name__}: {e}")
            return None


    async def _download_audio(self, page, audio_src: str, save_path: str) -> bool:
        """Download audio file from src URL."""
        try:
            # Method 1: context.request (carries cookies)
            try:
                logger.info(f"Downloading audio from: {audio_src[:100]}")
                response = await page.context.request.get(audio_src)
                ct = response.headers.get('content-type') or ''
                logger.info(f"Download response: status={response.status}, content-type={ct}")
                if response.ok and 'text/html' not in ct:
                    body = await response.body()
                    with open(save_path, "wb") as f:
                        f.write(body)
                    size = Path(save_path).stat().st_size
                    if size > 1000:
                        logger.info(f"Audio downloaded (context.request): {size} bytes, type={ct}")
                        return True
                    else:
                        logger.warning(f"Downloaded file too small: {size} bytes")
                elif not response.ok:
                    logger.warning(f"Download HTTP {response.status}")
                else:
                    logger.warning(f"Got HTML response, not audio")
            except Exception as e:
                logger.warning(f"context.request failed: {type(e).__name__}: {e}")

            # Method 2: In-page fetch (handles blob: URLs)
            try:
                b64_data = await page.evaluate("""
                    async (url) => {
                        try {
                            const resp = await fetch(url);
                            if (!resp.ok) return null;
                            const blob = await resp.blob();
                            return new Promise((resolve) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                reader.readAsDataURL(blob);
                            });
                        } catch(e) { return null; }
                    }
                """, audio_src)
                if b64_data:
                    with open(save_path, "wb") as f:
                        f.write(base64.b64decode(b64_data))
                    size = Path(save_path).stat().st_size
                    if size > 1000:
                        logger.info(f"Audio downloaded (page fetch): {size} bytes")
                        return True
            except Exception as e:
                logger.warning(f"Page fetch failed: {type(e).__name__}: {e}")

            # Method 3: expect_download
            try:
                async with page.expect_download(timeout=30000) as dl_info:
                    await page.evaluate("""(url) => {
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = 'audio.mp3';
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""", audio_src)
                download = await dl_info.value
                await download.save_as(save_path)
                size = Path(save_path).stat().st_size
                if size > 1000:
                    logger.info(f"Audio downloaded (expect_download): {size} bytes")
                    return True
            except Exception as e:
                logger.warning(f"expect_download failed: {type(e).__name__}: {e}")

            logger.error("All download methods failed")
            return False
        except Exception as e:
            logger.error(f"Audio download error: {type(e).__name__}: {e}")
            return False

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-gemini-audio")
        return False
