"""Vision router — POST /api/vision/analyze (Gemini browser relay)."""
import logging
from fastapi import APIRouter, HTTPException

from core.models import VisionRequest, VisionResponse
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager

logger = logging.getLogger("ai-hub.router.vision")
router = APIRouter(prefix="/api/vision", tags=["vision"])

job_queue: JobQueue = None
quota: QuotaTracker = None
chrome_mgr: ChromeManager = None
vision_provider = None


def init(q: JobQueue, qt: QuotaTracker, cm: ChromeManager, prov):
    global job_queue, quota, chrome_mgr, vision_provider
    job_queue = q
    quota = qt
    chrome_mgr = cm
    vision_provider = prov


@router.post("/analyze", response_model=VisionResponse)
async def analyze(request: VisionRequest):
    prov = vision_provider
    if not prov:
        raise HTTPException(503, "Vision provider not configured")

    if not quota.can_use(prov.name):
        raise HTTPException(429, "Vision quota exhausted")

    if prov.requires_chrome and prov.chrome_profile:
        ready = await chrome_mgr.ensure_running(prov.chrome_profile)
        if not ready:
            raise HTTPException(503, "Gemini Chrome not ready")

    try:
        result = await job_queue.submit(
            prov.name,
            lambda: prov.execute({
                "prompt": request.prompt,
                "image_base64": request.image_base64,
                "image_url": request.image_url,
            }),
            timeout=120,
            queue_key=getattr(prov, 'chrome_profile', None),
            queue_timeout=15,
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    if not result.success:
        raise HTTPException(500, result.message)

    quota.increment(prov.name)
    return VisionResponse(
        success=True,
        content=result.message,
        provider_used=result.provider,
        generation_time=result.generation_time,
        message="OK",
    )
