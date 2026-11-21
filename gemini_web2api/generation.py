"""Generation helpers that apply a per-request Gemini cookie override.

The platform gateway authenticates synchronously but generates inside async
route handlers, so ContextVars can be lost. We expose wrappers that accept an
explicit cookie and set the context variable immediately before generating.
"""
import contextlib

from .gemini import (
    generate as _generate,
    generate_stream as _generate_stream,
    request_cookie_override,
)


@contextlib.contextmanager
def _cookie_context(cookie: str):
    token = request_cookie_override.set(cookie or None)
    try:
        yield
    finally:
        request_cookie_override.reset(token)


def generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None, cookie: str = ""):
    with _cookie_context(cookie):
        return _generate(prompt, model_id, think_mode, file_refs, extra_fields)


def generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None, cookie: str = ""):
    with _cookie_context(cookie):
        yield from _generate_stream(prompt, model_id, think_mode, file_refs, extra_fields)
