"""Podcast router — POST /api/podcast/generate (full async pipeline).

Complete flow:
  Phase 1 (~60s): Create NotebookLM notebook, start Audio + Video Overview
  Phase 2 (5-15 min): Background poller checks every 30s, downloads when ready
  Re-mux: ffmpeg faststart fix for universal playback
  Result: job status "completed" with output_base64

Estimated times:
  - Podcast (audio): 5-15 minutes total
  - Video Overview: 3-10 minutes total
"""
import asyncio
import base64
import logging
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from core.models import PodcastRequest, JobResponse
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager
from config import OUTPUT_DIRS

logger = logging.getLogger("gemgate.router.podcast")
router = APIRouter(prefix="/api/podcast", tags=["podcast"])

job_queue: JobQueue = None
quota: QuotaTracker = None
chrome_mgr: ChromeManager = None
podcast_provider = None


def init(q: JobQueue, qt: QuotaTracker, cm: ChromeManager, prov):
    global job_queue, quota, chrome_mgr, podcast_provider
    job_queue = q
    quota = qt
    chrome_mgr = cm
    podcast_provider = prov


def _remux_faststart(file_path: str) -> str:
    """Re-mux with ffmpeg faststart for universal playback. Returns final path."""
    src = Path(file_path)
    fixed = src.with_suffix('.fixed' + src.suffix)
    try:
        subprocess.run(
            ['ffmpeg', '-i', str(src), '-c', 'copy',
             '-movflags', '+faststart', str(fixed), '-y'],
            capture_output=True, timeout=120,
        )
        if fixed.exists() and fixed.stat().st_size > 1000:
            fixed.replace(src)
            logger.info(f"Re-muxed with faststart: {src}")
    except Exception as e:
        logger.warning(f"Re-mux failed (original kept): {e}")
    return str(src)


async def _phase2_poll(job_id: str, notebook_url: str):
    """Background task: poll NotebookLM every 30s until audio is ready, then download.

    Max wait: 20 minutes (40 polls × 30s).
    """
    logger.info(f"[{job_id}] Phase 2 started: polling for audio completion")

    save_dir = Path(OUTPUT_DIRS.get("podcasts", "/opt/gemgate/output/podcasts"))
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = str(save_dir / f"podcast_{job_id}.m4a")

    for poll_i in range(40):  # 40 × 30s = 20 min max
        await asyncio.sleep(30)

        try:
            is_ready, audio_path, error = await podcast_provider.check_and_download(
                notebook_url, save_path
            )
        except Exception as e:
            logger.error(f"[{job_id}] Phase 2 poll error: {e}")
            continue

        if is_ready and audio_path:
            # Download succeeded — re-mux and finalize
            final_path = _remux_faststart(audio_path)

            # Read file as base64
            try:
                with open(final_path, 'rb') as f:
                    audio_b64 = base64.b64encode(f.read()).decode()
                file_size = Path(final_path).stat().st_size
            except Exception as e:
                logger.error(f"[{job_id}] Failed to read audio: {e}")
                quota.update_job(job_id, "failed", message=f"Audio file read error: {e}")
                return

            quota.update_job(
                job_id, "completed",
                output_path=final_path,
                output_base64=audio_b64,
                message=f"Podcast ready ({file_size / (1024*1024):.1f}MB, ~{file_size * 8 / 256000:.0f}s audio)",
            )
            logger.info(f"[{job_id}] Phase 2 complete: {file_size / (1024*1024):.1f}MB")
            return

        elif is_ready and error:
            # Audio section exists but download failed
            logger.warning(f"[{job_id}] Audio ready but download failed: {error}")
            quota.update_job(job_id, "failed", message=f"Download failed: {error}")
            return

        elif error and error not in (None, "no_audio_section"):
            logger.warning(f"[{job_id}] Phase 2 error: {error}")
            # Don't fail yet — might be transient

        else:
            if poll_i % 4 == 0:  # Log every 2 min
                elapsed = (poll_i + 1) * 30
                logger.info(f"[{job_id}] Still generating... ({elapsed}s elapsed)")

    # Timeout after 20 minutes
    quota.update_job(
        job_id, "failed",
        message="Podcast generation timed out after 20 minutes. Audio may still be generating on NotebookLM.",
    )
    logger.warning(f"[{job_id}] Phase 2 timed out after 20 min")


async def _run_podcast_job(job_id: str, params: dict):
    """Full pipeline: Phase 1 → Phase 2 → complete."""
    quota.update_job(job_id, "running")

    # ── Phase 1: Create notebook + start generation ──
    try:
        result = await job_queue.submit(
            podcast_provider.name,
            lambda: podcast_provider.execute(params),
            timeout=300,
            queue_key=getattr(podcast_provider, 'chrome_profile', None),
        )
    except Exception as e:
        quota.update_job(job_id, "failed", message=f"Phase 1 error: {e}")
        return

    if not result.success:
        quota.update_job(job_id, "failed", message=f"Phase 1 failed: {result.message}")
        return

    quota.increment(podcast_provider.name)
    notebook_url = result.output_path  # e.g. https://notebooklm.google.com/notebook/xxx

    # Mark as "generating" (NOT "completed") — audio is still being created
    quota.update_job(
        job_id, "generating",
        output_path=notebook_url,
        generation_time=result.generation_time,
        message=f"Audio generation started. Estimated 5-15 minutes. {result.message}",
    )
    logger.info(f"[{job_id}] Phase 1 done ({result.generation_time:.0f}s). Starting Phase 2 poll...")

    # ── Phase 2: Background poll + download ──
    asyncio.create_task(_phase2_poll(job_id, notebook_url))


@router.post("/generate", response_model=JobResponse)
async def generate_podcast(request: PodcastRequest):
    if not request.sources:
        raise HTTPException(400, "At least one source required")

    if not quota.can_use(podcast_provider.name):
        raise HTTPException(429, "NotebookLM daily quota exhausted (20/day)")

    job_id = quota.create_job(podcast_provider.name, "podcast", str(request.sources[:3]))
    asyncio.create_task(_run_podcast_job(job_id, {
        "sources": request.sources,
        "topic": request.topic,
    }))

    return JobResponse(
        job_id=job_id,
        status="pending",
        poll_url=f"/api/job/{job_id}",
        message="Podcast generation started. Phase 1: ~60s, Phase 2 (audio generation): 5-15 min. Poll /api/job/{job_id} for status.",
    )
