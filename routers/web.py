"""Web Fetch router — POST /api/web/fetch (RSS / HTTP / Browser)."""
import logging
from fastapi import APIRouter, HTTPException

from core.models import WebFetchRequest, WebFetchResponse
from core.queue import JobQueue
from core.quota import QuotaTracker

logger = logging.getLogger("ai-hub.router.web")
router = APIRouter(prefix="/api/web", tags=["web"])

job_queue: JobQueue = None
quota: QuotaTracker = None
web_provider = None


def init(q: JobQueue, qt: QuotaTracker, prov):
    global job_queue, quota, web_provider
    job_queue = q
    quota = qt
    web_provider = prov


@router.post("/fetch", response_model=WebFetchResponse)
async def fetch(request: WebFetchRequest):
    prov = web_provider
    if not prov:
        raise HTTPException(503, "Web fetcher not configured")

    try:
        result = await job_queue.submit(
            prov.name,
            lambda: prov.execute({
                "url": request.url,
                "level": request.level,
                "timeout": request.timeout,
            }),
            timeout=request.timeout + 30,
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    if not result.success:
        raise HTTPException(500, result.message)

    return WebFetchResponse(
        success=True,
        content=result.message,
        generation_time=result.generation_time,
        message="OK",
    )
