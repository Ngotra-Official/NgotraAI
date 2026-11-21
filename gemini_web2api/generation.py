"""Generation helpers that pass the user's Gemini session explicitly.

No ContextVar token is set/reset here — StreamingResponse iterates sync
generators in thread pools, and a token reset across contexts aborts the
stream. Plain arguments are safe in every context.
"""
from .gemini import generate as _generate, generate_stream as _generate_stream


def generate(prompt, model_id, think_mode, file_refs=None, extra_fields=None,
             cookie: str = "", auth_user: str = "", xsrf_token: str = ""):
    return _generate(prompt, model_id, think_mode, file_refs, extra_fields, cookie, auth_user, xsrf_token)


def generate_stream(prompt, model_id, think_mode, file_refs=None, extra_fields=None,
                    cookie: str = "", auth_user: str = "", xsrf_token: str = ""):
    yield from _generate_stream(prompt, model_id, think_mode, file_refs, extra_fields,
                                cookie, auth_user, xsrf_token)
