"""API Key management — SQLite-backed, self-service registration.

Students visit the landing page, enter a nickname, and receive a personal
API key they can paste into Google Apps Script or any HTTP client.

Each key has per-endpoint daily limits and RPM (requests per minute) throttling.
"""
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from config import QUOTA_DB

# ── Defaults ──

# Google FREE account global daily limits (shared across ALL keys)
# These are observed limits for FREE accounts, NOT Pro/paid.
# Google may change these without notice.
GOOGLE_GLOBAL_LIMITS = {
    "chat": 500,      # Gemini free: no hard cap observed, but quality throttle possible
    "image": 50,      # Gemini free: ~50 images/day (Pro would be ~100)
    "tts": 1000,      # gTTS library: no Google account limit (runs locally)
    "vision": 500,    # Gemini free: shares chat quota
    "video": 5,       # NotebookLM free: ~5 video overviews/day (Pro: ~20)
    "podcast": 5,     # NotebookLM free: ~5 audio overviews/day (Pro: ~20)
    "web": 1000,      # Local HTTP fetch: no Google limit
}

# Per-key defaults (auto-calculated from global / active keys if set to 0)
DEFAULT_LIMITS = {
    "chat": 0,        # 0 = auto-split from GOOGLE_GLOBAL_LIMITS
    "image": 0,
    "tts": 0,
    "vision": 0,
    "video": 0,
    "podcast": 0,
    "web": 0,
}
DEFAULT_RPM = 5  # requests per minute per key
DEFAULT_TTL_HOURS = 24  # keys auto-expire after 24 hours (0 = never expire)


@dataclass
class APIKey:
    id: int
    key: str
    student_name: str
    created_at: str
    expires_at: Optional[str]
    active: bool
    daily_chat: int
    daily_image: int
    daily_tts: int
    daily_vision: int
    daily_video: int
    daily_podcast: int
    daily_web: int
    rpm: int


