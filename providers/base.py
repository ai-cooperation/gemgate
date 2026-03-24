"""Abstract base class for all AI service providers."""
from abc import ABC, abstractmethod
from typing import Optional


class JobResult:
    def __init__(self, success: bool, message: str,
                 output_path: Optional[str] = None,
                 output_base64: Optional[str] = None,
                 generation_time: Optional[float] = None,
                 provider: str = "",
                 metadata: Optional[dict] = None):
        self.success = success
        self.message = message
        self.output_path = output_path
        self.output_base64 = output_base64
        self.generation_time = generation_time
        self.provider = provider
        self.metadata = metadata or {}


class BaseProvider(ABC):
    name: str = ""
    category: str = ""  # "image", "video", "podcast", "tts", "stt"
    chrome_profile: Optional[str] = None
    requires_chrome: bool = True

    @abstractmethod
    async def execute(self, params: dict) -> JobResult:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass
