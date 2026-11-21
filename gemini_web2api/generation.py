"""Generation helpers that pass the user's Gemini cookie explicitly.

A ContextVar token cannot be set/reset across the thread/context switches used
by StreamingResponse — doing so aborted streams after the first chunk. Passing
the cookie as a plain argument is safe in every context.
"""
from .gemini import generate as _generate, generate_stream as _generate_stream


def generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None, cookie: str = ""):
    return _generate(prompt, model_id, think_mode, file_refs, extra_fields, cookie)


def generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None, cookie: str = ""):
    yield from _generate_stream(prompt, model_id, think_mode, file_refs, extra_fields, cookie)
