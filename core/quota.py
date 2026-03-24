"""SQLite-backed quota tracker and job history."""
import sqlite3
import uuid
from datetime import date, datetime, timedelta, time
from typing import Optional
from config import QUOTA_DB, DAILY_LIMITS


# Gemini quota resets at 10:40 CST, not midnight
QUOTA_RESET_HOUR = 10
QUOTA_RESET_MINUTE = 40


def _quota_date() -> str:
    """Return the quota date based on Gemini reset time (10:40).
    Before 10:40 today counts as yesterday's quota period."""
    now = datetime.now()
    reset_time = now.replace(hour=QUOTA_RESET_HOUR, minute=QUOTA_RESET_MINUTE, second=0)
    if now < reset_time:
        return str((now - timedelta(days=1)).date())
    return str(now.date())


class QuotaTracker:
    def __init__(self):
        self.db = sqlite3.connect(QUOTA_DB, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS quota (
                provider TEXT,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (provider, date)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                provider TEXT,
                category TEXT,
                status TEXT DEFAULT 'pending',
                prompt TEXT,
                output_path TEXT,
                output_base64 TEXT,
                generation_time REAL,
                source TEXT,
                message TEXT,
                created_at TEXT,
                completed_at TEXT
            );
        """)

    def can_use(self, provider: str) -> bool:
        limit = DAILY_LIMITS.get(provider, 0)
        today = _quota_date()
        row = self.db.execute(
            "SELECT count FROM quota WHERE provider=? AND date=?",
            (provider, today)
        ).fetchone()
        return (row["count"] if row else 0) < limit

    def get_used(self, provider: str) -> int:
        today = _quota_date()
        row = self.db.execute(
            "SELECT count FROM quota WHERE provider=? AND date=?",
            (provider, today)
        ).fetchone()
        return row["count"] if row else 0

    def increment(self, provider: str):
        today = _quota_date()
        self.db.execute("""
            INSERT INTO quota (provider, date, count) VALUES (?, ?, 1)
            ON CONFLICT(provider, date) DO UPDATE SET count = count + 1
        """, (provider, today))
        self.db.commit()

    def get_all_quotas(self) -> dict:
        today = _quota_date()
        rows = self.db.execute(
            "SELECT provider, count FROM quota WHERE date=?", (today,)
        ).fetchall()
        result = {}
        for provider, limit in DAILY_LIMITS.items():
            used = 0
            for row in rows:
                if row["provider"] == provider:
                    used = row["count"]
                    break
            result[provider] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            }
        return result

    # === Job management ===
    def create_job(self, provider: str, category: str, prompt: str,
                   source: str = "api") -> str:
        job_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        self.db.execute("""
            INSERT INTO jobs (id, provider, category, status, prompt, source, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """, (job_id, provider, category, prompt, source, now))
        self.db.commit()
        return job_id

    def update_job(self, job_id: str, status: str,
                   output_path: Optional[str] = None,
                   output_base64: Optional[str] = None,
                   generation_time: Optional[float] = None,
                   message: Optional[str] = None):
        now = datetime.now().isoformat()
        self.db.execute("""
            UPDATE jobs SET status=?, output_path=?, output_base64=?,
                generation_time=?, message=?, completed_at=?
            WHERE id=?
        """, (status, output_path, output_base64, generation_time,
              message, now, job_id))
        self.db.commit()

    def get_job(self, job_id: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
