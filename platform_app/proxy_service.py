"""Gemini Web2API Proxy & Gateway Service with Quota & Expiry Control."""
import os
import time
import json
import uuid
from typing import Dict, Any, Optional, Tuple, AsyncGenerator
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse

from gemini_web2api.config import CONFIG
from gemini_web2api.models import MODELS, resolve_model
from gemini_web2api.gemini import (
    generate, generate_stream, log, request_cookie_index, request_cookie_override,
)
from .database import get_db_connection, get_setting, get_user_cookie, get_user_api_key
from gemini_web2api.tools import (
    messages_to_prompt, 
    parse_tool_calls, 
    google_contents_to_prompt, 
    parse_google_function_calls
)
from gemini_web2api.multimodal import fetch_image_bytes, detect_image_mime, upload_image

COOKIE_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "gemini_cookie.txt"))

def sync_gemini_config():
    """Sync dynamic settings from SQLite to gemini_web2api CONFIG."""
    bl = get_setting("gemini_bl", "boq_assistant-bard-web-server_20260716.08_p0")
    proxy = get_setting("gemini_proxy", "")
    default_model = get_setting("default_model", "gemini-3.7-flash")
    cookie_str = get_setting("gemini_cookie", "").strip()
    cookies = [line.strip() for line in cookie_str.splitlines() if line.strip()]

    CONFIG["gemini_bl"] = bl
    CONFIG["default_model"] = default_model
    CONFIG["proxy"] = proxy if proxy else None
    CONFIG["cookie_pool"] = cookies

    if cookies:
        os.makedirs(os.path.dirname(COOKIE_FILE_PATH), exist_ok=True)
        with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(cookies[0])
        CONFIG["cookie_file"] = COOKIE_FILE_PATH

