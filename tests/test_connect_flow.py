"""Connect flow tests: ZIP download, verification endpoint, live check helper."""
import os
import sys
import io
import zipfile
import uuid
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from platform_app.main import app
from gemini_web2api.gemini import validate_cookie


def test_connector_zip_download():
    client = TestClient(app)
    res = client.get("/static/gemini-connector.zip")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert "background.js" in names
    assert "content.js" in names
    assert "popup.html" in names


def test_verify_endpoint_and_status():
    client = TestClient(app)
    email = f"connect_{uuid.uuid4().hex[:10]}@example.com"
    reg = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert reg.status_code == 200, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}

    with mock.patch(
        "platform_app.gemini_connection.connection_state",
        return_value={"connected": True, "valid": True, "reason": "ok"},
    ):
        status = client.get("/api/user/cookie", headers=headers)
        assert status.status_code == 200
        assert status.json()["connection"]["valid"] is True

        verify = client.post("/api/user/cookie/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["connection"]["state"] == "ok"


def test_live_cookie_check_states():
    with mock.patch("gemini_web2api.gemini.urllib.request.urlopen") as mocked:
        class Resp:
            def geturl(self):
                return "https://gemini.google.com/u/0/app"

            def read(self, n):
                return b'<html>WIZ_global_data = {"SNlM0e":"abc"}</html>'

        mocked.return_value = Resp()
        result = validate_cookie("SAPISID=abc; __Secure-1PSID=def")
        assert result["valid"] is True

        class LoginResp(Resp):
            def geturl(self):
                return "https://accounts.google.com/ServiceLogin?continue=x"

        mocked.return_value = LoginResp()
        result = validate_cookie("SAPISID=abc; __Secure-1PSID=def")
        assert result["valid"] is False
        assert result["state"] == "expired"

    assert validate_cookie("")["state"] == "expired"


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL CONNECT FLOW TESTS PASSED")
