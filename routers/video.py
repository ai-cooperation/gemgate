"""Video router — POST /api/video/generate (full async pipeline).

Complete flow:
  Phase 1 (~60s): Create NotebookLM notebook, start Video Overview
  Phase 2 (3-10 min): Background poller checks every 30s, downloads when ready
  Re-mux: ffmpeg faststart fix for universal playback
  Result: job status "completed" with output_base64

Estimated time: 4-12 minutes total (Phase 1 + Phase 2)
Video files are typically 15-30MB (MP4).
"""
import asyncio
import base64
import logging
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from core.models import VideoRequest, JobResponse
from core.queue import JobQueue, QueueBusyError
from core.quota import QuotaTracker
from core.chrome_manager import ChromeManager
from config import FALLBACK_CHAINS, OUTPUT_DIRS

logger = logging.getLogger("gemgate.router.video")
router = APIRouter(prefix="/api/video", tags=["video"])

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


def _remux_faststart(file_path: str) -> str:
    """Re-mux with ffmpeg faststart for universal playback."""
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
            logger.info(f"Re-muxed video with faststart: {src}")
    except Exception as e:
        logger.warning(f"Video re-mux failed (original kept): {e}")
    return str(src)


async def _phase2_video_poll(job_id: str, notebook_url: str, provider_name: str):
    """Background task: poll NotebookLM every 30s until video is ready.

    Max wait: 15 minutes (30 polls × 30s).
    Video Overview typically takes 3-10 minutes.
    """
    prov = providers.get(provider_name)
    if not prov:
        quota.update_job(job_id, "failed", message="Provider not found for Phase 2")
        return

    # Import the tracker's download function
    try:
        from automations.podcast_tracker_ff import check_and_download_video
    except ImportError:
        logger.error(f"[{job_id}] podcast_tracker_ff not available for video download")
        quota.update_job(job_id, "failed", message="Video download module not available")
        return

    save_dir = Path(OUTPUT_DIRS.get("videos", "/opt/gemgate/output/videos"))
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = str(save_dir / f"video_{job_id}.mp4")

    logger.info(f"[{job_id}] Phase 2 video: polling for completion")

    for poll_i in range(30):  # 30 × 30s = 15 min max
        await asyncio.sleep(30)

        try:
            is_ready, video_path, error = await check_and_download_video(
                save_path, notebook_url
            )
        except Exception as e:
            logger.error(f"[{job_id}] Video poll error: {e}")
            continue

        if is_ready and video_path:
            final_path = _remux_faststart(video_path)

            try:
                with open(final_path, 'rb') as f:
                    video_b64 = base64.b64encode(f.read()).decode()
                file_size = Path(final_path).stat().st_size
            except Exception as e:
                quota.update_job(job_id, "failed", message=f"Video file read error: {e}")
                return

            quota.update_job(
                job_id, "completed",
                output_path=final_path,
                output_base64=video_b64,
                message=f"Video ready ({file_size / (1024*1024):.1f}MB)",
            )
            logger.info(f"[{job_id}] Video Phase 2 complete: {file_size / (1024*1024):.1f}MB")
            return

        elif is_ready and error:
            logger.warning(f"[{job_id}] Video ready but download failed: {error}")
            quota.update_job(job_id, "failed", message=f"Video download failed: {error}")
            return

        elif error == "no_video_section":
            quota.update_job(job_id, "failed", message="Video Overview not available on this notebook")
            return

        else:
            if poll_i % 4 == 0:
                elapsed = (poll_i + 1) * 30
                logger.info(f"[{job_id}] Video still generating... ({elapsed}s)")

    quota.update_job(
        job_id, "failed",
        message="Video generation timed out after 15 minutes.",
    )
    logger.warning(f"[{job_id}] Video Phase 2 timed out")


async def _run_video_job(job_id: str, provider_name: str, params: dict):
    """Full pipeline: Phase 1 → Phase 2 → complete."""
    prov = providers[provider_name]
    quota.update_job(job_id, "running")

    # ── Phase 1 ──
    try:
        result = await job_queue.submit(
            provider_name,
            lambda: prov.execute(params),
            timeout=params.get("timeout", 300) + 30,
            queue_key=getattr(prov, 'chrome_profile', None),
        )
    except Exception as e:
        quota.update_job(job_id, "failed", message=f"Phase 1 error: {e}")
        return

    if not result.success:
        quota.update_job(job_id, "failed", message=f"Phase 1 failed: {result.message}")
        return

    quota.increment(provider_name)
    notebook_url = result.output_path

    # Mark as "generating"
    quota.update_job(
        job_id, "generating",
        output_path=notebook_url,
        generation_time=result.generation_time,
        message=f"Video generation started. Estimated 3-10 minutes. {result.message or ''}",
    )
    logger.info(f"[{job_id}] Phase 1 done. Starting Phase 2 video poll...")

    # ── Phase 2 ──
    asyncio.create_task(_phase2_video_poll(job_id, notebook_url, provider_name))


@router.post("/generate", response_model=JobResponse)
async def generate_video(request: VideoRequest):
    # Build provider chain
    if request.provider == "auto":
        chain = FALLBACK_CHAINS.get("video", [])
    else:
        name = request.provider + "_video" if "_" not in request.provider else request.provider
        chain = [name] + [p for p in FALLBACK_CHAINS.get("video", []) if p != name]

    selected = None
    last_error = ""
    for provider_name in chain:
        prov = providers.get(provider_name)
        if not prov:
            continue
        if not quota.can_use(provider_name):
            last_error = f"{provider_name}: quota exhausted"
            continue
        selected = provider_name
        break

    if not selected:
        raise HTTPException(503, f"No video provider available. Last: {last_error}")

    job_id = quota.create_job(selected, "video", request.prompt)
    asyncio.create_task(_run_video_job(job_id, selected, {
        "prompt": request.prompt,
        "timeout": request.timeout,
    }))

    return JobResponse(
        job_id=job_id,
        status="pending",
        poll_url=f"/api/job/{job_id}",
        message=f"Video generation started with {selected}. Estimated 4-12 minutes. Poll /api/job/{job_id} for status.",
    )