def authenticate_api_key(request: Request) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Extract and validate API Key from:
    1. Header `Authorization: Bearer sk-...`
    2. Header `x-api-key: sk-...`
    3. Header `x-goog-api-key: sk-...`
    4. Query param `?key=sk-...`
    
    Validates key active state and the user's remaining token balance.
    """
    key_secret = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key_secret = auth[7:].strip()
    elif request.headers.get("x-api-key"):
        key_secret = request.headers.get("x-api-key", "").strip()
    elif request.headers.get("x-goog-api-key"):
        key_secret = request.headers.get("x-goog-api-key", "").strip()
    elif "key" in request.query_params:
        key_secret = request.query_params.get("key", "").strip()

    if not key_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "Missing API Key. Provide via 'Authorization: Bearer <sk-...>' or 'x-api-key'.", "type": "auth_error"}}
        )

    conn = get_db_connection()
    row = conn.execute("""
        SELECT k.*, u.id as owner_id, u.role, u.is_active as user_active, u.is_vip,
               u.token_balance, u.email
        FROM api_keys k
        JOIN users u ON k.user_id = u.id
        WHERE k.key_secret = ?
    """, (key_secret,)).fetchone()
    conn.close()

    if not row or not row["is_active"] or not row["user_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "Invalid or inactive API Key.", "type": "auth_error"}}
        )

    user = dict(row)
    # k.* puts the KEY id in "id"; the actual user id is owner_id. Downstream
    # code (token deduction, cookie lookup) must use the real user id.
    user["id"] = row["owner_id"]

    # Named plan quota + per-user request rate limit.
    from .plans import check_quota
    from .rate_limit import is_allowed as rate_allowed, cleanup as rate_cleanup
    quota = check_quota(int(user["id"]))
    if not quota["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {
                "message": quota["reason"], "type": "quota_exceeded", "code": 429,
                "plan": quota["plan"], "monthly_limit": quota["monthly_limit"],
                "used": quota["used"],
            }},
        )
    allowed, retry = rate_allowed(int(user["id"]))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {
                "message": f"Rate limit reached on your plan. Try again in {retry}s.",
                "type": "rate_limit", "code": 429, "retry_after": retry,
            }},
            headers={"Retry-After": str(retry)},
        )
    if int(time.time()) % 120 == 0:
        rate_cleanup()

    # Strict token-based access: EVERY key (including admin/VIP) needs tokens.
    # There are no time passes and no bypasses.
    token_balance = int(user.get("token_balance", 0) or 0)
    if token_balance <= 0:
        domain = get_setting("site_domain", "ngotra.site")
        unlock_url = f"https://{domain}/dashboard/overview"
        tokens_per_cycle = int(get_setting("tokens_per_cycle", "500000"))
        ads_required = int(get_setting("ads_required_count", "3"))
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": {
                    "message": (
                        f"Your token balance is empty. Please visit {unlock_url} to watch "
                        f"{ads_required} ads and earn {tokens_per_cycle:,} more tokens."
                    ),
                    "type": "no_tokens",
                    "code": 402,
                    "unlock_url": unlock_url,
                    "token_balance": token_balance
                }
            }
        )

    # Security gate: a verified Gemini connection is required for ANY API use.
    from .gemini_connection import connection_state, require_connection_message
    connection = connection_state(int(user["id"]))
    if not connection["connected"] or not connection["valid"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "message": require_connection_message(connection),
                    "type": connection["reason"],
                    "code": 403,
                    "unlock_url": f"https://{get_setting('site_domain', 'ngotra.site')}/dashboard/settings",
                }
            }
        )

    # A user's own connected cookie takes priority; otherwise spread users
    # across the shared cookie pool. The cookie is carried on the returned user
    # dict (and also via ContextVar for sync callers) because contextvars set in
    # a sync helper are not guaranteed to reach an async route handler.
    user_cookie = get_user_cookie(int(user["id"]))
    # If the user's own Gemini account is cooling down (rate-limited), use a
    # healthy account from the shared pool so requests keep working.
    from gemini_web2api.account_health import health, pick_healthy
    if user_cookie and not health(user_cookie)["ok"]:
        pool = CONFIG.get("cookie_pool") or []
        fallback = pick_healthy(pool)
        if fallback:
            log(f"User {user['id']} Gemini account cooling down; using shared pool fallback")
            user_cookie = fallback
    user["gemini_cookie_override"] = user_cookie or ""
    try:
        from .database import get_db_connection as _gdb
        _c = _gdb()
        _r = _c.execute("SELECT gemini_auth_user FROM users WHERE id = ?", (int(user["id"]),)).fetchone()
        _c.close()
        user["gemini_auth_user"] = (_r["gemini_auth_user"] if _r else "") or ""
    except Exception:
        user["gemini_auth_user"] = ""
    try:
        from .database import get_db_connection as _gdb
        _c = _gdb()
        _r = _c.execute("SELECT gemini_xsrf_token FROM users WHERE id = ?", (int(user["id"]),)).fetchone()
        _c.close()
        user["gemini_xsrf_token"] = (_r["gemini_xsrf_token"] if _r else "") or ""
    except Exception:
        user["gemini_xsrf_token"] = ""

    # If the account index is unknown, probe Gemini once and cache the working
    # one. Without this, a secondary-account session returns empty responses.
    if user_cookie and not user["gemini_auth_user"]:
        try:
            from .auth_probe import ensure_auth_user
            user["gemini_auth_user"] = ensure_auth_user(int(user["id"]), user_cookie)
        except Exception:
            pass
    user["gemini_api_key"] = get_user_api_key(int(user["id"])) or ""

    # If the user connected their Google account via OAuth, use the official
    # Gemini API with their access token instead of the shared web session.
    try:
        from .gemini_oauth import get_access_token
        user["gemini_oauth_token"] = get_access_token(int(user["id"])) or ""
    except Exception:
        user["gemini_oauth_token"] = ""
    if user_cookie:
        request_cookie_override.set(user_cookie)
    else:
        request_cookie_override.set(None)

    cookie_pool = CONFIG.get("cookie_pool") or []
    if cookie_pool:
        request_cookie_index.set(int(user["id"]) % len(cookie_pool))

    return user, {"key_id": row["id"], "key_prefix": row["key_prefix"], "key_secret": row["key_secret"]}

def record_api_usage(user_id: int, key_id: int, key_prefix: str, endpoint: str, model: str, status_code: int, duration_ms: float, tokens: int, ip: str):
    conn = get_db_connection()
    now = time.time()
    conn.execute("""
        INSERT INTO api_logs (user_id, key_prefix, endpoint, model, status_code, duration_ms, tokens_estimated, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, key_prefix, endpoint, model, status_code, duration_ms, tokens, ip, now))
    conn.execute("""
        UPDATE api_keys 
        SET total_requests = total_requests + 1, 
            total_tokens = total_tokens + ?, 
            last_used_at = ? 
        WHERE id = ?
    """, (tokens, now, key_id))
    # Spend the estimated tokens from the user's balance (never below zero).
    conn.execute("""
        UPDATE users
        SET token_balance = MAX(0, token_balance - ?),
            total_tokens_used = total_tokens_used + ?,
            total_requests = total_requests + 1
        WHERE id = ? AND token_balance > 0
    """, (tokens, tokens, user_id))
    conn.commit()
    conn.close()

    from .plans import add_monthly_usage
    add_monthly_usage(user_id, tokens)

def _upload_images_safe(images: list) -> list:
    if not images:
        return None
    file_refs = []
    for item in images:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = mime or "image/png"
        if not data:
            raise RuntimeError("image fetch failed")
        mime = detect_image_mime(data, mime or "image/png")
        try:
            ref = upload_image(data, "image.png", mime or "image/png")
            file_refs.append(ref)
        except Exception as e:
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None
