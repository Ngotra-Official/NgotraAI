"""Cached connection gate: no repeated live checks, graceful on network errors."""
import os
import sys
import time
import uuid
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from platform_app.database import (
    get_db_connection, set_user_cookie, mark_cookie_verified, get_cookie_verified_at,
)
from platform_app.gemini_connection import connection_state, invalidate


COOKIE = "SAPISID=abc; __Secure-1PSID=def; SID=ghi"


def _user():
    conn = get_db_connection()
    email = f"cache_{uuid.uuid4().hex[:10]}@example.com"
    conn.execute(
        "INSERT INTO users (email, password_hash, role, is_active, is_vip, token_balance, created_at) "
        "VALUES (?, ?, 'user', 1, 0, 0, ?)",
        (email, "x", time.time()),
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    conn.close()
    return uid


def test_saved_cookie_is_trusted_without_live_call():
    uid = _user()
    set_user_cookie(uid, COOKIE, verified=True)
    invalidate(uid)
    with mock.patch(
        "gemini_web2api.gemini.validate_cookie", side_effect=AssertionError("must not call")
    ):
        state = connection_state(uid)
    assert state == {"connected": True, "valid": True, "reason": "ok"}


def test_ttl_expiry_with_network_error_keeps_access():
    uid = _user()
    set_user_cookie(uid, COOKIE, verified=True)
    conn = get_db_connection()
    conn.execute("UPDATE users SET gemini_cookie_verified_at = ? WHERE id = ?",
                 (time.time() - 13 * 3600, uid))  # older than 12h trust, within 24h grace
    conn.commit()
    conn.close()
    invalidate(uid)
    with mock.patch("gemini_web2api.gemini.validate_cookie",
                    return_value={"valid": False, "state": "error", "detail": "network down"}):
        state = connection_state(uid)
    assert state["valid"] is True


def test_expired_session_blocks_and_clears_verification():
    uid = _user()
    set_user_cookie(uid, COOKIE, verified=True)
    invalidate(uid)
    with mock.patch("gemini_web2api.gemini.validate_cookie",
                    return_value={"valid": False, "state": "expired", "detail": "signed out"}):
        state = connection_state(uid, force=True)
    assert state == {"connected": True, "valid": False, "reason": "invalid"}
    assert get_cookie_verified_at(uid) == 0.0


def test_forced_check_updates_verification():
    uid = _user()
    set_user_cookie(uid, COOKIE, verified=True)
    invalidate(uid)
    with mock.patch("gemini_web2api.gemini.validate_cookie",
                    return_value={"valid": True, "state": "ok", "detail": "connected"}):
        state = connection_state(uid, force=True)
    assert state["valid"] is True
    assert get_cookie_verified_at(uid) > 0


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL CONNECTION CACHE TESTS PASSED")
