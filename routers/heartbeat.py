"""Heartbeat API — tasks self-report execution status.

POST /api/heartbeat  — record task execution result
GET  /api/heartbeat  — read all heartbeat records
GET  /api/heartbeat/{task} — read single task history
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("ai-hub.router.heartbeat")
router = APIRouter(prefix="/api", tags=["heartbeat"])

TAIPEI_TZ = timezone(timedelta(hours=8))
STATE_DIR = Path("/opt/gemgate/state")
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
MAX_HISTORY = 50  # per task


class HeartbeatPayload(BaseModel):
    task: str
    status: str  # "ok", "failed", "partial", "started"
    host: Optional[str] = None
    message: Optional[str] = None


def _read_store() -> dict:
    """Read heartbeat store. Returns {task: {last_status, last_ok, last_fail, history: [...]}}"""
    if HEARTBEAT_FILE.exists():
        try:
            return json.loads(HEARTBEAT_FILE.read_text())
        except Exception:
            pass
    return {}


def _write_store(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


@router.post("/heartbeat")
async def post_heartbeat(payload: HeartbeatPayload):
    now = datetime.now(tz=TAIPEI_TZ).isoformat()
    store = _read_store()

    task_key = payload.task
    if task_key not in store:
        store[task_key] = {
            "last_status": None,
            "last_ok": None,
            "last_fail": None,
            "host": payload.host,
            "history": [],
        }

    entry = store[task_key]
    entry["last_status"] = payload.status
    entry["host"] = payload.host or entry.get("host")

    if payload.status == "ok":
        entry["last_ok"] = now
    elif payload.status in ("failed", "partial"):
        entry["last_fail"] = now

    # Append to history (newest first, cap at MAX_HISTORY)
    entry["history"].insert(0, {
        "timestamp": now,
        "status": payload.status,
        "message": payload.message,
    })
    entry["history"] = entry["history"][:MAX_HISTORY]

    _write_store(store)
    logger.info(f"Heartbeat: {task_key} = {payload.status} ({payload.message or ''})")
    return {"ok": True, "task": task_key, "status": payload.status, "recorded_at": now}


@router.get("/heartbeat")
async def get_heartbeat():
    return _read_store()


@router.get("/heartbeat/{task}")
async def get_heartbeat_task(task: str):
    store = _read_store()
    if task not in store:
        return {"error": "task not found", "task": task}
    return store[task]
