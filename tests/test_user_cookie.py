"""Per-user Gemini cookie storage and routing tests."""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from unittest import mock

from platform_app.main import app
from platform_app.database import (
    get_db_connection, set_user_cookie, get_user_cookie, clear_user_cookie,
)
from gemini_web2api.config import CONFIG
from gemini_web2api.gemini import load_cookie, request_cookie_override


COOKIE = "SAPISID=abc123; __Secure-1PSID=def456; SID=xyz"


def _register():
    client = TestClient(app)
    email = f"cookie_{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert res.status_code == 200, res.text
    return client, {"Authorization": f"Bearer {res.json()['token']}"}, res.json()["user"]["id"]


def test_encrypted_storage_roundtrip_and_helpers():
    uid_real = _register()[2]
    set_user_cookie(uid_real, COOKIE)
    conn = get_db_connection()
    raw = conn.execute("SELECT gemini_cookie FROM users WHERE id = ?", (uid_real,)).fetchone()["gemini_cookie"]
    conn.close()
    assert raw and "SAPISID=abc123" not in raw  # encrypted at rest
    assert get_user_cookie(uid_real) == COOKIE
    clear_user_cookie(uid_real)
    assert get_user_cookie(uid_real) == ""


def test_cookie_api_and_gemini_override():
    client, headers, uid = _register()

    status = client.get("/api/user/cookie", headers=headers)
    assert status.status_code == 200
    assert status.json()["connected"] is False

    bad = client.post("/api/user/cookie", headers=headers, json={"cookie": "garbage"})
    assert bad.status_code == 400

    saved = client.post("/api/user/cookie", headers=headers, json={"cookie": COOKIE})
    assert saved.status_code == 200, saved.text
    status = client.get("/api/user/cookie", headers=headers)
    assert status.json()["connected"] is True

    # The saved cookie is now used by the Gemini client for this user.
    original = dict(CONFIG)
    token = request_cookie_override.set(COOKIE)
    try:
        CONFIG["cookie_pool"] = ["SAPISID=pool; other=1"]
        cookie, sapisid = load_cookie()
        assert "SAPISID=abc123" in cookie
        assert sapisid == "abc123"
    finally:
        request_cookie_override.reset(token)
        CONFIG.clear()
        CONFIG.update(original)

    removed = client.delete("/api/user/cookie", headers=headers)
    assert removed.status_code == 200
    assert client.get("/api/user/cookie", headers=headers).json()["connected"] is False


def test_api_key_uses_user_cookie_override():
    client, headers, uid = _register()
    mock.patch("platform_app.gemini_connection.connection_state",
               return_value={"connected": True, "valid": True, "reason": "ok"}).start()
    key = client.post("/api/user/keys", headers=headers, json={"name": "test"}).json()["key"]["key_secret"]
    set_user_cookie(uid, COOKIE)
    conn = get_db_connection()
    conn.execute("UPDATE users SET token_balance = 100000 WHERE id = ?", (uid,))
    conn.commit()
    conn.close()

    original = dict(CONFIG)
    token = request_cookie_override.set(None)
    captured = {}

    def capture_generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None, cookie="", **kwargs):
        captured["cookie"] = cookie
        return "ok"

    try:
        CONFIG["cookie_pool"] = ["SAPISID=pool; other=1"]
        with mock.patch("platform_app.routes.openai_routes.generate", side_effect=capture_generate):
            res = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
        assert res.status_code == 200, res.text
        assert "SAPISID=abc123" in captured.get("cookie", ""), captured
    finally:
        request_cookie_override.reset(token)
        CONFIG.clear()
        CONFIG.update(original)


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL USER COOKIE TESTS PASSED")
