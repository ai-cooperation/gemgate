"""GemGate — Google-only AI Gateway Configuration"""
import os

# Server
HUB_PORT = int(os.environ.get("GEMGATE_PORT", "8760"))
HUB_BIND = os.environ.get("GEMGATE_BIND", "0.0.0.0")
API_KEY = os.environ.get("GEMGATE_API_KEY", "")

# Chrome CDP ports (legacy compat — not used in Firefox mode)
CHROME_PORTS = {}

# Provider daily limits (Google-only)
DAILY_LIMITS = {
    "gemini_image": 999999,
    "gemini_image_pro": 100,
    "gemini_video": 3,
    "notebooklm_video": 20,
    "notebooklm": 20,
    "google_tts": 999999,
    "gemini_chat": 999999,
    "web_fetcher": 999999,
    "gemini_audio": 999999,
    "flow_image": 999999,
}

# Chrome tier config (legacy compat)
TIER1_PROFILES = []
TIER2_PROFILES = []

# Fallback chains (Google-only) — dual account round-robin
FALLBACK_CHAINS = {
    "image": ["gemini_image", "flow_image"],
    "video": ["notebooklm_video", "gemini_video"],
    "llm": ["gemini_chat"],
    "tts": ["google_tts"],
}

# Output directories
OUTPUT_BASE = "/opt/gemgate/output"
OUTPUT_DIRS = {
    "images": f"{OUTPUT_BASE}/images",
    "videos": f"{OUTPUT_BASE}/videos",
    "audio": f"{OUTPUT_BASE}/audio",
    "podcasts": f"{OUTPUT_BASE}/podcasts",
}

# State
STATE_DIR = "/opt/gemgate/state"
QUOTA_DB = f"{STATE_DIR}/quota.db"

# Idle timeout
CHROME_IDLE_TIMEOUT = 600

# Firefox browser backend — dual account profiles
FIREFOX_COOKIES_DB = ""

# Account 1 profiles
FIREFOX_QUEUE_KEYS = {
    "gemini": "firefox-gemini",
    "gemini-chat": "firefox-gemini-chat",
    "notebooklm": "firefox-notebooklm",
    "gemini-audio": "firefox-gemini-audio",
    "flow": "firefox-flow",
}

# Account 2 profiles (backup, optional)
FIREFOX_QUEUE_KEYS_2 = {
    "gemini": "firefox-gemini-2",
    "gemini-chat": "firefox-gemini-chat-2",
}

# Round-robin groups: providers that can use both accounts
DUAL_ACCOUNT_PROFILES = {
    "gemini_image": ["firefox-gemini", "firefox-gemini-2"],
    "gemini_chat": ["firefox-gemini-chat", "firefox-gemini-chat-2"],
}

FIREFOX_IDLE_TIMEOUT = 600
