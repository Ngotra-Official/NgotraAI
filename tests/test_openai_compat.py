"""Tests for OpenAI-compatible options, streaming and Google-native routes."""
import json
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from platform_app.main import app
from gemini_web2api.models import resolve_model


_client_cache = {}


def _client_with_tokens():
    if _client_cache:
        return _client_cache["client"], _client_cache["headers"]

    client = TestClient(app)
    email = f"compat_{int(time.time() * 1000)}@example.com"
    reg = client.post("/api/auth/register", json={"email": email, "password": "Test12345!"})
    assert reg.status_code == 200, reg.text
    data = reg.json()
    api_key = data["initial_key"]

    admin = client.post("/api/auth/login", json={
        "email": "admin@ngotra.site", "password": "Admin@Ngotra2026!",
    })
    admin_headers = {"Authorization": f"Bearer {admin.json()['token']}"}
    grant = client.post(
        f"/api/admin/users/{data['user']['id']}/extend",
        headers=admin_headers,
        json={"tokens": 1_000_000},
    )
    assert grant.status_code == 200, grant.text
    from unittest import mock
    mock.patch("platform_app.gemini_connection.connection_state",
               return_value={"connected": True, "valid": True, "reason": "ok"}).start()
    headers = {"Authorization": f"Bearer {api_key}"}
    _client_cache["client"] = client
    _client_cache["headers"] = headers
    return client, headers


def _sse_chunks(text):
    chunks = []
    for line in text.splitlines():
        if line.startswith("data: ") and "[DONE]" not in line:
            chunks.append(json.loads(line[6:]))
    return chunks



def _async_chunks(chunks):
    async def gen():
        for c in chunks:
            yield c
    return gen()

def test_model_aliases_are_echoed_and_routed():
    name, mode, think, err, _ = resolve_model("gemini-2.5-flash")
    assert (name, err) == ("gemini-2.5-flash", None)
    assert mode in (1, 2, 3, 4, 5, 6)

    name, _, _, err, _ = resolve_model("gpt-4o-mini")
    assert name == "gpt-4o-mini"  # client sees what it asked for
    assert err is None


def test_models_retrieve_and_real_ids():
    client, headers = _client_with_tokens()
    listing = client.get("/v1/models", headers=headers)
    assert listing.status_code == 200
    ids = {m["id"] for m in listing.json()["data"]}
    assert "gemini-2.5-flash" in ids
    assert "gemini-3.7-flash" in ids

    one = client.get("/v1/models/gemini-2.5-flash", headers=headers)
    assert one.status_code == 200
    assert one.json()["id"] == "gemini-2.5-flash"
    assert client.get("/v1/models/not-a-model", headers=headers).status_code == 404


def test_chat_stream_forwards_realtime_deltas_and_usage():
    client, headers = _client_with_tokens()
    with mock.patch(
        "platform_app.routes.openai_routes.astream",
        return_value=_async_chunks(["Hel", "lo", " world"]),
    ):
        res = client.post("/v1/chat/completions", headers=headers, json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        })
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    chunks = _sse_chunks(res.text)
    deltas = [
        c["choices"][0]["delta"].get("content")
        for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("content")
    ]
    assert deltas == ["Hel", "lo", " world"]
    assert any(c.get("usage") for c in chunks)
    assert any(c["choices"][0].get("finish_reason") == "stop" for c in chunks if c.get("choices"))
    assert res.text.rstrip().endswith("data: [DONE]")


def test_chat_honours_stop_and_max_tokens_and_echoes_model():
    client, headers = _client_with_tokens()
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "stop": "STOPHERE",
    }
    with mock.patch(
        "platform_app.routes.openai_routes.generate",
        return_value="answer text STOPHERE ignored",
    ):
        res = client.post("/v1/chat/completions", headers=headers, json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["model"] == "gpt-4o-mini"
    assert data["choices"][0]["message"]["content"] == "answer text "

    with mock.patch(
        "platform_app.routes.openai_routes.generate",
        return_value="abcdefghijklmnop",
    ):
        res = client.post("/v1/chat/completions", headers=headers, json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 2,
        })
    assert res.json()["choices"][0]["message"]["content"] == "abcdefgh"


def test_responses_stream_emits_true_text_deltas():
    client, headers = _client_with_tokens()
    with mock.patch(
        "platform_app.routes.openai_routes.astream",
        return_value=_async_chunks(["He", "llo"]),
    ):
        res = client.post("/v1/responses", headers=headers, json={
            "model": "gemini-2.5-flash",
            "input": "hi",
            "stream": True,
        })
    assert res.status_code == 200, res.text
    assert "event: response.created" in res.text
    assert '"type": "response.output_text.delta"' in res.text
    assert '"delta": "He"' in res.text
    assert '"delta": "llo"' in res.text
    assert "event: response.completed" in res.text


def test_google_native_endpoints():
    client, headers = _client_with_tokens()
    listing = client.get("/v1beta/models", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["models"][0]["name"].startswith("models/")

    url = "/v1beta/models/gemini-2.5-flash:generateContent"
    with mock.patch("platform_app.routes.openai_routes.generate", return_value="hello"):
        res = client.post(url, headers=headers, json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert data["modelVersion"] == "gemini-2.5-flash"
    assert "promptTokenCount" in data["usageMetadata"]

    stream_url = "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
    with mock.patch(
        "platform_app.routes.openai_routes.astream",
        return_value=_async_chunks(["stream", "ed"]),
    ):
        res = client.post(stream_url, headers=headers, json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        })
    assert res.status_code == 200
    chunks = _sse_chunks(res.text)
    texts = [
        c["candidates"][0]["content"]["parts"][0]["text"]
        for c in chunks if "content" in c["candidates"][0]
    ]
    assert texts == ["stream", "ed"]


def test_chat_stream_emits_tool_calls_without_leaking_raw_markup():
    client, headers = _client_with_tokens()
    pieces = iter([
        "Let me check that. ",
        "```tool_call\n",
        '{"name": "get_weather", "arguments": {"city": "Shanghai"}}',
        "\n```",
    ])
    with mock.patch(
        "platform_app.routes.openai_routes.astream",
        return_value=_async_chunks(list(pieces)),
    ):
        res = client.post("/v1/chat/completions", headers=headers, json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "weather?"}],
            "stream": True,
            "tools": [{
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }],
            "stream_options": {"include_usage": True},
        })
    assert res.status_code == 200, res.text
    chunks = _sse_chunks(res.text)
    contents = "".join(
        c["choices"][0]["delta"].get("content", "") or ""
        for c in chunks if c.get("choices")
    )
    assert "Let me check that." in contents
    assert "tool_call" not in contents

    tool_chunks = [c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    assert tool_chunks, res.text
    first = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert first["id"].startswith("call_")
    assert first["function"]["name"] == "get_weather"
    assert any(
        '"city"' in tc["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
        for tc in tool_chunks[1:]
    )
    assert any(
        c["choices"][0].get("finish_reason") == "tool_calls"
        for c in chunks if c.get("choices")
    )


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL OPENAI COMPAT TESTS PASSED")
