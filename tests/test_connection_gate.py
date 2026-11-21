"""Security gate: no verified Gemini connection -> no keys, no API."""
import os
import sys
import time
import uuid
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from platform_app.main import app
from platform_app.database import get_db_connection


def _user():
    client = TestClient(app)
    email = f"gate_{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert res.status_code == 200, res.text
    data = res.json()
    return client, {"Authorization": f"Bearer {data['token']}"}, data["user"]["id"]


def test_cannot_create_key_without_connection():
    client, headers, uid = _user()
    res = client.post("/api/user/keys", headers=headers, json={"name": "x"})
    assert res.status_code == 403, res.text
    assert "verify" in res.json()["detail"].lower()


def test_cannot_use_api_without_connection():
    client, headers, uid = _user()
    conn = get_db_connection()
    conn.execute("UPDATE users SET token_balance = 100000 WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    key = None
    # Create key while connection mocked valid, then test API with connection off.
    with mock.patch("platform_app.gemini_connection.connection_state",
                    return_value={"connected": True, "valid": True, "reason": "ok"}):
        key = client.post("/api/user/keys", headers=headers, json={"name": "x"}).json()["key"]["key_secret"]

    res = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["error"]["type"] == "not_connected"


def test_api_blocked_when_connection_invalid():
    client, headers, uid = _user()
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET token_balance = 100000, gemini_cookie = ?, gemini_cookie_verified_at = ? WHERE id = ?",
        ("fake; SAPISID=fake", time.time(), uid),
    )
    conn.commit()
    conn.close()

    with mock.patch("platform_app.gemini_connection.connection_state",
                    return_value={"connected": True, "valid": True, "reason": "ok"}):
        key = client.post("/api/user/keys", headers=headers, json={"name": "x"}).json()["key"]["key_secret"]

    with mock.patch("platform_app.gemini_connection.connection_state",
                    return_value={"connected": True, "valid": False, "reason": "invalid"}):
        res = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["error"]["type"] == "invalid"


def test_token_check_precedes_connection_gate():
    client, headers, uid = _user()
    with mock.patch("platform_app.gemini_connection.connection_state",
                    return_value={"connected": True, "valid": True, "reason": "ok"}):
        key = client.post("/api/user/keys", headers=headers, json={"name": "x"}).json()["key"]["key_secret"]
    res = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 402, res.text


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL CONNECTION GATE TESTS PASSED")
