"""Async streaming generation for high-concurrency gateway traffic.

Uses httpx.AsyncClient so thousands of connections don't consume thread-pool
workers, and yields deltas the moment upstream sends them.
"""
import json
import re

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG
from .gemini import (
    _build_payload, _get_url, _build_headers, _extract_texts_from_line,
    clean_text, classify_error, cool, mark_success,
)
from .account_health import health


async def astream(prompt, model_id, think_mode, file_refs=None, extra_fields=None,
                  cookie="", auth_user="", xsrf_token="", allow_fallback=True):
    """Yield incremental text deltas from Gemini's streaming endpoint."""
    if not HAS_HTTPX:
        from .generation import generate_stream
        for chunk in generate_stream(prompt, model_id, think_mode, file_refs,
                                     extra_fields, cookie, auth_user, xsrf_token):
            yield chunk
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, xsrf_token)
    url = _get_url(auth_user)
    headers = _build_headers(cookie, auth_user)
    proxy = CONFIG.get("proxy")
    timeout = httpx.Timeout(CONFIG.get("request_timeout_sec", 180), connect=10.0)

    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, limits=httpx.Limits(
            max_connections=200, max_keepalive_connections=50)) as client:
        emitted = ""
        buf = ""

        async def run(ck, au):
            nonlocal emitted
            u = _get_url(au)
            h = _build_headers(ck, au)
            async with client.stream("POST", u, content=body, headers=h) as resp:
                if resp.status_code >= 400:
                    try:
                        txt = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                    except Exception:
                        txt = ""
                    reason = classify_error(txt, resp.status_code)
                    if reason:
                        cool(ck, reason)
                    raise RuntimeError(f"Gemini upstream HTTP {resp.status_code}: {reason or 'error'}")
                local_buf = ""
                async for chunk in resp.aiter_text():
                    local_buf += chunk
                    if "BardErrorInfo" in local_buf or "rate_limit" in local_buf.lower():
                        reason = classify_error(local_buf[:4000])
                        if reason:
                            cool(ck, reason)
                        raise RuntimeError(f"Gemini upstream issue: {reason or 'error'}")
                    while "\n" in local_buf:
                        line, local_buf = local_buf.split("\n", 1)
                        texts = _extract_texts_from_line(line)
                        if not texts:
                            continue
                        snapshot = max(texts, key=len)
                        if snapshot == emitted:
                            continue
                        delta = (clean_text(snapshot[len(emitted):], strip=False)
                                 if emitted and snapshot.startswith(emitted)
                                 else clean_text(snapshot, strip=False))
                        emitted = snapshot
                        if delta:
                            yield delta
                if local_buf.strip():
                    texts = _extract_texts_from_line(local_buf)
                    if texts:
                        snapshot = max(texts, key=len)
                        if snapshot and snapshot != emitted:
                            delta = (clean_text(snapshot[len(emitted):], strip=False)
                                     if emitted and snapshot.startswith(emitted)
                                     else clean_text(snapshot, strip=False))
                            emitted = snapshot
                            if delta:
                                yield delta

        last_error = None
        for attempt in range(CONFIG.get("retry_attempts", 2)):
            try:
                async for delta in run(cookie, auth_user):
                    yield delta
                if not emitted:
                    cool(cookie, "empty")
                    raise RuntimeError(
                        "Gemini returned an empty response. Please reconnect your "
                        "Gemini account in Dashboard → Settings and try again."
                    )
                mark_success(cookie)
                return
            except Exception as e:
                last_error = e
                if attempt < CONFIG.get("retry_attempts", 2) - 1:
                    continue
                # Try a healthy account from the shared pool before failing.
                if allow_fallback:
                    pool = CONFIG.get("cookie_pool") or []
                    for alt in pool:
                        if not alt or alt == cookie or not health(alt)["ok"]:
                            continue
                        try:
                            async for delta in astream(prompt, model_id, think_mode, file_refs,
                                                       extra_fields, alt, "", xsrf_token,
                                                       allow_fallback=False):
                                yield delta
                            return
                        except Exception:
                            continue
                raise last_error
