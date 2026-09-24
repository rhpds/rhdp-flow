"""FastAPI application factory for RHDP-Flow Web UI."""

from __future__ import annotations

import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Ensure parent dir is on path for rhdp_flow imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def configure_logging() -> None:
    """Set up structured JSON logging when LOG_FORMAT=json, otherwise human-readable."""
    log_format = os.environ.get("LOG_FORMAT", "").lower()
    if log_format == "json":
        try:
            from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore

            handler = logging.StreamHandler()
            handler.setFormatter(JsonFormatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level"},
            ))
            logging.root.handlers = [handler]
            logging.root.setLevel(logging.INFO)
        except ImportError:
            logging.basicConfig(level=logging.INFO)
            logging.getLogger("rhdp_flow.api").warning(
                "python-json-logger not installed; falling back to text logging"
            )
    else:
        logging.basicConfig(level=logging.INFO)


configure_logging()

from api.routes import router

logger = logging.getLogger("rhdp_flow.api")

# Graceful shutdown state
_shutting_down = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    global _shutting_down
    _shutting_down = True
    logger.info("RHDP-Flow API shutting down gracefully")


app = FastAPI(
    title="RHDP-Flow API",
    description="Web API for Red Hat Demo Platform Workshop Automation. Authors: Josh Disraeli, Billy Bethell.",
    version="1.4.2",
    lifespan=lifespan,
)

# Auth startup check — fail closed by default (see ApiKeyGateMiddleware below)
if not os.environ.get("RHDP_API_KEY"):
    if os.environ.get("RHDP_ALLOW_UNAUTHENTICATED", "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "RHDP_API_KEY not set and RHDP_ALLOW_UNAUTHENTICATED=true -- API is UNAUTHENTICATED (local dev only)"
        )
    else:
        logger.error(
            "RHDP_API_KEY not set -- API will refuse all requests with 503. "
            "Set RHDP_API_KEY (or RHDP_ALLOW_UNAUTHENTICATED=true for local dev)."
        )

# ---------------------------------------------------------------------------
# Rate limiting via SlowAPI (shared limiter from api.limiter)
# ---------------------------------------------------------------------------
from api.limiter import limiter as _limiter

if _limiter:
    app.state.limiter = _limiter
    try:
        from slowapi import _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded

        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    except ImportError:
        pass
else:
    logger.warning("Rate limiting disabled (slowapi not available)")

# ---------------------------------------------------------------------------
# CORS — configurable via CORS_ORIGINS env var
# ---------------------------------------------------------------------------
_cors_origins_env = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()] if _cors_origins_env else [
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# CSP headers middleware
# ---------------------------------------------------------------------------
_FRAME_ANCESTORS = os.environ.get("RHDP_FRAME_ANCESTORS", "").strip()

class CSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response: Response = await call_next(request)
        # When RHDP_FRAME_ANCESTORS is set (e.g. the Labagator origin), allow
        # framing from that origin via CSP frame-ancestors and drop the older
        # X-Frame-Options header (frame-ancestors supersedes it in all modern
        # browsers). Without the env var the default is DENY — no change.
        if _FRAME_ANCESTORS:
            frame_ancestors = f"frame-ancestors 'self' {_FRAME_ANCESTORS}; "
        else:
            response.headers["X-Frame-Options"] = "DENY"
            frame_ancestors = ""
        response.headers["Content-Security-Policy"] = (
            f"{frame_ancestors}"
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self';"
            "font-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


app.add_middleware(CSPMiddleware)


# ---------------------------------------------------------------------------
# API-key gate — require X-API-Key on ALL /api endpoints (reads included),
# except health diagnostics and the lightweight pod probe
# and CORS preflight. Fail closed: if RHDP_API_KEY is not set, the API
# refuses to serve (503) rather than running open — UNLESS
# RHDP_ALLOW_UNAUTHENTICATED=true is set for local development. Note: the
# static dashboard shell at "/" and /openapi.json are not gated (a browser
# cannot attach X-API-Key to a page load); the SPA gates itself and every
# /api call it makes is gated here.
# ---------------------------------------------------------------------------
def _unauth_allowed() -> bool:
    return os.environ.get("RHDP_ALLOW_UNAUTHENTICATED", "").strip().lower() in ("1", "true", "yes")


class ApiKeyGateMiddleware(BaseHTTPMiddleware):
    _EXEMPT = {"/api/health", "/api/v1/health", "/api/healthz", "/api/v1/healthz"}

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if request.method != "OPTIONS" and path.startswith("/api") and path not in self._EXEMPT:
            required = os.environ.get("RHDP_API_KEY") or ""
            if not required:
                # Fail closed unless explicitly opted out for local dev.
                if not _unauth_allowed():
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": "Server misconfigured: RHDP_API_KEY is not set. "
                            "Set it, or set RHDP_ALLOW_UNAUTHENTICATED=true for local development."
                        },
                    )
            else:
                provided = request.headers.get("X-API-Key", "")
                if not provided or not hmac.compare_digest(provided, required):
                    return JSONResponse(
                        status_code=403, content={"detail": "Invalid or missing API key"}
                    )
        return await call_next(request)


app.add_middleware(ApiKeyGateMiddleware)

# ---------------------------------------------------------------------------
# API routes — available at /api (primary) and /api/v1 (versioned alias)
# ---------------------------------------------------------------------------
app.include_router(router, prefix="/api")
app.include_router(router, prefix="/api/v1", tags=["v1"])


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def is_shutting_down() -> bool:
    return _shutting_down


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(request: Request, exc: FileNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Static files — serve frontend/dist/ (React build) or web/ (legacy) at root
# ---------------------------------------------------------------------------

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
_web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
_static_dir = _frontend_dist if os.path.isdir(_frontend_dist) else _web_dir
logger.info(f"Static files: {os.path.abspath(_static_dir)}")
if os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
