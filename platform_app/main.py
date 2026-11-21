"""FastAPI Main Entrypoint for Ngotra AI Web App & API Gateway Platform."""
import os
import io
import zipfile
import time
import hashlib
import collections
from typing import Optional
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .database import init_db, get_setting, cleanup_old_data
from .auth import get_current_user_optional
from .proxy_service import sync_gemini_config
from .routes import (
    auth_routes,
    user_routes,
    unlock_routes,
    admin_routes,
    openai_routes,
    public_routes,
    gemini_oauth_routes,
)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(
    title="Ngotra AI Platform",
    description="High-Speed OpenAI-compatible Gemini API Gateway with Adsterra Monetization",
    version="2.0.0"
)

# ---------------------------------------------------------------------------
# Rate Limiter — sliding window, dict-based, no extra libraries
# ---------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe (GIL-protected) sliding-window rate limiter."""

    def __init__(self):
        # { (ip, endpoint_key): deque([timestamp, ...]) }
        self._windows: dict = {}

    def _key(self, ip: str, endpoint: str) -> tuple:
        return (ip, endpoint)

    def is_allowed(self, ip: str, endpoint: str, max_requests: int, window_seconds: int) -> bool:
        """Return True if the request is within the allowed rate."""
        now = time.time()
        k = self._key(ip, endpoint)
        if k not in self._windows:
            self._windows[k] = collections.deque()
        dq = self._windows[k]
        cutoff = now - window_seconds
        # Drop timestamps outside the window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_requests:
            return False
        dq.append(now)
        return True

    def cleanup(self, max_idle_sec: int = 3600):
        """Drop windows with no recent traffic so memory stays bounded."""
        now = time.time()
        cutoff = now - max_idle_sec
        for k, dq in list(self._windows.items()):
            if not dq or dq[-1] < cutoff:
                del self._windows[k]


_last_rate_cleanup = [0.0]


_rate_limiter = RateLimiter()

# Rate-limit rules: (path_prefix_or_exact, max_requests, window_seconds, endpoint_key)
_RATE_RULES = [
    ("/api/auth/login",    "/api/auth/login",    10, 300),   # 10/5 min
    ("/api/auth/register", "/api/auth/register", 5,  600),   # 5/10 min
    ("/api/unlock",        "/api/unlock",         30, 60),    # 30/min
]
_V1_RULE = ("/v1/", "/v1/*", 60, 60)                         # 60/min

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    client_ip = (request.client.host if request.client else "unknown")

    # Periodically drop stale rate-limit windows to keep memory bounded.
    now = time.time()
    if now - _last_rate_cleanup[0] > 120:
        _last_rate_cleanup[0] = now
        _rate_limiter.cleanup()

    # API requests are limited per API key (not per IP) so users behind the
    # same NAT do not share a single quota.
    identity = client_ip
    if path.startswith("/v1/"):
        auth = request.headers.get("Authorization", "")
        key = ""
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
        key = key or request.headers.get("x-api-key", "") or request.query_params.get("key", "")
        if key:
            identity = "key:" + hashlib.sha256(key.encode()).hexdigest()[:16]

    # Check specific endpoint rules first
    for path_prefix, endpoint_key, max_req, window in _RATE_RULES:
        if path.startswith(path_prefix):
            if not _rate_limiter.is_allowed(identity, endpoint_key, max_req, window):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down."},
                    headers={"Retry-After": str(window)},
                )
            break  # only apply the first matching rule
    else:
        # Apply /v1/* rule for any unmatched /v1/ path
        if path.startswith(_V1_RULE[0]):
            if not _rate_limiter.is_allowed(identity, _V1_RULE[1], _V1_RULE[2], _V1_RULE[3]):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "API rate limit exceeded (60 req/min). Please slow down."},
                    headers={"Retry-After": "60"},
                )

    return await call_next(request)


# ---------------------------------------------------------------------------
# CORS — split into two layers
#   • /v1/* — permissive for external AI agents (no credentials)
#   • /api/* — cookie-based, same-site only
# ---------------------------------------------------------------------------

