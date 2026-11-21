"""User management endpoints: API Keys, Usage stats, Access Status."""
import time
import secrets
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from ..database import (
    get_db_connection,
    set_user_cookie, get_user_cookie, clear_user_cookie,
)
from ..auth import get_current_user, get_password_hash, verify_password
from ..unlock_service import get_user_access_status

router = APIRouter(prefix="/api/user", tags=["user"])

class CreateKeyRequest(BaseModel):
    name: Optional[str] = "Default Key"

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class CookieRequest(BaseModel):
    cookie: str
    auth_user: str = ""


def _cookie_status(user_row) -> dict:
    cookie_at = float(user_row["gemini_cookie_at"] or 0.0)
    return {
        "connected": bool(user_row["gemini_cookie"]),
        "connected_at": cookie_at or None,
    }

@router.get("/status")
def user_status(current_user: dict = Depends(get_current_user)):
    # Refetch fresh user data from DB
    conn = get_db_connection()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    conn.close()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    return get_user_access_status(dict(user_row))

@router.get("/cookie")
def get_cookie_status(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_cookie, gemini_cookie_at FROM users WHERE id = ?", (current_user["id"],)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    data = _cookie_status(row)
    # Use the cached, fail-tolerant gate instead of a live Gemini call on every
    # page load. A transient upstream failure must never "disconnect" a user.
    from ..gemini_connection import connection_state
    state = connection_state(current_user["id"])
    data["connection"] = {
        "valid": state["valid"],
        "state": state["reason"],
        "detail": (
            "Connected and working." if state["valid"] else
            "Connect your Gemini account first." if state["reason"] == "not_connected" else
            "Connection is not working. Please reconnect."
        ),
    }
    return data


@router.post("/cookie")
def save_cookie(req: CookieRequest, current_user: dict = Depends(get_current_user)):
    """Store a Gemini web-session cookie supplied by the browser connector."""
    cookie = (req.cookie or "").strip()
    lowered = cookie.lower()
    if not cookie or "=" not in cookie or not any(
        k in lowered for k in ("sapisid", "1psid", "3psid", "sid=", "hsid", "ssid")
    ):
        raise HTTPException(status_code=400, detail="Invalid cookie format.")
    set_user_cookie(current_user["id"], cookie, auth_user=req.auth_user)
    return {"connected": True, "message": "Gemini account cookie saved."}


@router.delete("/cookie")
def remove_cookie(current_user: dict = Depends(get_current_user)):
    """Disconnect the Gemini account by removing the stored cookie."""
    clear_user_cookie(current_user["id"])
    return {"connected": False}


@router.post("/cookie/verify")
def verify_cookie(current_user: dict = Depends(get_current_user)):
    """Re-check the stored Gemini cookie right now."""
    from ..gemini_connection import connection_state
    state = connection_state(current_user["id"], force=True)
    result = {
        "valid": state["valid"],
        "state": state["reason"],
        "detail": "Connected and working." if state["valid"] else (
            "Connect your Gemini account first." if state["reason"] == "not_connected"
            else "Connection is not working. Please reconnect."
        ),
    }
    return {"connection": result}


@router.get("/keys")
def list_keys(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, key_prefix, key_secret, name, is_active, total_requests, total_tokens, last_used_at, created_at
        FROM api_keys
        WHERE user_id = ?
        ORDER BY id DESC
    """, (current_user["id"],)).fetchall()
    conn.close()

    return {
        "keys": [dict(r) for r in rows]
    }

@router.post("/keys")
def create_key(req: CreateKeyRequest, current_user: dict = Depends(get_current_user)):
    from ..gemini_connection import connection_state, require_connection_message
    connection = connection_state(current_user["id"])
    if not connection["connected"] or not connection["valid"]:
        raise HTTPException(status_code=403, detail=require_connection_message(connection))

    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) as c FROM api_keys WHERE user_id = ?", (current_user["id"],)).fetchone()["c"]
    if count >= 10:
        conn.close()
        raise HTTPException(status_code=400, detail="Maximum 10 API keys allowed per account.")

    key_secret = f"sk-ngo-{secrets.token_hex(16)}"
    key_prefix = key_secret[:12] + "..."
    now = time.time()
    name = (req.name or "API Key").strip()[:50]

    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO api_keys (user_id, key_prefix, key_secret, name, is_active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (current_user["id"], key_prefix, key_secret, name, now))
    key_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "success": True,
        "key": {
            "id": key_id,
            "name": name,
            "key_prefix": key_prefix,
            "key_secret": key_secret,
            "is_active": 1,
            "created_at": now
        }
    }

@router.delete("/keys/{key_id}")
def revoke_key(key_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    row = conn.execute("SELECT id FROM api_keys WHERE id = ? AND user_id = ?", (key_id, current_user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Key not found")

    conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Key revoked successfully"}

@router.get("/stats")
def user_stats(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    logs = conn.execute("""
        SELECT endpoint, model, status_code, duration_ms, tokens_estimated, created_at
        FROM api_logs
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 10
    """, (current_user["id"],)).fetchall()
    conn.close()

    return {
        "total_requests": user_row["total_requests"],
        "total_ads_watched": user_row["total_ads_watched"],
        "token_balance": user_row["token_balance"],
        "total_tokens_granted": user_row["total_tokens_granted"],
        "total_tokens_used": user_row["total_tokens_used"],
        "recent_activity": [dict(l) for l in logs]
    }

@router.post("/change-password")
def change_password(req: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (current_user["id"],)).fetchone()
    if not verify_password(req.current_password, user["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if len(req.new_password) < 6:
        conn.close()
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    new_hash = get_password_hash(req.new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, current_user["id"]))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Password updated successfully"}
