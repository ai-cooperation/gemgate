"""Firefox instance lifecycle manager — Persistent Context mode.

Uses launch_persistent_context so that Google login sessions persist
across restarts. No cookie injection needed — user logs in once via
the headed Firefox window, and the session data (cookies, localStorage,
IndexedDB) is saved to a dedicated profile directory per queue key.
"""
import asyncio
import os
import logging
import time
from pathlib import Path

logger = logging.getLogger("ai-hub.firefox")

PROFILE_BASE = Path("/opt/gemgate/state/firefox-profiles")


class FirefoxInstance:
    """Holds a Playwright Firefox persistent context."""

    def __init__(self, context, queue_key: str):
        self.context = context
        self.queue_key = queue_key
        self.last_used = time.time()
        self._keep_alive_until = 0

    @property
    def browser(self):
        return self.context.browser

    def is_connected(self) -> bool:
        try:
            b = self.context.browser
            return b is not None and b.is_connected()
        except Exception:
            return False


class FirefoxManager:
    """Manages Firefox instances with persistent profiles."""

    def __init__(self, idle_timeout: int = 1200):
        self._instances: dict[str, FirefoxInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pw = None
        self._idle_timeout = idle_timeout
        self._idle_tasks: dict[str, asyncio.Task] = {}

    def _get_lock(self, queue_key: str) -> asyncio.Lock:
        if queue_key not in self._locks:
            self._locks[queue_key] = asyncio.Lock()
        return self._locks[queue_key]

    async def _ensure_playwright(self):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        return self._pw

    async def get_instance(self, queue_key: str) -> FirefoxInstance:
        """Get or create a Firefox persistent context for the given queue key."""
        inst = self._instances.get(queue_key)
        if inst and inst.is_connected():
            inst.last_used = time.time()
            self._reset_idle_timer(queue_key)
            return inst

        profile_dir = PROFILE_BASE / queue_key
        profile_dir.mkdir(parents=True, exist_ok=True)

        pw = await self._ensure_playwright()
        os.environ.setdefault("DISPLAY", ":0")

        context = await pw.firefox.launch_persistent_context(
            str(profile_dir),
            headless=os.environ.get("GEMGATE_HEADLESS", "true").lower() != "false",
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )

        # Inject Google cookies from desktop Firefox snap
        cookies = self.load_google_cookies()
        if cookies:
            await context.add_cookies(cookies)
            logger.info(f"Injected {len(cookies)} Google cookies into {queue_key}")

        inst = FirefoxInstance(context, queue_key)
        self._instances[queue_key] = inst
        self._reset_idle_timer(queue_key)
        logger.info(f"Firefox persistent context created: {queue_key} (profile: {profile_dir})")
        return inst

    async def get_page(self, queue_key: str, url: str) -> "Page":
        """Get a fresh page navigated to the given URL."""
        inst = await self.get_instance(queue_key)
        page = await inst.context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"[{queue_key}] Navigation failed: {e}, retrying...")
            await page.close()
            page = await inst.context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        await asyncio.sleep(3)

        if "accounts.google.com" in page.url:
            logger.warning(f"[{queue_key}] Redirected to Google login, re-injecting cookies...")
            cookies = self.load_google_cookies()
            if cookies:
                await page.context.add_cookies(cookies)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

        return page


    async def get_or_reuse_page(self, queue_key: str, url: str) -> "Page":
        """Reuse first existing page (navigate to url) or create new one.

        Unlike get_page() which always creates a new tab, this reuses the
        existing tab to avoid Gemini's slow JS re-init on fresh tabs.
        """
        inst = await self.get_instance(queue_key)
        pages = inst.context.pages

        if pages:
            page = pages[0]
            # Close extra tabs (keep only one)
            for extra in pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"[{queue_key}] Reuse navigation failed: {e}, creating new page")
                try:
                    await page.close()
                except Exception:
                    pass
                page = await inst.context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        else:
            page = await inst.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"[{queue_key}] Navigation failed: {e}, retrying...")
                await page.close()
                page = await inst.context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        await asyncio.sleep(3)

        if "accounts.google.com" in page.url:
            logger.warning(f"[{queue_key}] Redirected to Google login, re-injecting cookies...")
            cookies = self.load_google_cookies()
            if cookies:
                await page.context.add_cookies(cookies)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

        return page

    # Cookie injection fallback (for other providers that may need it)
    def load_google_cookies(self) -> list[dict]:
        """Read Google cookies from Firefox snap's cookies.sqlite."""
        import shutil, sqlite3, tempfile
        db = Path("/nonexistent")
        if not db.exists():
            return []
        tmp = tempfile.mktemp(suffix=".sqlite")
        shutil.copy2(str(db), tmp)
        try:
            conn = sqlite3.connect(tmp)
            rows = conn.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies WHERE host LIKE '%google%'"
            ).fetchall()
            conn.close()
        finally:
            Path(tmp).unlink(missing_ok=True)
        now = int(time.time())
        cookies = []
        for name, value, host, path, expiry, is_secure, is_http_only, same_site in rows:
            exp_sec = expiry
            if exp_sec > 1e12:
                exp_sec = int(exp_sec / 1000)
            if exp_sec > 0 and exp_sec < now:
                continue
            cookie = {"name": name, "value": value, "domain": host, "path": path,
                      "secure": bool(is_secure), "httpOnly": bool(is_http_only)}
            if exp_sec > 0:
                cookie["expires"] = exp_sec
            sm = {0: "None", 1: "Lax", 2: "Strict"}
            if same_site in sm:
                cookie["sameSite"] = sm[same_site]
            cookies.append(cookie)
        logger.info(f"Loaded {len(cookies)} Google cookies from Firefox")
        return cookies

    def keep_alive(self, queue_key: str, seconds: int = 2700):
        inst = self._instances.get(queue_key)
        if inst:
            inst._keep_alive_until = time.time() + seconds
            logger.info(f"[{queue_key}] Keep-alive set for {seconds}s")

    def _reset_idle_timer(self, queue_key: str):
        if queue_key in self._idle_tasks:
            self._idle_tasks[queue_key].cancel()
        self._idle_tasks[queue_key] = asyncio.create_task(
            self._idle_shutdown(queue_key)
        )

    async def _idle_shutdown(self, queue_key: str):
        await asyncio.sleep(self._idle_timeout)
        inst = self._instances.get(queue_key)
        if not inst:
            return
        if inst._keep_alive_until > time.time():
            remaining = int(inst._keep_alive_until - time.time())
            logger.info(f"[{queue_key}] Keep-alive active, {remaining}s remaining")
            self._idle_tasks[queue_key] = asyncio.create_task(
                self._idle_shutdown(queue_key)
            )
            return
        elapsed = time.time() - inst.last_used
        if elapsed < self._idle_timeout:
            self._idle_tasks[queue_key] = asyncio.create_task(
                self._idle_shutdown(queue_key)
            )
            return
        logger.info(f"Shutting down idle Firefox: {queue_key}")
        await self.close_instance(queue_key)

    async def is_ready(self, queue_key: str) -> bool:
        inst = self._instances.get(queue_key)
        return bool(inst and inst.is_connected())

    async def close_instance(self, queue_key: str):
        inst = self._instances.pop(queue_key, None)
        if inst:
            try:
                await inst.context.close()
            except Exception:
                pass

    async def close_all(self):
        for key in list(self._instances.keys()):
            await self.close_instance(key)
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def get_all_status(self) -> dict:
        result = {}
        for key in ["firefox-gemini", "firefox-gemini-chat", "firefox-gemini-audio", "firefox-notebooklm"]:
            inst = self._instances.get(key)
            result[key] = {
                "ready": bool(inst and inst.is_connected()),
                "last_used": int(inst.last_used) if inst else 0,
                "pages": len(inst.context.pages) if inst else 0,
            }
        return result
