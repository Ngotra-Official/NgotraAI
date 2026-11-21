"""Admin Management Endpoints."""
import time
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from ..database import get_db_connection, get_all_settings, update_settings, get_setting
from ..auth import get_current_admin
from ..proxy_service import sync_gemini_config

router = APIRouter(prefix="/api/admin", tags=["admin"])

class GrantTokensRequest(BaseModel):
    tokens: int = 500000

class UpdateSettingsRequest(BaseModel):
    settings: Dict[str, Any]

@router.get("/stats")
def admin_stats(admin: dict = Depends(get_current_admin)):
    conn = get_db_connection()
    now = time.time()
    today_start = now - (now % 86400)

    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    total_keys = conn.execute("SELECT COUNT(*) as c FROM api_keys").fetchone()["c"]
    total_requests = conn.execute("SELECT COUNT(*) as c FROM api_logs").fetchone()["c"]
    total_ads_watched = conn.execute("SELECT SUM(total_ads_watched) as c FROM users").fetchone()["c"] or 0
    
    active_unlocks = conn.execute("""
        SELECT COUNT(*) as c FROM users
        WHERE token_balance > 0 OR is_vip = 1
    """).fetchone()["c"]

    today_requests = conn.execute("""
        SELECT COUNT(*) as c FROM api_logs WHERE created_at >= ?
    """, (today_start,)).fetchone()["c"]

    today_unlocks = conn.execute("""
        SELECT COUNT(*) as c FROM ad_sessions WHERE status = 'completed' AND created_at >= ?
    """, (today_start,)).fetchone()["c"]

    conn.close()

    return {
        "total_users": total_users,
        "active_unlocks": active_unlocks,
        "total_keys": total_keys,
        "total_requests": total_requests,
        "total_ads_watched": total_ads_watched,
        "today_requests": today_requests,
        "today_unlocks": today_unlocks
    }

@router.get("/users")
def list_users(search: Optional[str] = None, admin: dict = Depends(get_current_admin)):
    conn = get_db_connection()
    cooldown_seconds = int(float(get_setting("ad_cycle_cooldown_hours", "1")) * 3600)

    if search:
        pattern = f"%{search.strip().lower()}%"
        rows = conn.execute("""
            SELECT id, email, role, is_active, is_vip, token_balance, total_tokens_granted,
                   total_tokens_used, last_ad_cycle_at, total_ads_watched, total_requests,
                   created_at, last_login_at
            FROM users
            WHERE email LIKE ?
            ORDER BY id DESC
            LIMIT 100
        """, (pattern,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, email, role, is_active, is_vip, token_balance, total_tokens_granted,
                   total_tokens_used, last_ad_cycle_at, total_ads_watched, total_requests,
                   created_at, last_login_at
            FROM users
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()
    conn.close()

    users = []
    for r in rows:
        u = dict(r)
        balance = int(u["token_balance"] or 0)
        if balance > 0:
            u["status_label"] = f"{balance:,} tokens" + (" (VIP)" if u["is_vip"] else "")
        else:
            u["status_label"] = "No tokens"
        u["remaining_seconds"] = 0
        cycle_at = float(u["last_ad_cycle_at"] or 0.0)
        u["next_ad_cycle_at"] = cycle_at + cooldown_seconds if cycle_at else 0.0
        users.append(u)

    return {"users": users}

@router.post("/users/{user_id}/extend")
def grant_tokens(user_id: int, req: GrantTokensRequest, admin: dict = Depends(get_current_admin)):
    """Manually grant tokens to a user (replaces the old add-hours endpoint)."""
    conn = get_db_connection()
    user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    tokens = max(0, int(req.tokens))
    conn.execute("""
        UPDATE users
        SET token_balance = token_balance + ?,
            total_tokens_granted = total_tokens_granted + ?
        WHERE id = ?
    """, (tokens, tokens, user_id))
    conn.commit()
    new_balance = conn.execute(
        "SELECT token_balance FROM users WHERE id = ?", (user_id,)
    ).fetchone()["token_balance"]
    conn.close()

    return {"success": True, "message": f"Granted {tokens:,} tokens. New balance: {new_balance:,}"}

@router.post("/users/{user_id}/reset")
def reset_user_pass(user_id: int, admin: dict = Depends(get_current_admin)):
    conn = get_db_connection()
    conn.execute("UPDATE users SET token_balance = 0, access_expires_at = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "User token balance reset to 0"}

@router.post("/users/{user_id}/status")
def toggle_status(user_id: int, admin: dict = Depends(get_current_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot disable your own admin account")

    conn = get_db_connection()
    user = conn.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    new_active = 0 if user["is_active"] else 1
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_active, user_id))
    conn.commit()
    conn.close()
    return {"success": True, "is_active": bool(new_active)}

@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(get_current_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account")

    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "User deleted"}

@router.get("/settings")
def get_settings_endpoint(admin: dict = Depends(get_current_admin)):
    return {"settings": get_all_settings()}

@router.post("/settings")
def save_settings_endpoint(req: UpdateSettingsRequest, admin: dict = Depends(get_current_admin)):
    update_settings(req.settings)
    sync_gemini_config()
    return {"success": True, "message": "Settings updated and Gemini config re-synchronized"}

@router.get("/logs")
def view_logs(limit: int = 50, admin: dict = Depends(get_current_admin)):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT l.*, u.email
        FROM api_logs l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.id DESC
        LIMIT ?
    """, (min(limit, 200),)).fetchall()
    conn.close()
    return {"logs": [dict(r) for r in rows]}
