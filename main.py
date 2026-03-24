#!/usr/bin/env python3
"""
GemGate — Self-hosted Google AI Gateway
Zero API key. Zero cost. Just a Google account.
"""
import logging
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import HUB_PORT, HUB_BIND
from core.auth import AuthMiddleware
from core.queue import JobQueue
from core.quota import QuotaTracker
from core.api_keys import APIKeyManager
from core.firefox_manager import FirefoxManager

# Routers
from routers import admin, tts, image, video, podcast, llm, vision, web, dashboard, audio, heartbeat
from routers import openai_compat, register

# Providers — Google services only
from providers.google_tts import GoogleTTSProvider
from providers.gemini_image_ff import GeminiImageProvider
from providers.gemini_video_ff import GeminiVideoProvider
from providers.gemini_chat_ff import GeminiChatProvider
from providers.gemini_audio_ff import GeminiAudioProvider
from providers.flow_image_ff import FlowImageProvider
from providers.notebooklm_ff import NotebookLMProvider
from providers.notebooklm_video_ff import NotebookLMVideoProvider
from providers.web_fetcher_ff import WebFetcherProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("gemgate")

app = FastAPI(
    title="GemGate",
    description="Self-hosted Google AI Gateway — Zero API key, Zero cost",
    version="1.0.0",
)

app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core
job_queue = JobQueue()
quota = QuotaTracker()
key_mgr = APIKeyManager()
firefox_mgr = FirefoxManager()

# ChromeManager stub (for router compat — not actually used)
class ChromeManagerStub:
    async def ensure_running(self, profile): return True
    async def close_all(self): pass
    async def get_all_status(self): return {}

chrome_mgr = ChromeManagerStub()

# Google-only providers
all_providers = {
    "google_tts": GoogleTTSProvider(),
    "gemini_image": GeminiImageProvider(),
    "gemini_video": GeminiVideoProvider(),
    "gemini_chat": GeminiChatProvider(),
    "gemini_audio": GeminiAudioProvider(),
    "flow_image": FlowImageProvider(),
    "notebooklm": NotebookLMProvider(),
    "notebooklm_video": NotebookLMVideoProvider(),
    "web_fetcher": WebFetcherProvider(),
}

# Set FirefoxManager on providers that need it
for _prov in all_providers.values():
    if hasattr(_prov, '_firefox_mgr'):
        _prov._firefox_mgr = firefox_mgr

image_providers = {k: v for k, v in all_providers.items() if v.category == "image"}
video_providers = {k: v for k, v in all_providers.items() if v.category == "video"}
llm_providers = {k: v for k, v in all_providers.items() if v.category == "llm"}
audio_providers = {k: v for k, v in all_providers.items() if v.category == "audio"}

# Init legacy routers
tts.init(job_queue, quota)
image.init(job_queue, quota, chrome_mgr, image_providers)
video.init(job_queue, quota, chrome_mgr, video_providers)
podcast.init(job_queue, quota, chrome_mgr, all_providers["notebooklm"])
llm.init(job_queue, quota, chrome_mgr, llm_providers)
vision.init(job_queue, quota, chrome_mgr, all_providers["gemini_chat"])
web.init(job_queue, quota, all_providers["web_fetcher"])
audio.init(job_queue, quota, chrome_mgr, audio_providers)
admin.init(job_queue, quota, chrome_mgr, all_providers)
dashboard.init(job_queue, quota, all_providers)

# Init new routers (OpenAI compat + registration)
openai_compat.init(key_mgr, job_queue, quota, all_providers)
register.init(key_mgr)

# Mount routers — register and openai_compat FIRST (landing page at /)
app.include_router(register.router)
app.include_router(openai_compat.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(tts.router)
app.include_router(image.router)
app.include_router(video.router)
app.include_router(podcast.router)
app.include_router(llm.router)
app.include_router(vision.router)
app.include_router(web.router)
app.include_router(audio.router)
app.include_router(heartbeat.router)

# Static files
_dashboard_dir = os.path.join(os.path.dirname(__file__), "output", "dashboard")
os.makedirs(_dashboard_dir, exist_ok=True)
app.mount("/dashboard", StaticFiles(directory=_dashboard_dir, html=True), name="dashboard")


@app.on_event("shutdown")
async def shutdown():
    await firefox_mgr.close_all()


@app.on_event("startup")
async def startup():
    logger.info(f"GemGate v1.0.0 on {HUB_BIND}:{HUB_PORT}")
    logger.info(f"Providers: {list(all_providers.keys())}")
    logger.info(f"API Keys: {len(key_mgr.get_all_keys())} registered")
    logger.info("Browser backend: Firefox persistent context")
    logger.info("OpenAI-compatible API: /v1/chat/completions, /v1/images/generations, /v1/audio/speech")


if __name__ == "__main__":
    uvicorn.run(app, host=HUB_BIND, port=HUB_PORT)
