"""Connection gate for the Gemini account (cached, fail-tolerant).

No verified connection -> no API keys, no API access.
A previously verified session is trusted for a long TTL and is NOT
re-checked on every request. Live checks only happen on "Check connection"
or when the trust period has expired. Transient network errors never lock out
a previously working account.
"""
import time
import threading

from .database import (
    get_db_connection, get_user_cookie, get_cookie_verified_at, mark_cookie_verified,
)

# Trust a verified session for 12 hours before contacting Google again.
_VALIDATION_TTL = 12 * 3600
# If a live check fails with a network/server error, keep trusting the last
# known-good verification for this long instead of blocking the user.
_GRACE_TTL = 24 * 3600

_cache = {}            # user_id -> (checked_at, valid)
_lock = threading.Lock()


def _cached(user_id: int):
    with _lock:
        entry = _cache.get(user_id)
    if entry and time.time() - entry[0] < _VALIDATION_TTL:
        return entry[1]
    return None


def _store(user_id: int, valid: bool):
    with _lock:
        _cache[user_id] = (time.time(), valid)


def invalidate(user_id: int):
    with _lock:
        _cache.pop(user_id, None)


def connection_state(user_id: int, force: bool = False) -> dict:
    """Return {"connected", "valid", "reason"} for a user's Gemini account."""
    cookie = get_user_cookie(user_id)
    try:
        from .database import get_db_connection as _gdb
        _c = _gdb()
        _r = _c.execute("SELECT gemini_auth_user FROM users WHERE id = ?", (user_id,)).fetchone()
        _c.close()
        auth_user = (_r["gemini_auth_user"] if _r else "") or ""
    except Exception:
        auth_user = ""
    if not cookie:
        invalidate(user_id)
        return {"connected": False, "valid": False, "reason": "not_connected"}

    verified_at = get_cookie_verified_at(user_id)
    now = time.time()

    # Fast path: in-process cache from a prior check.
    if not force:
        cached = _cached(user_id)
        if cached is not None:
            return {"connected": True, "valid": cached,
                    "reason": "ok" if cached else "invalid"}
        # Persisted verification from save time or a prior check: trust it
        # without touching the network at all.
        if verified_at and now - verified_at < _VALIDATION_TTL:
            _store(user_id, True)
            return {"connected": True, "valid": True, "reason": "ok"}

    # Slow path: actual live verification (explicit check or TTL expired).
    try:
        from gemini_web2api.gemini import validate_cookie
        result = validate_cookie(cookie)
        valid = bool(result.get("valid"))
        state = result.get("state", "")
    except Exception:
        valid, state = False, "error"

    if valid:
        mark_cookie_verified(user_id, True)
        _store(user_id, True)
        return {"connected": True, "valid": True, "reason": "ok"}

    # Network/upstream error: do NOT lock out a previously working account.
    if state == "error" and verified_at and now - verified_at < _GRACE_TTL:
        _store(user_id, True)
        return {"connected": True, "valid": True, "reason": "ok"}

    # Clear evidence that the session is no longer signed in.
    mark_cookie_verified(user_id, False)
    _store(user_id, False)
    return {"connected": True, "valid": False, "reason": "invalid"}


def require_connection_message(state: dict) -> str:
    if state["reason"] == "not_connected":
        return (
            "Please verify your Gemini account first. Go to Dashboard → Settings → "
            "Connect Gemini Account, then click Verify & Connect."
        )
    return (
        "Your Gemini connection is not working. Please reconnect in Dashboard → "
        "Settings → Connect Gemini Account, then try again."
    )
