"""Web Fetcher provider — 3-level web content retrieval (Firefox version).

v3: Level 3 (browser) migrated from Chrome CDP to Firefox + Cookie injection.
Level 1 (RSS) and Level 2 (HTTP) unchanged — no browser needed.
"""
import logging
import time
from typing import Optional
from xml.etree import ElementTree

import httpx

from providers.base import BaseProvider, JobResult

logger = logging.getLogger("ai-hub.web-fetcher")


class WebFetcherProvider(BaseProvider):
    name = "web_fetcher"
    category = "web"
    chrome_profile = None
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py (only needed for level=browser)

    async def execute(self, params: dict) -> JobResult:
        url = params.get("url", "")
        level = params.get("level", "auto")
        timeout = params.get("timeout", 30)

        if not url:
            return JobResult(False, "URL is required", provider=self.name)

        start = time.time()

        if level == "auto":
            if any(x in url.lower() for x in ["/rss", "/feed", "/atom", ".xml"]):
                level = "rss"
            else:
                level = "http"

        try:
            if level == "rss":
                result = await self._fetch_rss(url, timeout)
            elif level == "browser":
                result = await self._fetch_browser(url, timeout)
            else:
                result = await self._fetch_http(url, timeout)

            elapsed = time.time() - start

            if result:
                logger.info(f"Web fetch done: {len(result)} chars, {elapsed:.1f}s, level={level}")
                return JobResult(
                    success=True,
                    message=result,
                    generation_time=elapsed,
                    provider=self.name,
                )
            else:
                if level == "http":
                    logger.info("HTTP fetch returned empty, trying browser fallback")
                    result = await self._fetch_browser(url, timeout)
                    elapsed = time.time() - start
                    if result:
                        return JobResult(True, result, generation_time=elapsed, provider=self.name)

                return JobResult(False, "No content retrieved", provider=self.name)

        except Exception as e:
            logger.error(f"Web fetch error: {e}")
            return JobResult(False, str(e), provider=self.name)

    async def _fetch_rss(self, url: str, timeout: int) -> Optional[str]:
        """Level 1: Parse RSS/Atom feed."""
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "AI-Hub-Fetcher/1.0"})
            if resp.status_code != 200:
                return None

        root = ElementTree.fromstring(resp.text)

        items = []
        for item in root.iter("item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")
            items.append(f"## {title}\n{pub_date}\n{link}\n\n{desc}\n")

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary = entry.findtext("{http://www.w3.org/2005/Atom}summary", "")
            updated = entry.findtext("{http://www.w3.org/2005/Atom}updated", "")
            items.append(f"## {title}\n{updated}\n{link}\n\n{summary}\n")

        if not items:
            return None

        return f"# RSS Feed: {url}\n\n" + "\n---\n".join(items[:20])

    async def _fetch_http(self, url: str, timeout: int) -> Optional[str]:
        """Level 2: HTTP fetch with basic HTML-to-text conversion."""
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; AI-Hub-Fetcher/1.0)",
            })
            if resp.status_code != 200:
                return None

        html = resp.text

        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</(p|div|h[1-6]|li|tr)>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = text.strip()

        if len(text) > 50000:
            text = text[:50000] + "\n\n[... truncated]"

        return text if len(text) > 100 else None

    async def _fetch_browser(self, url: str, timeout: int) -> Optional[str]:
        """Level 3: Firefox browser rendering for JS-heavy pages."""
        if not self._firefox_mgr:
            logger.warning("FirefoxManager not available for browser fetch")
            return None

        page = None
        try:
            page = await self._firefox_mgr.get_page("firefox-gemini", url)
            await page.wait_for_timeout(2000)

            text = await page.evaluate("""() => {
                for (const el of document.querySelectorAll('script, style, nav, footer, header')) {
                    el.remove();
                }
                return document.body.innerText;
            }""")

            if len(text) > 50000:
                text = text[:50000] + "\n\n[... truncated]"

            return text if len(text) > 100 else None

        except Exception as e:
            logger.error(f"Browser fetch error: {e}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def health_check(self) -> bool:
        return True
