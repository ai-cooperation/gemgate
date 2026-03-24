"""Admin router — /api/health, /api/status, /api/quota, /api/job/{id}"""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from core.quota import QuotaTracker
from core.queue import JobQueue
from core.chrome_manager import ChromeManager
from core.models import JobStatus
from config import DAILY_LIMITS, OUTPUT_BASE

logger = logging.getLogger("ai-hub.router.admin")
router = APIRouter(prefix="/api", tags=["admin"])

# Injected from main.py
quota: QuotaTracker = None
job_queue: JobQueue = None
chrome_mgr: ChromeManager = None

# Provider registry (filled by main.py)
providers: dict = {}


def init(q: JobQueue, qt: QuotaTracker, cm: ChromeManager, prov: dict):
    global job_queue, quota, chrome_mgr, providers
    job_queue = q
    quota = qt
    chrome_mgr = cm
    providers = prov


@router.get("/health")
async def health():
    return {"status": "ok", "service": "ai-hub"}


@router.get("/status")
async def status():
    chrome_status = await chrome_mgr.get_all_status()
    result = {}
    for name, prov in providers.items():
        profile = prov.chrome_profile
        chrome_ready = True
        if profile:
            chrome_ready = chrome_status.get(profile, {}).get("ready", False)
        result[name] = {
            "category": prov.category,
            "chrome_profile": profile,
            "chrome_ready": chrome_ready,
            "busy": job_queue.is_busy(name),
            "healthy": True if (profile and not chrome_ready) else await prov.health_check(),
            "today_used": quota.get_used(name),
            "daily_limit": DAILY_LIMITS.get(name, 0),
            "remaining": max(0, DAILY_LIMITS.get(name, 0) - quota.get_used(name)),
        }
    return result


@router.get("/quota")
async def get_quota():
    return quota.get_all_quotas()


@router.get("/job/{job_id}")
async def get_job(job_id: str):
    job = quota.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    # Estimate remaining time for generating jobs
    estimated_seconds = None
    if job["status"] == "generating":
        from datetime import datetime
        created = datetime.fromisoformat(job["created_at"])
        elapsed = (datetime.now() - created).total_seconds()
        if job["category"] == "podcast":
            estimated_seconds = max(0, int(900 - elapsed))  # ~15 min total
        elif job["category"] == "video":
            estimated_seconds = max(0, int(600 - elapsed))  # ~10 min total

    result = {
        "job_id": job["id"],
        "provider": job["provider"],
        "status": job["status"],
        "prompt": job["prompt"] or "",
        "output_path": job.get("output_path"),
        "output_base64": job.get("output_base64"),
        "generation_time": job.get("generation_time"),
        "message": job.get("message"),
    }
    if estimated_seconds is not None:
        result["estimated_seconds"] = estimated_seconds
    return result


@router.get("/files/{category}/{filename}")
async def download_file(category: str, filename: str):
    """Download a generated file."""
    allowed = {"images", "videos", "audio", "podcasts"}
    if category not in allowed:
        raise HTTPException(400, f"Invalid category: {category}")

    file_path = Path(OUTPUT_BASE) / category / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    # Determine media type
    suffix = file_path.suffix.lower()
    media_types = {
        ".png": "image/png", ".jpg": "image/jpeg",
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".mp4": "video/mp4",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(file_path, media_type=media_type)