# Layer 1: Permissive for /v1/* API endpoints (agents, external tools)
# allow_credentials=False is required when allow_origins=["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# Layer 2: Restrictive for /api/* cookie-based auth endpoints
# FastAPI evaluates middlewares last-added-first, so this executes before Layer 1.
# We achieve path-scoped behaviour by mounting a sub-application, but since
# CORSMiddleware applies globally we use a custom middleware to override the
# headers for /api/* routes.
@app.middleware("http")
async def api_cors_override(request: Request, call_next):
    """For /api/* paths, restrict CORS to same-site (no wildcard credentials)."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        origin = request.headers.get("origin", "")
        # Only reflect the origin if it looks like our own domain or localhost
        allowed_origins = {
            "https://ngotra.site",
            "http://ngotra.site",
            "http://localhost",
            "http://127.0.0.1",
        }
        if origin in allowed_origins or origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            # Strip wildcard header set by the global middleware for /api/ paths
            if "Access-Control-Allow-Origin" in response.headers:
                del response.headers["Access-Control-Allow-Origin"]
            if "Access-Control-Allow-Credentials" in response.headers:
                del response.headers["Access-Control-Allow-Credentials"]
    return response


# ---------------------------------------------------------------------------
# Security Headers Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # Ad networks load scripts/iframes from many domains; allow external
        # http(s) sources so saved Adsterra codes actually render.
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https: http: data:; "
        "frame-src 'self' https: http:; "
        "child-src 'self' https: http:; "
        "style-src 'self' 'unsafe-inline' https: http: fonts.googleapis.com fonts.gstatic.com; "
        "img-src 'self' data: *; "
        "connect-src 'self' *; "
        "font-src 'self' fonts.gstatic.com data:;"
    )
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["X-DNS-Prefetch-Control"] = "off"
    return response


# Mount Routers
app.include_router(public_routes.router)
app.include_router(auth_routes.router)
app.include_router(user_routes.router)
app.include_router(unlock_routes.router)
app.include_router(admin_routes.router)
app.include_router(openai_routes.router)
app.include_router(gemini_oauth_routes.router)

# Mount Static if exists
if os.path.exists(STATIC_DIR):
    app.mount("/static_assets", StaticFiles(directory=STATIC_DIR), name="static")

@app.on_event("startup")
def on_startup():
    init_db()
    cleanup_old_data()
    sync_gemini_config()
    print("==================================================")
    print(" Ngotra AI Platform Started (ngotra.site) ")
    print(" Default Admin: admin@ngotra.site / Admin@Ngotra2026!")
    print("==================================================")

# Static Logo Helpers (no-cache so logo updates show immediately)
def _logo_response(filename: str):
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return HTMLResponse("", status_code=404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

@app.get("/static/logo.png")
def get_logo(request: Request):
    # All logo variants share this URL so older running servers (without the
    # newer routes) still serve a real image instead of a broken one.
    asset = request.query_params.get("asset", "favicon")
    files = {
        "favicon": "logo.png",
        "mark": "logo-mark.png",
        "lockup": "logo-lockup.png",
    }
    return _logo_response(files.get(asset, "logo.png"))

@app.get("/static/logo-mark.png")
def get_logo_mark():
    return _logo_response("logo-mark.png")

@app.get("/static/logo-lockup.png")
def get_logo_lockup():
    return _logo_response("logo-lockup.png")

@app.get("/favicon.ico")
def favicon():
    return _logo_response("logo.png")

@app.get("/static/gemini-connector.zip")
def download_connector():
    """Serve the obfuscated extension ZIP built by build_extension.py."""
    zip_path = os.path.join(BASE_DIR, "gemini-connector.zip")
    if os.path.exists(zip_path):
        with open(zip_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="gemini-connector.zip"',
                "Cache-Control": "no-store",
            },
        )
    # Fallback: build on the fly from the readable source if not pre-built.
    ext_dir = os.path.join(BASE_DIR, "gemini-cookie-sync-extension")
    if not os.path.isdir(ext_dir):
        return HTMLResponse("", status_code=404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in os.listdir(ext_dir):
            file_path = os.path.join(ext_dir, name)
            if os.path.isfile(file_path):
                zf.write(file_path, arcname=name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="gemini-connector.zip"',
            "Cache-Control": "no-store",
        },
    )


def _render_template(name: str) -> HTMLResponse:
    """Render a page without caching it in the browser."""
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


# HTML Web Pages
@app.get("/", response_class=HTMLResponse)
def index_page():
    return _render_template("index.html")

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return _render_template("privacy.html")

@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/dashboard/{subpath:path}", response_class=HTMLResponse)
def dashboard_page(request: Request, subpath: str = "", current_user: Optional[dict] = Depends(get_current_user_optional)):
    # Server-side guard: anonymous visitors never receive the dashboard shell.
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
    return _render_template("dashboard.html")

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/{subpath:path}", response_class=HTMLResponse)
def admin_page(request: Request, subpath: str = "", current_user: Optional[dict] = Depends(get_current_user_optional)):
    # Server-side guard: only admins may receive the admin shell.
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
    if current_user.get("role") != "admin":
        return RedirectResponse(url="/dashboard", status_code=303)
    return _render_template("admin.html")

@app.get("/unlock")
def unlock_redirect(current_user: Optional[dict] = Depends(get_current_user_optional)):
    if not current_user:
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "Ngotra AI Gateway",
        "domain": get_setting("site_domain", "ngotra.site")
    }
