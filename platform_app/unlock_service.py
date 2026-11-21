"""Adsterra 3-Step Unlock & Token Monetization Service.

Watching 3 ads grants a token balance (default 500,000 tokens). API requests
spend from that balance. A new ad cycle can only be started after the
configured cooldown (default 1 hour).
"""
import time
import uuid
from typing import Dict, Any
from .database import get_db_connection, get_setting


def get_direct_links() -> list:
    raw = get_setting("adsterra_direct_links", "https://www.profitablecpmrate.com/example_direct_link")
    links = [line.strip() for line in raw.replace(",", "\n").split("\n") if line.strip()]
    return links if links else ["https://www.profitablecpmrate.com/example_direct_link"]


def get_next_direct_link(step: int) -> str:
    links = get_direct_links()
    idx = (step) % len(links)
    return links[idx]


def get_cycle_settings() -> Dict[str, Any]:
    """Read the ad-cycle rules from admin settings."""
    return {
        "required_steps": int(get_setting("ads_required_count", "3")),
        "tokens_per_cycle": int(get_setting("tokens_per_cycle", "500000")),
        "cooldown_hours": float(get_setting("ad_cycle_cooldown_hours", "1")),
        "cooldown_seconds": int(float(get_setting("ad_cycle_cooldown_hours", "1")) * 3600),
        "ad_view_seconds": int(get_setting("ad_cooldown_seconds", "10")),
    }


