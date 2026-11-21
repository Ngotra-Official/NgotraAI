"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import random
import hashlib
from contextvars import ContextVar

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_httpx_client = None

# Per-request account selection for multi-cookie deployments. The platform
# sets this from the user id, so each user consistently uses one account and
# load is spread across the configured cookie pool.
request_cookie_index: ContextVar = ContextVar("request_cookie_index", default=None)
# A user's own Gemini cookie, set per request by the platform gateway.
request_cookie_override: ContextVar = ContextVar("request_cookie_override", default=None)


def _parse_cookie_string(content: str) -> tuple:
    content = (content or "").strip()
    if not content:
        return "", None
    if content.startswith("{"):
        data = json.loads(content)
        return data.get("cookie", ""), data.get("sapisid", "") or None
    pairs = dict(p.split("=", 1) for p in content.split("; ") if "=" in p)
    return content, pairs.get("SAPISID") or None


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def load_cookie() -> tuple:
    """Load the cookie for this request (user override > pool > file)."""
    override = request_cookie_override.get()
    if override:
        try:
            return _parse_cookie_string(override)
        except Exception as e:
            log(f"User cookie parse error: {e}")
            return "", None

    pool = CONFIG.get("cookie_pool") or []
    if pool:
        index = request_cookie_index.get()
        if index is None or not (0 <= index < len(pool)):
            index = random.randrange(len(pool))
        try:
            return _parse_cookie_string(pool[index])
        except Exception as e:
            log(f"Cookie pool entry {index} error: {e}")
            return "", None

    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        cookie_str, sapisid = _parse_cookie_string(content)
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def validate_cookie(cookie_str: str, auth_user: str = "") -> dict:
    """Check a Gemini web cookie by loading the app page with it.

    Returns {"valid": bool, "state": "ok"|"expired"|"error", "detail": str}.
    A logged-in page exposes WIZ_global_data / SNlM0e; a signed-out page
    redirects to accounts.google.com or renders a sign-in form.
    """
    if not cookie_str or not cookie_str.strip():
        return {"valid": False, "state": "expired", "detail": "No cookie stored."}
    try:
        cookie_str, sapisid = _parse_cookie_string(cookie_str.strip())
    except Exception as e:
        return {"valid": False, "state": "error", "detail": str(e)}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_str,
    }
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    url = f"https://gemini.google.com{_account_prefix(auth_user)}/app"
    if auth_user:
        headers["X-Goog-AuthUser"] = str(auth_user)
    proxy = CONFIG.get("proxy")
    try:
        req = urllib.request.Request(url, headers=headers)
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            resp = opener.open(req, timeout=20)
        else:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=20)
        final_url = resp.geturl()
        body = resp.read(400000).decode("utf-8", errors="replace")
    except Exception as e:
        return {"valid": False, "state": "error", "detail": f"Could not reach Gemini: {e}"}

    if "accounts.google.com" in final_url or "ServiceLogin" in final_url:
        return {"valid": False, "state": "expired", "detail": "Session expired. Connect again."}
    # Google redirects a secondary-account session to /u/<n>/app. Capture that
    # number so requests are routed to the correct Gemini account.
    detected_auth_user = ""
    m = re.search(r"/u/(\d+)(?:/|$)", final_url)
    if m:
        detected_auth_user = m.group(1)

    if "WIZ_global_data" in body or "SNlM0e" in body or 'data-app="gemini"' in body:
        return {"valid": True, "state": "ok", "detail": "Connected to Gemini.",
                "auth_user": detected_auth_user}
    return {"valid": False, "state": "expired", "detail": "Not signed in to Gemini.",
            "auth_user": detected_auth_user}


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix(auth_user: str = "") -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    au = auth_user or CONFIG.get("auth_user")
    if au is None or au == "":
        return ""
    return f"/u/{au}"


def _build_headers(cookie: str = "", auth_user: str = "") -> dict:
    account_prefix = _account_prefix(auth_user)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(auth_user or CONFIG["auth_user"])
    # An explicit cookie is passed straight through from the platform gateway.
    # This avoids ContextVar token resets across async/thread contexts during
    # streaming, which aborted responses after the first chunk.
    if cookie:
        try:
            cookie_str, sapisid = _parse_cookie_string(cookie)
        except Exception:
            cookie_str, sapisid = cookie, None
    else:
        cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url(auth_user: str = "") -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix(auth_user)
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
             extra_fields: dict = None, cookie: str = "", auth_user: str = "") -> str:
    """Non-streaming generation with retry."""
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url(auth_user)
    headers = _build_headers(cookie, auth_user)
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            raw = resp.read().decode("utf-8", errors="replace")
            return extract_response_text(raw)
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                    extra_fields: dict = None, cookie: str = "", auth_user: str = ""):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, cookie, auth_user)
        if text:
            yield text
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url(auth_user)
    headers = _build_headers(cookie, auth_user)
    client = _get_httpx_client()

    last_err = None
    emitted_raw_text = ""
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf:
                        bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                        if bard_err:
                            raise RuntimeError(
                                f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                            )
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        texts = _extract_texts_from_line(line)
                        if not texts:
                            continue
                        # Upstream sends cumulative snapshots. Thinking responses
                        # can contain several text parts; the longest one is the
                        # assembled answer, so emit only its new suffix.
                        snapshot = max(texts, key=len)
                        if snapshot == emitted_raw_text:
                            continue
                        if emitted_raw_text and snapshot.startswith(emitted_raw_text):
                            delta = clean_text(snapshot[len(emitted_raw_text):], strip=False)
                        else:
                            # A fresh response (or a retry after a truncated
                            # stream): start over from the beginning.
                            delta = clean_text(snapshot, strip=False)
                        emitted_raw_text = snapshot
                        if delta:
                            yield delta
                # Some upstream responses end without a trailing newline.
                if buf.strip():
                    texts = _extract_texts_from_line(buf)
                    if texts:
                        snapshot = max(texts, key=len)
                        if snapshot and snapshot != emitted_raw_text:
                            if emitted_raw_text and snapshot.startswith(emitted_raw_text):
                                delta = clean_text(snapshot[len(emitted_raw_text):], strip=False)
                            else:
                                delta = clean_text(snapshot, strip=False)
                            emitted_raw_text = snapshot
                            if delta:
                                yield delta
            return
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err
