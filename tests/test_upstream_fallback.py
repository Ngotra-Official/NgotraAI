"""Upstream error surfacing and account-fallback tests."""
import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gemini_web2api import astream as astream_mod
from gemini_web2api.astream import _error_detail, astream
from gemini_web2api.gemini import classify_error


def test_error_detail_extracts_bard_code():
    assert _error_detail('[["BardErrorInfo",123]]') == "code 123"
    assert _error_detail('{"x":"BardErrorInfo [456]"}') == "code 456"
    assert _error_detail("no error here") == ""


def test_classify_error_expanded_patterns():
    assert classify_error("RATE_LIMIT_EXCEEDED") == "rate_limit"
    assert classify_error("PERMISSION_DENIED") == "auth"
    assert classify_error("BardErrorInfo [123]") == "upstream"
    assert classify_error("something random") == ""


class _Resp:
    def __init__(self, text):
        self.status_code = 200
        self._text = text

    async def aread(self):
        return self._text.encode()

    def aiter_text(self):
        async def _gen():
            yield self._text
        return _gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Client:
    attempts = []

    def __init__(self, **kwargs):
        pass

    def stream(self, method, url, content=None, headers=None):
        cookie = (headers or {}).get("Cookie", "")
        if "primary" in cookie:
            self.attempts.append("primary")
            return _Resp('[["BardErrorInfo",123]]\n')
        self.attempts.append("pool")
        return _Resp('["wrb.fr",null,"...some text..."]\n')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_fallback_on_bard_error_without_surfacing_upstream_error():
    original_pool = astream_mod.CONFIG.get("cookie_pool")
    original_retries = astream_mod.CONFIG.get("retry_attempts")
    astream_mod.CONFIG["cookie_pool"] = ["SAPISID=pool; x=1"]
    astream_mod.CONFIG["retry_attempts"] = 2

    calls = []

    def fake_extract(line):
        calls.append(line)
        if "BardErrorInfo" in line:
            return []
        return ["hello from pool"]

    async def _run():
        with mock.patch.object(astream_mod.httpx, "AsyncClient", _Client), \
             mock.patch.object(astream_mod, "_extract_texts_from_line", fake_extract), \
             mock.patch.object(astream_mod, "cool"), \
             mock.patch.object(astream_mod, "mark_success"):
            chunks = []
            async for c in astream("hi", "gemini-2.5-flash", 4,
                                   cookie="SAPISID=primary; x=1", allow_fallback=True):
                chunks.append(c)
            return chunks

    _Client.attempts = []
    try:
        result = asyncio.run(_run())
    finally:
        if original_pool is None:
            astream_mod.CONFIG.pop("cookie_pool", None)
        else:
            astream_mod.CONFIG["cookie_pool"] = original_pool
        if original_retries is None:
            astream_mod.CONFIG.pop("retry_attempts", None)
        else:
            astream_mod.CONFIG["retry_attempts"] = original_retries

    assert result == ["hello from pool"], result
    # The primary cookie must be attempted only once — no pointless retry of an
    # account that just returned a BardErrorInfo — and the pool account is used.
    assert _Client.attempts == ["primary", "pool"], _Client.attempts


if __name__ == "__main__":
    test_error_detail_extracts_bard_code()
    test_classify_error_expanded_patterns()
    test_fallback_on_bard_error_without_surfacing_upstream_error()
    print("ALL UPSTREAM FALLBACK TESTS PASSED")