def format_cooldown(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


def get_user_access_status(user: Dict[str, Any]) -> Dict[str, Any]:
    """Token-based access status (replaces the old time-based pass)."""
    now = time.time()
    rules = get_cycle_settings()

    token_balance = int(user.get("token_balance", 0) or 0)
    is_vip = bool(user.get("is_vip", 0))
    is_unlocked = token_balance > 0

    last_cycle = float(user.get("last_ad_cycle_at", 0.0) or 0.0)
    next_cycle_at = last_cycle + rules["cooldown_seconds"] if last_cycle > 0 else 0.0
    cooldown_remaining = max(0, int(next_cycle_at - now))
    can_watch_ads = cooldown_remaining == 0

    return {
        "user_id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "is_vip": is_vip,
        "is_unlocked": is_unlocked,
        "token_balance": token_balance,
        "tokens_per_cycle": rules["tokens_per_cycle"],
        "total_tokens_granted": int(user.get("total_tokens_granted", 0) or 0),
        "total_tokens_used": int(user.get("total_tokens_used", 0) or 0),
        "last_ad_cycle_at": last_cycle,
        "next_ad_cycle_at": next_cycle_at,
        "cooldown_hours": rules["cooldown_hours"],
        "cooldown_remaining_seconds": cooldown_remaining,
        "cooldown_remaining_formatted": format_cooldown(cooldown_remaining),
        "can_watch_ads": can_watch_ads,
        "required_steps": rules["required_steps"],
        "total_ads_watched": int(user.get("total_ads_watched", 0) or 0),
        "total_requests": int(user.get("total_requests", 0) or 0),
        # Legacy alias for older clients that still read this field.
        "remaining_seconds": cooldown_remaining,
    }


def start_or_get_unlock_session(user_id: int) -> Dict[str, Any]:
    conn = get_db_connection()
    rules = get_cycle_settings()
    now = time.time()

    user_row = conn.execute(
        "SELECT token_balance, last_ad_cycle_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    token_balance = int(user_row["token_balance"] or 0) if user_row else 0
    last_cycle = float(user_row["last_ad_cycle_at"] or 0.0) if user_row else 0.0

    # A new 3-ad cycle is only allowed after the cooldown has elapsed.
    next_cycle_at = last_cycle + rules["cooldown_seconds"] if last_cycle > 0 else 0.0
    if next_cycle_at > now:
        remaining = int(next_cycle_at - now)
        conn.close()
        return {
            "can_unlock": False,
            "is_active": False,
            "cooldown_remaining_seconds": remaining,
            "cooldown_remaining_formatted": format_cooldown(remaining),
            "next_unlock_at": next_cycle_at,
            "cooldown_hours": rules["cooldown_hours"],
            "token_balance": token_balance,
            "message": (
                f"You can watch the next {rules['required_steps']} ads in "
                f"{format_cooldown(remaining)} to earn "
                f"{rules['tokens_per_cycle']:,} more tokens."
            ),
        }

    # Reuse a pending session (stale sessions expire after one cooldown window).
    row = conn.execute("""
        SELECT * FROM ad_sessions
        WHERE user_id = ? AND status = 'pending'
        ORDER BY id DESC LIMIT 1
    """, (user_id,)).fetchone()

    if row and (now - row["created_at"]) > max(rules["cooldown_seconds"], 7200):
        conn.execute("UPDATE ad_sessions SET status = 'expired' WHERE id = ?", (row["id"],))
        conn.commit()
        row = None

    if not row:
        session_id = uuid.uuid4().hex
        conn.execute("""
            INSERT INTO ad_sessions
                (user_id, session_id, current_step, required_steps, duration_minutes,
                 tokens_to_grant, status, created_at)
            VALUES (?, ?, 0, ?, 0, ?, 'pending', ?)
        """, (user_id, session_id, rules["required_steps"], rules["tokens_per_cycle"], now))
        conn.commit()
        session = {
            "session_id": session_id,
            "current_step": 0,
            "required_steps": rules["required_steps"],
            "tokens_to_grant": rules["tokens_per_cycle"],
            "status": "pending",
        }
    else:
        session = dict(row)

    conn.close()

    return {
        "session_id": session["session_id"],
        "current_step": session["current_step"],
        "required_steps": session["required_steps"],
        "tokens_to_grant": session.get("tokens_to_grant") or rules["tokens_per_cycle"],
        "ad_url": get_next_direct_link(session["current_step"]),
        "cooldown_seconds": rules["ad_view_seconds"],
        "can_unlock": True,
        "is_active": False,
        "token_balance": token_balance,
    }


def record_ad_click(session_id: str, user_id: int) -> Dict[str, Any]:
    conn = get_db_connection()
    now = time.time()

    row = conn.execute("""
        SELECT * FROM ad_sessions WHERE session_id = ? AND user_id = ? AND status = 'pending'
    """, (session_id, user_id)).fetchone()

    if not row:
        conn.close()
        return {"error": "Invalid or expired session"}

    conn.execute("UPDATE ad_sessions SET step_started_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    conn.close()

    cooldown = int(get_setting("ad_cooldown_seconds", "10"))
    return {
        "success": True,
        "step_started_at": now,
        "cooldown_seconds": cooldown,
    }


def verify_and_advance_ad_step(session_id: str, user_id: int) -> Dict[str, Any]:
    """Verify an ad step atomically.

    A write lock is acquired for the whole check-and-reward sequence so two
    simultaneous verify calls can never grant the cycle reward twice.
    """
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM ad_sessions WHERE session_id = ? AND user_id = ? AND status = 'pending'
        """, (session_id, user_id)).fetchone()

        if not row:
            conn.rollback()
            return {"error": "Invalid or expired session"}

        session = dict(row)
        now = time.time()
        rules = get_cycle_settings()

        step_started_at = session.get("step_started_at")
        cooldown = rules["ad_view_seconds"]

        # Minimum view time verification (anti-bypass)
        if not step_started_at or (now - step_started_at < (cooldown - 1)):
            conn.rollback()
            remaining_wait = max(1, int(cooldown - (now - (step_started_at or now))))
            return {
                "error": f"Please watch the ad for at least {cooldown} seconds. ({remaining_wait}s remaining)",
                "wait_seconds": remaining_wait,
            }

        new_step = session["current_step"] + 1
        required_steps = session["required_steps"]

        # Claim this exact step; a concurrent verifier will affect 0 rows.
        if new_step >= required_steps:
            claimed = conn.execute("""
                UPDATE ad_sessions SET current_step = ?, status = 'completed'
                WHERE id = ? AND status = 'pending' AND current_step = ?
            """, (new_step, session["id"], session["current_step"]))
            if claimed.rowcount != 1:
                conn.rollback()
                return {"error": "This ad cycle was already completed"}

            tokens_to_grant = int(session.get("tokens_to_grant") or rules["tokens_per_cycle"])
            next_cycle_at = now + rules["cooldown_seconds"]

            conn.execute("UPDATE users SET total_ads_watched = total_ads_watched + 1 WHERE id = ?", (user_id,))
            conn.execute("""
                UPDATE users
                SET token_balance = token_balance + ?,
                    total_tokens_granted = total_tokens_granted + ?,
                    last_ad_cycle_at = ?
                WHERE id = ?
            """, (tokens_to_grant, tokens_to_grant, now, user_id))
            conn.commit()

            user_row = conn.execute(
                "SELECT token_balance FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            new_balance = int(user_row["token_balance"] or 0) if user_row else tokens_to_grant

            return {
                "success": True,
                "completed": True,
                "message": f"Congratulations! {tokens_to_grant:,} tokens have been added to your balance.",
                "tokens_added": tokens_to_grant,
                "token_balance": new_balance,
                "next_ad_cycle_at": next_cycle_at,
                "cooldown_hours": rules["cooldown_hours"],
                "remaining_seconds": rules["cooldown_seconds"],
                "unlocked": True,
            }

        claimed = conn.execute("""
            UPDATE ad_sessions SET current_step = ?, step_started_at = NULL
            WHERE id = ? AND status = 'pending' AND current_step = ?
        """, (new_step, session["id"], session["current_step"]))
        if claimed.rowcount != 1:
            conn.rollback()
            return {"error": "This ad step was already verified"}

        conn.execute("UPDATE users SET total_ads_watched = total_ads_watched + 1 WHERE id = ?", (user_id,))
        conn.commit()

        return {
            "success": True,
            "completed": False,
            "current_step": new_step,
            "required_steps": required_steps,
            "next_ad_url": get_next_direct_link(new_step),
            "message": f"Ad {new_step} of {required_steps} verified! Continue to Ad {new_step + 1}.",
        }
    finally:
        conn.close()
