"""End-to-end proof that API requests reduce the token balance."""
import os
import sys
import time
import uuid
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from platform_app.main import app
from platform_app.database import get_db_connection


def _user_with_key_and_cookie(tokens=1000):
    client = TestClient(app)
    email = f"spend_{uuid.uuid4().hex[:10]}@example.com"
    reg = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert reg.status_code == 200, reg.text
    data = reg.json()
    uid = data["user"]["id"]
    key = data["initial_key"]

    mock.patch("platform_app.gemini_connection.connection_state",
               return_value={"connected": True, "valid": True, "reason": "ok"}).start()
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET token_balance = ?, gemini_cookie = ?, gemini_cookie_verified_at = ? WHERE id = ?",
        (tokens, "SAPISID=test; __Secure-1PSID=test", time.time(), uid),
    )
    conn.commit()
    conn.close()
    return client, key, uid


def balance_of(uid):
    conn = get_db_connection()
    row = conn.execute("SELECT token_balance FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return row["token_balance"]


def test_balance_decreases_after_chat_completion():
    client, key, uid = _user_with_key_and_cookie(1000)
    before = balance_of(uid)

    with mock.patch("platform_app.routes.openai_routes.generate", return_value="hello world"):
        res = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi there"}],
        })
    assert res.status_code == 200, res.text
    after = balance_of(uid)
    assert after < before, f"balance did not drop: {before} -> {after}"
    print(f"  balance {before} -> {after}, spent {before - after}")


def test_balance_decreases_after_streaming_chat():
    client, key, uid = _user_with_key_and_cookie(1000)
    before = balance_of(uid)

    with mock.patch("platform_app.routes.openai_routes.generate_stream",
                    return_value=iter(["hello", " world", "!"])):
        res = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    assert res.status_code == 200, res.text
    after = balance_of(uid)
    assert after < before, f"streaming balance did not drop: {before} -> {after}"
    print(f"  streaming balance {before} -> {after}, spent {before - after}")


def test_short_request_still_spends_at_least_one_token():
    from gemini_web2api.options import build_usage
    usage = build_usage("hi", "ok")
    assert usage["total_tokens"] >= 1


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL TOKEN SPEND TESTS PASSED")
