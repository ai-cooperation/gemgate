"""TTS router — POST /api/tts/generate (Google TTS only)"""
import logging
from fastapi import APIRouter, HTTPException

from core.models import TTSRequest, TTSResponse
from core.queue import JobQueue
from core.quota import QuotaTracker
from providers.google_tts import GoogleTTSProvider

logger = logging.getLogger("gemgate.router.tts")
router = APIRouter(prefix="/api/tts", tags=["tts"])

job_queue: JobQueue = None
quota: QuotaTracker = None
tts_providers = [GoogleTTSProvider()]


def init(q: JobQueue, qt: QuotaTracker):
    global job_queue, quota
    job_queue = q
    quota = qt


@router.post("/generate", response_model=TTSResponse)
async def generate_tts(request: TTSRequest):
    last_error = "No TTS provider available"
    for provider in tts_providers:
        if not quota.can_use(provider.name):
            continue
        try:
            result = await job_queue.submit(
                provider.name,
                lambda p=provider: p.execute({
                    "text": request.text,
                    "lang": request.lang,
                    "slow": getattr(request, "slow", False),
                }),
                timeout=60,
            )
        except Exception as e:
            last_error = str(e)
            continue
        if result.success:
            quota.increment(provider.name)
            return TTSResponse(success=True, audio_path=result.output_path,
                             audio_base64=result.output_base64, message=result.message)
        last_error = result.message
    raise HTTPException(500, f"TTS failed: {last_error}")
