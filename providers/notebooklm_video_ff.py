"""NotebookLM Video Overview provider — Firefox + Cookie injection.

v3: Wraps NotebookLMProvider (Firefox version) to create Video Overview.
Phase 1 only: creates notebook, adds source, starts Video Overview generation.
Actual video download is handled by podcast_tracker.py (Phase 2).
"""
import logging
import time

from providers.base import BaseProvider, JobResult
from providers.notebooklm_ff import NotebookLMProvider

logger = logging.getLogger("ai-hub.notebooklm_video")


class NotebookLMVideoProvider(BaseProvider):
    name = "notebooklm_video"
    category = "video"
    chrome_profile = "firefox-notebooklm"  # Queue key for job serialization
    requires_chrome = False

    _firefox_mgr = None  # Set by main.py

    def __init__(self):
        self._nlm = NotebookLMProvider()

    async def execute(self, params: dict) -> JobResult:
        """Create a NotebookLM Video Overview from a text prompt."""
        prompt = params.get("prompt", "")
        if not prompt:
            return JobResult(False, "Prompt is required", provider=self.name)

        # Pass FirefoxManager to inner provider
        self._nlm._firefox_mgr = self._firefox_mgr

        start = time.time()

        result = await self._nlm.execute({
            "sources": [{"type": "text", "content": prompt}],
            "topic": "請用繁體中文",
        })

        if not result.success:
            return JobResult(
                False,
                f"NotebookLM failed: {result.message}",
                provider=self.name,
            )

        video_started = result.metadata.get("video_overview_started", False)
        elapsed = time.time() - start

        if video_started:
            return JobResult(
                success=True,
                message=f"Video Overview generation started ({elapsed:.0f}s). Ready in 5-15 min.",
                output_path=result.output_path,
                generation_time=elapsed,
                provider=self.name,
                metadata={
                    "notebook_url": result.output_path,
                    "video_overview_started": True,
                },
            )
        else:
            return JobResult(
                False,
                "Video Overview not started (daily limit reached or button not found)",
                provider=self.name,
            )

    async def health_check(self) -> bool:
        if self._firefox_mgr:
            return await self._firefox_mgr.is_ready("firefox-notebooklm")
        return False
