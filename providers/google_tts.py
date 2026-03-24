"""Google TTS provider using gTTS library. No browser needed."""
import base64
import logging
import time
import uuid
from pathlib import Path

from gtts import gTTS

from providers.base import BaseProvider, JobResult
from config import OUTPUT_DIRS

logger = logging.getLogger("ai-hub.tts")


class GoogleTTSProvider(BaseProvider):
    name = "google_tts"
    category = "tts"
    chrome_profile = None
    requires_chrome = False

    async def execute(self, params: dict) -> JobResult:
        text = params.get("text", "")
        lang = params.get("lang", "zh-TW")
        slow = params.get("slow", False)

        if not text:
            return JobResult(False, "Text is required", provider=self.name)

        if len(text) > 5000:
            return JobResult(False, "Text too long (max 5000 chars)", provider=self.name)

        start = time.time()
        file_id = str(uuid.uuid4())[:8]
        output_path = Path(OUTPUT_DIRS["audio"]) / f"tts_{file_id}.mp3"

        try:
            tts = gTTS(text=text, lang=lang, slow=slow)
            tts.save(str(output_path))

            with open(output_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")

            elapsed = time.time() - start
            logger.info(f"TTS done: {len(text)} chars, {lang}, {elapsed:.1f}s")

            return JobResult(
                success=True,
                message=f"TTS 完成，{len(text)} 字，耗時 {elapsed:.1f}s",
                output_path=str(output_path),
                output_base64=audio_b64,
                generation_time=elapsed,
                provider=self.name,
            )
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return JobResult(False, str(e), provider=self.name)

    async def health_check(self) -> bool:
        return True  # gTTS is a library, always available