class APIKeyManager:
    def __init__(self, db_path: str = QUOTA_DB):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_tables()
        # In-memory RPM tracker: {key_hash: [timestamp, ...]}
        self._rpm_log: dict[str, list[float]] = {}

    def _init_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                student_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                daily_chat INTEGER DEFAULT 50,
                daily_image INTEGER DEFAULT 10,
                daily_tts INTEGER DEFAULT 20,
                daily_vision INTEGER DEFAULT 20,
                daily_video INTEGER DEFAULT 3,
                daily_podcast INTEGER DEFAULT 3,
                daily_web INTEGER DEFAULT 30,
                rpm INTEGER DEFAULT 5
            );
            CREATE TABLE IF NOT EXISTS key_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT DEFAULT 'ok',
                latency_ms INTEGER DEFAULT 0,
                provider TEXT DEFAULT '',
                error_msg TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_key_usage_key_ts
                ON key_usage(api_key, timestamp);
            CREATE INDEX IF NOT EXISTS idx_key_usage_endpoint_ts
                ON key_usage(endpoint, timestamp);
        """)

    # ── Key Generation ──

    def register(self, student_name: str) -> APIKey:
        """Register a new student and return their API key."""
        student_name = student_name.strip()
        if not student_name or len(student_name) > 50:
            raise ValueError("Name must be 1-50 characters")

        # Check if name already has an active, non-expired key
        existing = self.db.execute(
            "SELECT * FROM api_keys WHERE student_name = ? AND active = 1",
            (student_name,),
        ).fetchone()
        if existing:
            key_obj = self._row_to_key(existing)
            if not self._is_expired(key_obj):
                return key_obj
            # Expired — deactivate old key, create new one
            self.deactivate(existing["key"])

        # Generate key: gem-<random 32 chars>
        key = f"gem-{secrets.token_hex(16)}"
        now = datetime.now()
        expires = (now + timedelta(hours=DEFAULT_TTL_HOURS)).isoformat() if DEFAULT_TTL_HOURS > 0 else None

        self.db.execute(
            """INSERT INTO api_keys
               (key, student_name, created_at, expires_at, daily_chat, daily_image,
                daily_tts, daily_vision, daily_video, daily_podcast, daily_web, rpm)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, student_name, now.isoformat(), expires,
             DEFAULT_LIMITS["chat"], DEFAULT_LIMITS["image"],
             DEFAULT_LIMITS["tts"], DEFAULT_LIMITS["vision"],
             DEFAULT_LIMITS["video"], DEFAULT_LIMITS["podcast"],
             DEFAULT_LIMITS["web"], DEFAULT_RPM),
        )
        self.db.commit()
        return self.get_by_key(key)

    def _is_expired(self, api_key: "APIKey") -> bool:
        """Check if a key has expired."""
        if not api_key.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(api_key.expires_at)
            return datetime.now() > exp
        except (ValueError, TypeError):
            return False

    def get_active_count(self) -> int:
        """Return count of active, non-expired keys."""
        self._cleanup_expired()
        row = self.db.execute("SELECT COUNT(*) FROM api_keys WHERE active = 1").fetchone()
        return row[0] if row else 0

    def _cleanup_expired(self):
        """Deactivate all expired keys."""
        now = datetime.now().isoformat()
        self.db.execute(
            "UPDATE api_keys SET active = 0 WHERE active = 1 AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        self.db.commit()

    # ── Lookup ──

    def get_by_key(self, key: str) -> Optional[APIKey]:
        row = self.db.execute(
            "SELECT * FROM api_keys WHERE key = ?", (key,)
        ).fetchone()
        return self._row_to_key(row) if row else None

    def get_all_keys(self) -> list[APIKey]:
        rows = self.db.execute(
            "SELECT * FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_key(r) for r in rows]

    def deactivate(self, key: str) -> bool:
        self.db.execute(
            "UPDATE api_keys SET active = 0 WHERE key = ?", (key,)
        )
        self.db.commit()
        return self.db.total_changes > 0

    def activate(self, key: str) -> bool:
        self.db.execute(
            "UPDATE api_keys SET active = 1 WHERE key = ?", (key,)
        )
        self.db.commit()
        return self.db.total_changes > 0

    # ── Usage & Rate Limiting ──

    def check_and_record(self, key: str, endpoint: str) -> tuple[bool, str]:
        """Check quota + RPM, record usage if allowed.

        Returns (allowed: bool, reason: str).
        """
        api_key = self.get_by_key(key)
        if not api_key:
            return False, "Invalid API key"
        if not api_key.active:
            return False, "API key is deactivated"
        if self._is_expired(api_key):
            self.deactivate(key)
            return False, "API key has expired (24h TTL)"

        # RPM check
        now = time.time()
        window = [t for t in self._rpm_log.get(key, []) if now - t < 60]
        self._rpm_log[key] = window
        if len(window) >= api_key.rpm:
            return False, f"Rate limit exceeded ({api_key.rpm} RPM)"

        # Daily quota check
        limit = self._get_daily_limit(api_key, endpoint)
        used = self._get_daily_usage(key, endpoint)
        if used >= limit:
            return False, f"Daily quota exhausted ({used}/{limit} for {endpoint})"

        # Record
        self._rpm_log[key] = window + [now]
        self._record_usage(key, endpoint, "ok")
        return True, "OK"

    def record_completion(self, key: str, endpoint: str, latency_ms: int,
                          provider: str = "", error_msg: str = ""):
        """Update the most recent usage record with completion info."""
        self.db.execute(
            """UPDATE key_usage SET latency_ms = ?, provider = ?, error_msg = ?,
                   status = ?
               WHERE id = (
                   SELECT id FROM key_usage
                   WHERE api_key = ? AND endpoint = ?
                   ORDER BY id DESC LIMIT 1
               )""",
            (latency_ms, provider, error_msg,
             "error" if error_msg else "ok",
             key, endpoint),
        )
        self.db.commit()

    def get_usage_stats(self, key: str) -> dict:
        """Get today's usage for a specific key."""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            """SELECT endpoint, COUNT(*) as cnt, AVG(latency_ms) as avg_lat
               FROM key_usage
               WHERE api_key = ? AND timestamp >= ? AND status = 'ok'
               GROUP BY endpoint""",
            (key, today),
        ).fetchall()

        api_key = self.get_by_key(key)
        result = {}
        for row in rows:
            ep = row["endpoint"]
            limit = self._get_daily_limit(api_key, ep) if api_key else 0
            result[ep] = {
                "used": row["cnt"],
                "limit": limit,
                "remaining": max(0, limit - row["cnt"]),
                "avg_latency_ms": round(row["avg_lat"] or 0),
            }
        return result

    def get_all_usage_today(self) -> list[dict]:
        """Get today's aggregated usage for all active keys (admin view)."""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = self.db.execute(
            """SELECT k.student_name, k.key, k.active,
                      COUNT(u.id) as total_calls,
                      SUM(CASE WHEN u.status='error' THEN 1 ELSE 0 END) as errors
               FROM api_keys k
               LEFT JOIN key_usage u ON k.key = u.api_key AND u.timestamp >= ?
               GROUP BY k.key
               ORDER BY total_calls DESC""",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Internals ──

    def _get_daily_limit(self, api_key: APIKey, endpoint: str) -> int:
        per_key = {
            "chat": api_key.daily_chat,
            "image": api_key.daily_image,
            "tts": api_key.daily_tts,
            "vision": api_key.daily_vision,
            "video": api_key.daily_video,
            "podcast": api_key.daily_podcast,
            "web": api_key.daily_web,
        }
        val = per_key.get(endpoint, 10)
        if val > 0:
            return val

        # Auto-split: global limit / active keys (minimum 1)
        global_limit = GOOGLE_GLOBAL_LIMITS.get(endpoint, 100)
        active = max(1, self.get_active_count())
        return max(1, global_limit // active)

    def get_per_key_limits(self) -> dict:
        """Return current per-key limits (for landing page display)."""
        active = max(1, self.get_active_count())
        return {ep: max(1, gl // active) for ep, gl in GOOGLE_GLOBAL_LIMITS.items()}

    def _get_daily_usage(self, key: str, endpoint: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        row = self.db.execute(
            """SELECT COUNT(*) as cnt FROM key_usage
               WHERE api_key = ? AND endpoint = ? AND timestamp >= ?
               AND status = 'ok'""",
            (key, endpoint, today),
        ).fetchone()
        return row["cnt"] if row else 0

    def _record_usage(self, key: str, endpoint: str, status: str):
        now = datetime.now().isoformat()
        self.db.execute(
            "INSERT INTO key_usage (api_key, endpoint, timestamp, status) VALUES (?,?,?,?)",
            (key, endpoint, now, status),
        )
        self.db.commit()

    def _row_to_key(self, row) -> APIKey:
        # Handle both old DB (no expires_at) and new DB
        try:
            expires_at = row["expires_at"]
        except (IndexError, KeyError):
            expires_at = None

        return APIKey(
            id=row["id"],
            key=row["key"],
            student_name=row["student_name"],
            created_at=row["created_at"],
            expires_at=expires_at,
            active=bool(row["active"]),
            daily_chat=row["daily_chat"],
            daily_image=row["daily_image"],
            daily_tts=row["daily_tts"],
            daily_vision=row["daily_vision"],
            daily_video=row["daily_video"],
            daily_podcast=row["daily_podcast"],
            daily_web=row["daily_web"],
            rpm=row["rpm"],
        )
