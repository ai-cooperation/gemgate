"""API authentication middleware.

Routes:
  /v1/*       → Handled by openai_compat router (Bearer token per-key auth)
  /register   → Public (no auth)
  /           → Public (landing page)
  /admin/*    → Protected by admin secret query param
  /api/*      → Legacy internal API (Tailscale trusted or global API_KEY)
  /docs, etc  → Public
"""
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from config import API_KEY

# Tailscale + localhost = trusted
TRUSTED_PREFIXES = ("100.", "127.0.0.1", "::1")

# Paths that skip auth entirely
PUBLIC_PATHS = {"/", "/register", "/api/health", "/api/key-stats", "/docs", "/openapi.json", "/favicon.ico"}
PUBLIC_PREFIXES = ("/v1/", "/my/", "/admin/")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Public endpoints
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # /v1/* and /admin/* handle their own auth
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Static files
        if path.startswith("/dashboard") or path.startswith("/output"):
            return await call_next(request)

        client_ip = request.client.host if request.client else ""

        # Tailscale internal = trusted
        if any(client_ip.startswith(p) for p in TRUSTED_PREFIXES):
            return await call_next(request)

        # External: require global API key for legacy /api/* endpoints
        if not API_KEY:
            return await call_next(request)

        api_key = (
            request.headers.get("X-API-Key")
            or request.query_params.get("api_key")
        )
        if not api_key or api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

        return await call_next(request)
