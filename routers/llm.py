"""LLM router — POST /api/llm/chat with Groq primary + Gemini browser fallback."""
import logging
from fastapi import APIRouter, HTTPException

from core.models import LLMRequest, LLMResponse
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager
from config import FALLBACK_CHAINS

logger = logging.getLogger("ai-hub.router.llm")
router = APIRouter(prefix="/api/llm", tags=["llm"])

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


@router.post("/chat", response_model=LLMResponse)
async def chat(request: LLMRequest):
    # Build provider chain
    if request.provider == "auto":
        chain = FALLBACK_CHAINS.get("llm", ["gemini_chat"])
    else:
        # Map short names to actual provider names
        provider_aliases = {"gemini": "gemini_chat"}
        name = provider_aliases.get(request.provider, 
               request.provider if "_" in request.provider else request.provider + "_llm")
        chain = [name] + [p for p in FALLBACK_CHAINS.get("llm", []) if p != name]

    last_error = ""
    for provider_name in chain:
        prov = providers.get(provider_name)
        if not prov:
            continue

        if not quota.can_use(provider_name):
            last_error = f"{provider_name}: quota exhausted"
            logger.info(last_error)
            continue

        if prov.requires_chrome and prov.chrome_profile:
            ready = await chrome_mgr.ensure_running(prov.chrome_profile)
            if not ready:
                last_error = f"{provider_name}: Chrome not ready"
                logger.warning(last_error)
                continue

        try:
            result = await job_queue.submit(
                provider_name,
                lambda p=prov: p.execute({
                    "prompt": request.prompt,
                    "system_prompt": request.system_prompt,
                    "model": request.model,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                }),
                timeout=120,
                queue_key=getattr(prov, 'chrome_profile', None),
                queue_timeout=15 if getattr(prov, 'chrome_profile', None) else None,
            )
            if result.success:
                quota.increment(provider_name)
                return LLMResponse(
                    success=True,
                    content=result.message,
                    provider_used=result.provider,
                    generation_time=result.generation_time,
                    message="OK",
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
            last_error = f"{provider_name}: {e}"
            logger.error(last_error)
            continue

    raise HTTPException(503, f"All LLM providers failed. Last: {last_error}")
