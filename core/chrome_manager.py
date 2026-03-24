"""Chrome Manager stub — not used in GemGate (Firefox-only)"""
class ChromeManager:
    async def ensure_running(self, profile): return True
    async def close_all(self): pass
    async def get_all_status(self): return {}
