"""Common utilities for automations."""
import logging
logger = logging.getLogger("gemgate.automations")

async def tg_send(msg: str):
    """Telegram notification stub — not used in GemGate demo."""
    logger.info(f"TG stub: {msg[:80]}...")
