"""Image router — POST /api/image/generate with fallback support.

Model selection:
  - model="auto" (default): always use Pro (no silent fallback to fast)
  - model="pro": same as auto
  - model="fast": force fast (unlimited, no CJK)
  - model="flow": use Google Flow (Nano Banana 2, free, separate Chrome)

Pro usage tracked via "gemini_image_pro" quota key.
"""
import logging
from fastapi import APIRouter, HTTPException

from core.models import ImageRequest, ImageResponse
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager
from config import FALLBACK_CHAINS

logger = logging.getLogger("ai-hub.router.image")
router = APIRouter(prefix="/api/image", tags=["image"])

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


@router.post("/generate", response_model=ImageResponse)
async def generate_image(request: ImageRequest):
    actual_model = request.model

    # --- model="flow": route directly to Flow provider ---
    if actual_model == "flow":
        chain = ["flow_image"]
    else:
        # Resolve Pro/fast model
        if actual_model in ("auto", "pro"):
            actual_model = "pro"
            pro_available = quota.can_use("gemini_image_pro")
            if not pro_available:
                raise HTTPException(
                    503,
                    f"Pro quota exhausted ({quota.get_used('gemini_image_pro')}/day). "
                    "Use model=fast or model=flow.",
                )
            from providers.gemini_image_ff import GeminiImageProvider
            if GeminiImageProvider.is_pro_exhausted():
                raise HTTPException(
                    503,
                    "Pro quota exhausted (vision verify detected CJK garble). "
                    "Use model=flow for free generation.",
                )

        # Build provider chain
        if request.provider == "auto":
            chain = FALLBACK_CHAINS.get("image", [])
        else:
            name = (
                request.provider + "_image"
                if "_" not in request.provider
                else request.provider
            )
            chain = [name] + [
                p for p in FALLBACK_CHAINS.get("image", []) if p != name
            ]

    last_error = ""
    for provider_name in chain:
        prov = providers.get(provider_name)
        if not prov:
            continue

        # Check quota
        if not quota.can_use(provider_name):
            last_error = f"{provider_name}: quota exhausted"
            logger.info(last_error)
            continue

        # Ensure Chrome is running
        if prov.requires_chrome and prov.chrome_profile:
            ready = await chrome_mgr.ensure_running(prov.chrome_profile)
            if not ready:
                last_error = f"{provider_name}: Chrome not ready"
                logger.warning(last_error)
                continue

        # Execute
        try:
            result = await job_queue.submit(
                provider_name,
                lambda p=prov, m=actual_model: p.execute({
                    "prompt": request.prompt,
                    "timeout": request.timeout,
                    "model": m,
                    "skip_base64": False,
                }),
                timeout=request.timeout + 30,
                queue_key=getattr(prov, "chrome_profile", None),
                queue_timeout=request.queue_timeout or (
                    None if getattr(prov, "chrome_profile", None) else None
                ),
            )
            if result.success:
                quota.increment(provider_name)
                if actual_model == "pro" and provider_name != "flow_image":
                    quota.increment("gemini_image_pro")
                return ImageResponse(
                    success=True,
                    image_path=result.output_path,
                    image_base64=result.output_base64,
                    provider_used=result.provider,
                    generation_time=result.generation_time,
                    message=f"{result.message} [model={actual_model}]",
                )
            else:
                last_error = f"{provider_name}: {result.message}"
                logger.warning(last_error)
                continue

        except QueueBusyError as e:
            last_error = f"{provider_name}: {e}"
            logger.info(last_error)
            continue
        except Exception as e:
            last_error = f"{provider_name}: {type(e).__name__}: {e}"
            logger.error(last_error)
            continue

    raise HTTPException(503, f"All image providers failed. Last: {last_error}")
