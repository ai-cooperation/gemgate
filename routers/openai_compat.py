"""OpenAI-compatible API layer for GemGate.

Maps OpenAI SDK calls to GemGate's internal providers so students can use
the standard openai Python/JS SDK or paste the endpoint into Google Apps Script.

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions       → Gemini Chat (+ Vision if image attached)
  POST /v1/images/generations     → Gemini Image
  POST /v1/audio/speech           → Google TTS
"""
import base64
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from core.api_keys import APIKeyManager
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from config import FALLBACK_CHAINS, DUAL_ACCOUNT_PROFILES

logger = logging.getLogger("gemgate.openai")
router = APIRouter(prefix="/v1", tags=["openai-compat"])

# Injected from main.py
key_mgr: APIKeyManager = None
job_queue: JobQueue = None
quota: QuotaTracker = None
providers: dict = {}

# Round-robin counter for dual-account load balancing
_rr_counter: dict[str, int] = {}


def init(_key_mgr, _job_queue, _quota, _providers):
    global key_mgr, job_queue, quota, providers
    key_mgr = _key_mgr
    job_queue = _job_queue
    quota = _quota
    providers = _providers


# ── Auth helper ──

def _auth(authorization: Optional[str]) -> str:
    """Extract and validate API key from Bearer token. Returns key string."""
    if not authorization:
        raise HTTPException(401, "Missing Authorization header. Use: Bearer gem-xxx")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Invalid Authorization format. Use: Bearer gem-xxx")
    key = parts[1]
    api_key = key_mgr.get_by_key(key)
    if not api_key or not api_key.active:
        raise HTTPException(401, "Invalid or deactivated API key")
    return key


def _check_quota(key: str, endpoint: str):
    """Check rate limit and daily quota. Raises 429 if exceeded."""
    allowed, reason = key_mgr.check_and_record(key, endpoint)
    if not allowed:
        raise HTTPException(429, reason)


def _get_queue_key(provider_name: str) -> str | None:
    """Get queue key with round-robin for dual-account providers."""
    profiles = DUAL_ACCOUNT_PROFILES.get(provider_name)
    if not profiles:
        prov = providers.get(provider_name)
        return getattr(prov, 'chrome_profile', None) if prov else None

    # Round-robin between profiles
    idx = _rr_counter.get(provider_name, 0)
    profile = profiles[idx % len(profiles)]
    _rr_counter[provider_name] = idx + 1
    logger.info(f"Round-robin {provider_name}: using profile {profile} (#{idx})")
    return profile


# ── Models ──

@router.get("/models")
async def list_models():
    """List available models (OpenAI-compatible)."""
    models = [
        {"id": "gemini", "object": "model", "created": 1700000000,
         "owned_by": "google", "description": "Gemini Chat (free via Google)"},
        {"id": "gemini-image", "object": "model", "created": 1700000000,
         "owned_by": "google", "description": "Gemini Image Generation (fast)"},
        {"id": "gemini-vision", "object": "model", "created": 1700000000,
         "owned_by": "google", "description": "Gemini Vision (image understanding)"},
        {"id": "gemini-tts", "object": "model", "created": 1700000000,
         "owned_by": "google", "description": "Google Text-to-Speech"},
    ]
    return {"object": "list", "data": models}


# ── Chat Completions ──

class ChatMessage(BaseModel):
    role: str
    content: str | list  # str or list of content parts (for vision)

class ChatRequest(BaseModel):
    model: str = "gemini"
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 4096
    stream: bool = False

@router.post("/chat/completions")
async def chat_completions(
    request: ChatRequest,
    authorization: str = Header(None),
):
    key = _auth(authorization)
    start = time.time()

    # Detect vision request (image in messages)
    has_image = False
    image_b64 = None
    prompt_text = ""

    for msg in request.messages:
        if isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        has_image = True
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            image_b64 = url.split(",", 1)[-1]
                    elif part.get("type") == "text":
                        prompt_text += part.get("text", "") + "\n"
        else:
            if msg.role == "user":
                prompt_text += msg.content + "\n"

    endpoint = "vision" if has_image else "chat"
    _check_quota(key, endpoint)

    # Route to provider
    if has_image:
        prov = providers.get("gemini_chat")
        if not prov:
            raise HTTPException(503, "Vision provider not available")
        try:
            result = await job_queue.submit(
                "gemini_chat",
                lambda: prov.execute({
                    "prompt": prompt_text.strip(),
                    "image_base64": image_b64,
                }),
                timeout=120,
                queue_key=_get_queue_key("gemini_chat"),
            )
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            key_mgr.record_completion(key, endpoint, elapsed, error_msg=str(e))
            raise HTTPException(503, f"Vision failed: {e}")
    else:
        # Text chat
        chain = FALLBACK_CHAINS.get("llm", ["gemini_chat"])
        result = None
        last_error = ""
        for provider_name in chain:
            prov = providers.get(provider_name)
            if not prov:
                continue
            if not quota.can_use(provider_name):
                last_error = f"{provider_name}: global quota exhausted"
                continue
            try:
                result = await job_queue.submit(
                    provider_name,
                    lambda p=prov: p.execute({
                        "prompt": prompt_text.strip(),
                        "system_prompt": next(
                            (m.content for m in request.messages
                             if m.role == "system" and isinstance(m.content, str)),
                            ""
                        ),
                        "temperature": request.temperature,
                        "max_tokens": request.max_tokens,
                    }),
                    timeout=120,
                    queue_key=_get_queue_key(provider_name),
                )
                if result.success:
                    quota.increment(provider_name)
                    break
                last_error = f"{provider_name}: {result.message}"
            except QueueBusyError as e:
                last_error = str(e)
                continue
            except Exception as e:
                last_error = str(e)
                continue

        if not result or not result.success:
            elapsed = int((time.time() - start) * 1000)
            key_mgr.record_completion(key, endpoint, elapsed, error_msg=last_error)
            raise HTTPException(503, f"Chat failed: {last_error}")

    elapsed = int((time.time() - start) * 1000)
    key_mgr.record_completion(key, endpoint, elapsed, provider=result.provider or "")

    # OpenAI-compatible response
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": result.message or "",
            },
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": len(prompt_text) // 4,  # approximate
            "completion_tokens": len(result.message or "") // 4,
            "total_tokens": (len(prompt_text) + len(result.message or "")) // 4,
        },
    }


