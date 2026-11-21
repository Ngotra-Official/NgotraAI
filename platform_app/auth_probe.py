"""Detect which Gemini account index (/u/N/) a stored cookie belongs to.

Gemini Web routes secondary accounts through /u/N/. If we use the wrong prefix
the upstream returns an empty response with no error. We probe a few indexes
once with a tiny prompt, cache the working one in the database, and reuse it.
"""
import time

from .database import get_db_connection, get_user_cookie


def _state(user_id: int) -> tuple:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_auth_user, gemini_auth_checked FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not row:
        return "", False
    return (row["gemini_auth_user"] or ""), bool(row["gemini_auth_checked"])


def _mark_checked(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute("UPDATE users SET gemini_auth_checked = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def _stored(user_id: int) -> str:
    return _state(user_id)[0]


def _stored_xsrf(user_id: int) -> str:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_xsrf_token FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return (row["gemini_xsrf_token"] if row else "") or ""


def _store(user_id: int, auth_user: str, xsrf_token: str = "") -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_auth_user = ?, gemini_xsrf_token = ?, gemini_auth_checked = 1 WHERE id = ?",
        (auth_user, xsrf_token, user_id),
    )
    conn.commit()
    conn.close()


def ensure_auth_user(user_id: int, cookie: str = "") -> str:
    """Return a working auth_user, probing and caching it when unknown."""
    stored, checked = _state(user_id)
    if checked:
        return stored
    if not cookie:
        cookie = get_user_cookie(user_id)
    if not cookie:
        return ""

    xsrf = _stored_xsrf(user_id)
    # Mark as checked immediately so concurrent requests never run this probe
    # again — a full probe can take many seconds.
    _mark_checked(user_id)
    try:
        from gemini_web2api.gemini import generate, validate_cookie
        from gemini_web2api.models import resolve_model
        _, mode, think, err, extra = resolve_model("gemini-3.7-flash")
        if err:
            return ""
        if not xsrf:
            try:
                result = validate_cookie(cookie, stored)
                xsrf = result.get("xsrf_token", "")
            except Exception:
                pass
        # Use a prompt that reliably produces text (a bare "hi" can yield an
        # empty upstream response and cause pointless retries).
        for candidate in ["", "1", "0", "2", "3"]:
            try:
                text = generate("Say hello in 5 words", mode, think, None, extra,
                                cookie, candidate, xsrf)
            except Exception:
                continue
            if text and text.strip():
                _store(user_id, candidate, xsrf)
                return candidate
    except Exception:
        pass
    return ""
