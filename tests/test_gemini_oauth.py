"""Google OAuth connect-flow tests."""
import os
import sys
import time
import uuid
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from platform_app.main import app
from platform_app.database import get_db_connection
from platform_app import gemini_oauth


def _register():
    client = TestClient(app)
    email = f"oauth_{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert res.status_code == 200, res.text
    data = res.json()
    return client, {"Authorization": f"Bearer {data['token']}"}, data["user"]["id"]


def test_status_when_not_connected_and_not_configured():
    client, headers, uid = _register()
    res = client.get("/api/user/gemini-connection", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is False
    assert data["configured"] is False
    assert data["state"] == "not_connected"


def test_connect_requires_login_and_configured_redirect():
    client = TestClient(app)
    res = client.get("/api/auth/gemini/connect", follow_redirects=False)
    assert res.status_code in (401, 303)

    client, headers, uid = _register()
    with mock.patch("platform_app.gemini_oauth.begin_auth", return_value="https://accounts.google.com/o/oauth2/v2/auth?x=1"):
        res = client.get("/api/auth/gemini/connect", headers=headers, follow_redirects=False)
    assert res.status_code == 303
    assert "accounts.google.com" in res.headers["location"]


def test_callback_stores_tokens_and_status_refreshes():
    client, headers, uid = _register()

    with mock.patch("platform_app.routes.gemini_oauth_routes.complete_auth", return_value={"ok": True, "email": "user@gmail.com"}):
        with mock.patch("platform_app.auth.get_current_user_optional", new=lambda request: {"id": uid}):
            res = client.get("/api/auth/gemini/callback?code=abc&state=xyz", follow_redirects=False)
    assert res.status_code == 303
    assert "gemini=connected" in res.headers["location"]

    with mock.patch("platform_app.routes.gemini_oauth_routes.connection_status", return_value={
        "connected": True, "email": "user@gmail.com", "state": "ok",
        "detail": "Connected as user@gmail.com", "configured": True,
    }):
        status = client.get("/api/user/gemini-connection", headers=headers)
    assert status.json()["connected"] is True
    assert status.json()["email"] == "user@gmail.com"


def test_disconnect_clears_tokens():
    client, headers, uid = _register()
    conn = get_db_connection()
    conn.execute("UPDATE users SET gemini_oauth_email = ? WHERE id = ?", ("user@gmail.com", uid))
    conn.commit()
    conn.close()

    res = client.delete("/api/user/gemini-connection", headers=headers)
    assert res.status_code == 200

    conn = get_db_connection()
    row = conn.execute("SELECT gemini_oauth_email FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    assert row["gemini_oauth_email"] == ""


def test_state_validation_rejects_mismatch():
    client, headers, uid = _register()
    with mock.patch("platform_app.gemini_oauth.client_config", return_value=("id", "secret")):
        with mock.patch("platform_app.gemini_oauth._post_token", return_value={"access_token": "x"}):
            result = gemini_oauth.complete_auth(uid, "code", "wrong-state", "http://localhost:8081")
    assert result["ok"] is False


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL GEMINI OAUTH TESTS PASSED")
