"""Official Gemini API client for user-connected Google AI keys.

This is the no-extension "connect your account" path: a user pastes an API key
from Google AI Studio, we verify it live against Google, and all of that
user's requests are routed through the official API with their own key.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _proxy_opener():
    proxy = CONFIG.get("proxy")
    if not proxy:
        return None
    import ssl
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ctx),
    )


def _url(path: str, key: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{BASE}/{path}{sep}key={urllib.parse.quote(key)}"


def _request(path: str, key: str, method: str = "GET", payload: dict = None, timeout: int = 60) -> str:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(_url(path, key), data=data, headers=headers, method=method)
    opener = _proxy_opener()
    resp = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode("utf-8", errors="replace")


def _request_token(path: str, token: str, method: str = "GET", payload: dict = None, timeout: int = 180) -> str:
    """Official API request authenticated with an OAuth access token."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    req = urllib.request.Request(f"{BASE}/{path}", data=data, headers=headers, method=method)
    opener = _proxy_opener()
    resp = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode("utf-8", errors="replace")


def generate_with_token(prompt: str, model: str, token: str, images: list = None,
                        generation_config: dict = None, tools: list = None) -> tuple:
    payload = _build_payload(prompt, images, generation_config or {}, tools or [])
    raw = _request_token(f"{_normalise_model(model)}:generateContent", token, "POST", payload)
    return _extract(json.loads(raw))


def generate_stream_with_token(prompt: str, model: str, token: str, images: list = None,
                               generation_config: dict = None, tools: list = None):
    payload = _build_payload(prompt, images, generation_config or {}, tools or [])
    url = f"{BASE}/{_normalise_model(model)}:streamGenerateContent?alt=sse"
    if not HAS_HTTPX:
        text, _, _ = generate_with_token(prompt, model, token, images, generation_config, tools)
        if text:
            yield text
        return

    with httpx.Client(timeout=180, proxy=CONFIG.get("proxy")) as client:
        with client.stream("POST", url, json=payload,
                           headers={"Authorization": f"Bearer {token}"}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text, _, _ = _extract(data)
                if text:
                    yield text


def verify_key(key: str) -> dict:
    """Live-check an AI Studio key with Google's models endpoint."""
    key = (key or "").strip()
    if not key.startswith("AIza"):
        return {"valid": False, "state": "invalid", "detail": "Key must start with AIza (Google AI Studio key)."}
    try:
        data = json.loads(_request("models", key, timeout=20))
        models = [
            m["name"].replace("models/", "")
            for m in data.get("models", [])
            if m.get("name", "").startswith("models/gemini")
        ]
        return {
            "valid": True,
            "state": "ok",
            "detail": "Connected. " + (f"{len(models)} Gemini models available." if models else "Key accepted."),
            "models": models[:30],
        }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        message = ""
        try:
            message = (json.loads(body).get("error") or {}).get("message", "")
        except Exception:
            pass
        if e.code in (400, 401, 403):
            return {"valid": False, "state": "invalid", "detail": message or "Invalid API key."}
        if e.code == 429:
            return {"valid": False, "state": "quota", "detail": "Key is valid but its quota is exhausted."}
        return {"valid": False, "state": "error", "detail": message or f"HTTP {e.code}"}
    except Exception as e:
        return {"valid": False, "state": "error", "detail": str(e)}


def build_generation_config(body: dict) -> dict:
    """Map common OpenAI options to Gemini generationConfig fields."""
    config = {}
    if body.get("temperature") is not None:
        try:
            config["temperature"] = float(body["temperature"])
        except (TypeError, ValueError):
            pass
    if body.get("top_p") is not None:
        try:
            config["topP"] = float(body["top_p"])
        except (TypeError, ValueError):
            pass
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    if max_tokens is not None:
        try:
            config["maxOutputTokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        config["stopSequences"] = [stop]
    elif isinstance(stop, list) and stop:
        config["stopSequences"] = [s for s in stop if isinstance(s, str)][:5]
    return config


def _normalise_model(model: str) -> str:
    model = (model or "").strip()
    if not model.startswith("models/"):
        model = "models/" + model
    return model


def _build_payload(prompt: str, images: list, generation_config: dict, tools: list) -> dict:
    parts = [{"text": prompt}]
    for item in images or []:
        if isinstance(item, tuple) and len(item) == 2 and item[0]:
            import base64
            data, mime = item
            if isinstance(data, bytes):
                parts.append({"inlineData": {"mimeType": mime or "image/png",
                                             "data": base64.b64encode(data).decode()}})
    payload = {"contents": [{"role": "user", "parts": parts}]}
    if generation_config:
        payload["generationConfig"] = generation_config
    if tools:
        declarations = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        payload["tools"] = [{"functionDeclarations": declarations}]
    return payload


def _extract(data: dict) -> tuple:
    """Return (text, function_calls, usage) from a generateContent response."""
    text_parts = []
    function_calls = []
    try:
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("text"):
                    text_parts.append(part["text"])
                if part.get("functionCall"):
                    fc = part["functionCall"]
                    function_calls.append({
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                    })
    except (TypeError, KeyError):
        pass
    usage = data.get("usageMetadata", {})
    return "".join(text_parts), function_calls, {
        "prompt_tokens": usage.get("promptTokenCount", 0),
        "completion_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0),
    }


def generate(prompt: str, model: str, key: str, images: list = None,
             generation_config: dict = None, tools: list = None) -> tuple:
    payload = _build_payload(prompt, images, generation_config or {}, tools or [])
    raw = _request(f"{_normalise_model(model)}:generateContent", key, "POST", payload, timeout=180)
    return _extract(json.loads(raw))


def generate_stream(prompt: str, model: str, key: str, images: list = None,
                    generation_config: dict = None, tools: list = None):
    """Yield incremental text deltas from the official streaming endpoint."""
    payload = _build_payload(prompt, images, generation_config or {}, tools or [])
    url = _url(f"{_normalise_model(model)}:streamGenerateContent?alt=sse", key)
    proxy = CONFIG.get("proxy")
    if not HAS_HTTPX:
        text, _, _ = generate(prompt, model, key, images, generation_config, tools)
        if text:
            yield text
        return

    with httpx.Client(timeout=180, proxy=proxy) as client:
        with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text, _, _ = _extract(data)
                if text:
                    yield text
