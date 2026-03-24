"""Pydantic models for API requests and responses."""
from typing import Optional
from pydantic import BaseModel


# === Image ===
class ImageRequest(BaseModel):
    prompt: str
    provider: str = "auto"  # "gemini", "bing", "auto"
    model: str = "auto"  # "pro", "fast", "flow" (Google Flow, free), "auto" (=pro)
    timeout: int = 120
    queue_timeout: Optional[int] = None  # Queue wait time (default: 15s for Chrome providers)

class ImageResponse(BaseModel):
    success: bool
    image_path: Optional[str] = None
    image_base64: Optional[str] = None
    provider_used: Optional[str] = None
    generation_time: Optional[float] = None
    message: str


# === Video ===
class VideoRequest(BaseModel):
    prompt: str
    provider: str = "auto"  # "gemini", "kling", "auto"
    timeout: int = 300

class JobResponse(BaseModel):
    job_id: str
    status: str  # "pending", "running", "completed", "failed"
    poll_url: str
    message: str


# === Podcast ===
class PodcastRequest(BaseModel):
    sources: list  # URLs, file paths, or {"type": "text", "content": "..."}
    topic: str = ""
    timeout: int = 600


# === TTS ===
class TTSRequest(BaseModel):
    text: str
    lang: str = "zh-TW"
    slow: bool = False

class TTSResponse(BaseModel):
    success: bool
    audio_path: Optional[str] = None
    audio_base64: Optional[str] = None
    message: str


# === STT ===
class STTRequest(BaseModel):
    audio_base64: Optional[str] = None
    audio_url: Optional[str] = None

class STTResponse(BaseModel):
    success: bool
    text: Optional[str] = None
    language: Optional[str] = None
    message: str



# === Audio (Music) ===
class AudioRequest(BaseModel):
    prompt: str
    provider: str = "auto"
    timeout: int = 120

class AudioResponse(BaseModel):
    success: bool
    audio_path: Optional[str] = None
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    generation_time: Optional[float] = None
    message: str


# === Admin ===
class ProviderStatus(BaseModel):
    name: str
    category: str
    status: str  # "ready", "busy", "error", "offline"
    chrome_profile: Optional[str] = None
    today_used: int = 0
    daily_limit: int = 0
    remaining: int = 0


class JobStatus(BaseModel):
    job_id: str
    provider: str
    status: str
    prompt: str
    output_path: Optional[str] = None
    output_base64: Optional[str] = None
    generation_time: Optional[float] = None
    message: Optional[str] = None


# === LLM ===
class LLMRequest(BaseModel):
    prompt: str
    system_prompt: str = ""
    provider: str = "auto"  # "groq", "gemini", "auto"
    model: str = "llama-3.3-70b-versatile"
    temperature: float = 0.7
    max_tokens: int = 4096

class LLMResponse(BaseModel):
    success: bool
    content: Optional[str] = None
    provider_used: Optional[str] = None
    generation_time: Optional[float] = None
    message: str


# === Vision ===
class VisionRequest(BaseModel):
    prompt: str
    image_base64: Optional[str] = None
    image_url: Optional[str] = None

class VisionResponse(BaseModel):
    success: bool
    content: Optional[str] = None
    provider_used: Optional[str] = None
    generation_time: Optional[float] = None
    message: str


# === Web Fetch ===
class WebFetchRequest(BaseModel):
    url: str
    level: str = "auto"  # "rss", "http", "browser", "auto"
    timeout: int = 30

class WebFetchResponse(BaseModel):
    success: bool
    content: Optional[str] = None
    generation_time: Optional[float] = None
    message: str


# === Photoshoot (Pomelli) ===
class PhotoshootRequest(BaseModel):
    image_path: str = ""
    image_base64: str = ""
    aspect_ratio: str = "story"  # "story" (9:16), "square" (1:1), "landscape", "portrait"
    templates: list = []  # empty = auto-select all
    timeout: int = 300

class PhotoshootResponse(BaseModel):
    success: bool
    image_path: Optional[str] = None
    image_base64: Optional[str] = None
    all_images: list = []
    zip_path: Optional[str] = None
    count: int = 0
    provider_used: Optional[str] = None
    generation_time: Optional[float] = None
    message: str