# ── Image Generations ──

class ImageGenRequest(BaseModel):
    model: str = "gemini-image"
    prompt: str
    n: int = 1
    size: str = "1024x1024"
    response_format: str = "b64_json"  # "url" or "b64_json"

@router.post("/images/generations")
async def image_generations(
    request: ImageGenRequest,
    authorization: str = Header(None),
):
    key = _auth(authorization)
    _check_quota(key, "image")
    start = time.time()

    prov = providers.get("gemini_image")
    if not prov:
        raise HTTPException(503, "Image provider not available")

    try:
        result = await job_queue.submit(
            "gemini_image",
            lambda: prov.execute({
                "prompt": request.prompt,
                "timeout": 120,
                "model": "fast",
                "skip_base64": False,
            }),
            timeout=150,
            queue_key=_get_queue_key("gemini_image"),
        )
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        key_mgr.record_completion(key, "image", elapsed, error_msg=str(e))
        raise HTTPException(503, f"Image generation failed: {e}")

    if not result.success:
        elapsed = int((time.time() - start) * 1000)
        key_mgr.record_completion(key, "image", elapsed, error_msg=result.message)
        raise HTTPException(503, f"Image generation failed: {result.message}")

    quota.increment("gemini_image")
    elapsed = int((time.time() - start) * 1000)
    key_mgr.record_completion(key, "image", elapsed, provider="gemini_image")

    data_item = {}
    if request.response_format == "b64_json":
        data_item["b64_json"] = result.output_base64 or ""
    else:
        # Return as data URL for simplicity
        data_item["url"] = f"data:image/png;base64,{result.output_base64 or ''}"

    return {
        "created": int(time.time()),
        "data": [data_item],
    }


# ── Audio Speech (TTS) ──

class TTSSpeechRequest(BaseModel):
    model: str = "gemini-tts"
    input: str
    voice: str = "alloy"  # ignored, uses Google TTS
    response_format: str = "mp3"
    speed: float = 1.0

@router.post("/audio/speech")
async def audio_speech(
    request: TTSSpeechRequest,
    authorization: str = Header(None),
):
    key = _auth(authorization)
    _check_quota(key, "tts")
    start = time.time()

    prov = providers.get("google_tts")
    if not prov:
        raise HTTPException(503, "TTS provider not available")

    try:
        result = await job_queue.submit(
            "google_tts",
            lambda: prov.execute({
                "text": request.input,
                "lang": "zh-TW",
                "slow": request.speed < 0.8,
            }),
            timeout=30,
        )
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        key_mgr.record_completion(key, "tts", elapsed, error_msg=str(e))
        raise HTTPException(503, f"TTS failed: {e}")

    if not result.success:
        elapsed = int((time.time() - start) * 1000)
        key_mgr.record_completion(key, "tts", elapsed, error_msg=result.message)
        raise HTTPException(503, f"TTS failed: {result.message}")

    elapsed = int((time.time() - start) * 1000)
    key_mgr.record_completion(key, "tts", elapsed, provider="google_tts")

    # Return audio as binary (OpenAI returns binary for audio/speech)
    audio_bytes = base64.b64decode(result.output_base64) if result.output_base64 else b""
    return StreamingResponse(
        iter([audio_bytes]),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "attachment; filename=speech.mp3"},
    )


# ── Usage (non-OpenAI, GemGate extension) ──

@router.get("/usage")
async def get_usage(authorization: str = Header(None)):
    """Get current key's usage stats."""
    key = _auth(authorization)
    stats = key_mgr.get_usage_stats(key)
    api_key = key_mgr.get_by_key(key)
    return {
        "student_name": api_key.student_name if api_key else "",
        "usage": stats,
    }
