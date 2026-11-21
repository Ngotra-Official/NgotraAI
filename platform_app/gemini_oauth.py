"""Google OAuth 2.0 for the 'Connect Gemini Account' flow.

Uses the official Generative Language API (Gemini API), not web scraping.
Requires a Google Cloud OAuth Web client configured in admin settings.
"""
import json
import time
import secrets
import urllib.parse
import urllib.request
import urllib.error

from .database import (
    get_setting, get_db_connection, decrypt_cookie,
    set_user_oauth, set_oauth_state,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPES = [
    "https://www.googleapis.com/auth/generative-language",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def redirect_uri(base_url: str) -> str:
    return str(base_url).rstrip("/") + "/api/auth/gemini/callback"


def client_config() -> tuple:
    return (
        get_setting("google_client_id", "").strip(),
        get_setting("google_client_secret", "").strip(),
    )


def is_configured() -> bool:
    cid, secret = client_config()
    return bool(cid and secret)


def begin_auth(user_id: int, base_url: str) -> str:
    cid, _ = client_config()
    state = secrets.token_urlsafe(24)
    set_oauth_state(user_id, state)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(base_url),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"
    }, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def complete_auth(user_id: int, code: str, state: str, base_url: str) -> dict:
    cid, secret = client_config()
    conn = get_db_connection()
    row = conn.execute("SELECT gemini_oauth_state FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    expected = row["gemini_oauth_state"] if row else ""
    if not expected or not secrets.compare_digest(expected, state):
        return {"ok": False, "detail": "Invalid connection state. Please try again."}

    try:
        tokens = _post_token({
            "code": code,
            "client_id": cid,
            "client_secret": secret,
            "redirect_uri": redirect_uri(base_url),
            "grant_type": "authorization_code",
        })
    except Exception as e:
        return {"ok": False, "detail": f"Token exchange failed: {e}"}

    access = tokens.get("access_token", "")
    if not access:
        return {"ok": False, "detail": (tokens.get("error_description") or "No access token returned.")}

    refresh = tokens.get("refresh_token", "")
    email = ""
    try:
        req = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            email = json.loads(resp.read().decode()).get("email", "")
    except Exception:
        pass

    set_user_oauth(user_id, access, refresh, tokens.get("expires_in", 3600), email)
    return {"ok": True, "email": email}


def _user_row(user_id: int):
    conn = get_db_connection()
    row = conn.execute("""
        SELECT gemini_oauth_access_token, gemini_oauth_refresh_token,
               gemini_oauth_expiry, gemini_oauth_email
        FROM users WHERE id = ?
    """, (user_id,)).fetchone()
    conn.close()
    return row


def get_access_token(user_id: int) -> str:
    """Return a valid access token, refreshing it when needed."""
    row = _user_row(user_id)
    if not row:
        return ""
    access = decrypt_cookie(row["gemini_oauth_access_token"])
    expiry = float(row["gemini_oauth_expiry"] or 0)
    refresh = decrypt_cookie(row["gemini_oauth_refresh_token"])
    if access and expiry > time.time() + 30:
        return access
    if not refresh:
        return "" if expiry <= time.time() else access

    cid, secret = client_config()
    try:
        tokens = _post_token({
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        })
    except Exception:
        return ""
    new_access = tokens.get("access_token", "")
    if new_access:
        set_user_oauth(user_id, new_access, refresh, tokens.get("expires_in", 3600),
                       row["gemini_oauth_email"] or "")
    return new_access


def connection_status(user_id: int) -> dict:
    row = _user_row(user_id)
    if not row or not row["gemini_oauth_access_token"]:
        return {"connected": False, "email": "", "state": "not_connected",
                "detail": "Not connected.", "configured": is_configured()}
    token = get_access_token(user_id)
    if not token:
        return {"connected": False, "email": row["gemini_oauth_email"] or "",
                "state": "expired", "detail": "Session expired. Connect again.",
                "configured": is_configured()}

    # Live verify with Google's userinfo endpoint.
    try:
        req = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode())
        email = info.get("email", "") or row["gemini_oauth_email"] or ""
        return {"connected": True, "email": email, "state": "ok",
                "detail": f"Connected as {email}" if email else "Connected.",
                "configured": is_configured()}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"connected": False, "email": row["gemini_oauth_email"] or "",
                    "state": "expired", "detail": "Connection expired. Connect again.",
                    "configured": is_configured()}
        return {"connected": True, "email": row["gemini_oauth_email"] or "",
                "state": "ok", "detail": "Connected.", "configured": is_configured()}
    except Exception:
        return {"connected": True, "email": row["gemini_oauth_email"] or "",
                "state": "ok", "detail": "Connected.", "configured": is_configured()}
