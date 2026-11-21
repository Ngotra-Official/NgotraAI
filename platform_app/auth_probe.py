"""Detect which Gemini account index (/u/N/) a stored cookie belongs to.

Gemini Web routes secondary accounts through /u/N/. If we use the wrong prefix
the upstream returns an empty response with no error. We probe a few indexes
once with a tiny prompt, cache the working one in the database, and reuse it.
"""
import time

from .database import get_db_connection, get_user_cookie


def _stored(user_id: int) -> str:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_auth_user FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return (row["gemini_auth_user"] if row else "") or ""


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
        "UPDATE users SET gemini_auth_user = ?, gemini_xsrf_token = ? WHERE id = ?",
        (auth_user, xsrf_token, user_id),
    )
    conn.commit()
    conn.close()


def ensure_auth_user(user_id: int, cookie: str = "") -> str:
    """Return a working auth_user, probing and caching it when unknown."""
    stored = _stored(user_id)
    if stored:
        return stored
    if not cookie:
        cookie = get_user_cookie(user_id)
    if not cookie:
        return ""

    xsrf = _stored_xsrf(user_id)
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
                if xsrf:
                    _store(user_id, stored, xsrf)
            except Exception:
                pass
        for candidate in ["", "0", "1", "2", "3", "4", "5"]:
            try:
                text = generate("hi", mode, think, None, extra, cookie, candidate, xsrf)
            except Exception:
                continue
            if text and text.strip():
                if candidate != stored:
                    _store(user_id, candidate, xsrf)
                return candidate
    except Exception:
        pass
    return ""
