"""Audio (music) router — POST /api/audio/generate
Uses Gemini Pro's music generation feature.
"""
import logging
from fastapi import APIRouter, HTTPException

from core.models import AudioRequest, AudioResponse
from core.queue import JobQueue
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager

logger = logging.getLogger("ai-hub.router.audio")
router = APIRouter(prefix="/api/audio", tags=["audio"])

job_queue: JobQueue = None
quota: QuotaTracker = None
chrome_mgr: ChromeManager = None
providers: dict = {}


def init(q: JobQueue, qt: QuotaTracker, cm: ChromeManager, prov: dict):
    global job_queue, quota, chrome_mgr, providers
    job_queue = q
    quota = qt
    chrome_mgr = cm
    providers = prov


@router.post("/generate", response_model=AudioResponse)
async def generate_audio(request: AudioRequest):
    chain = ["gemini_audio"]
    if request.provider != "auto":
        name = request.provider + "_audio" if "_" not in request.provider else request.provider
        chain = [name]

    last_error = "No audio provider available"
    for provider_name in chain:
        prov = providers.get(provider_name)
        if not prov:
            continue
        if not quota.can_use(provider_name):
            last_error = f"{provider_name}: quota exhausted"
            continue

        try:
            result = await job_queue.submit(
                provider_name,
                lambda p=prov: p.execute({
                    "prompt": request.prompt,
                    "timeout": request.timeout,
                }),
                timeout=request.timeout + 30,
                queue_key=getattr(prov, "chrome_profile", None),
            )
        except Exception as e:
            logger.warning(f"{provider_name} failed: {e}")
            last_error = str(e)
            continue

        if result.success:
            quota.increment(provider_name)
            return AudioResponse(
                success=True,
                audio_path=result.output_path,
                audio_base64=result.output_base64,
                provider_used=result.provider,
                generation_time=result.generation_time,
                message=result.message,
            )
        else:
            last_error = result.message
            continue

    raise HTTPException(503, f"Audio generation failed: {last_error}")
