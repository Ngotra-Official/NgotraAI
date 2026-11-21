"""Model definitions and mapping from Gemini frontend JS source.

The IDs below are the public model names exposed by this gateway. Gemini Web
itself routes requests through numeric "mode" ids (see MODE_CATEGORY in the
frontend bundle); the mapping here keeps the OpenAI-facing names stable while
allowing real Gemini model names and common client aliases to be used.
"""

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.7-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-2.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 2.5 Flash (fast, high volume)",
    },
    "gemini-2.0-flash": {
        "mode": 1, "think": 4,
        "desc": "Gemini 2.0 Flash",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-2.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Gemini 2.5 Flash with extended thinking",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-2.5-pro": {
        "mode": 3, "think": 4,
        "desc": "Gemini 2.5 Pro",
    },
    "gemini-3.1-pro-enhanced": {
        "mode": 3, "think": 4, "extra": {31: 2, 80: 3},
        "desc": "Pro with enhanced output (experimental)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# Client-side aliases -> canonical gateway model. Unknown names are not errors:
# they are echoed back to the client but routed through the default model.
MODEL_ALIASES = {
    "gemini-2.5-flash-latest": "gemini-2.5-flash",
    "gemini-flash-latest": "gemini-3.7-flash",
    "gemini-pro-latest": "gemini-3.1-pro",
    "gemini-2.5-pro-latest": "gemini-2.5-pro",
    "gemini-3-flash": "gemini-3.7-flash",
    "gemini-3-pro": "gemini-3.1-pro",
    "flash": "gemini-3.7-flash",
    "pro": "gemini-3.1-pro",
    "thinking": "gemini-3.5-flash-thinking",
    "lite": "gemini-flash-lite",
    # Common third-party identifiers seen from OpenAI-compatible clients.
    "gpt-4o": "gemini-3.7-flash",
    "gpt-4o-mini": "gemini-flash-lite",
    "gpt-4.1": "gemini-3.1-pro",
    "gpt-3.5-turbo": "gemini-flash-lite",
    "claude-3-5-sonnet": "gemini-3.1-pro",
}


def resolve_model(model_name: str, default: str = "gemini-3.6-flash"):
    """Resolve a requested model to (name, mode_id, think_mode, error, extra).

    The *requested* name is always returned so responses echo what the client
    asked for. Only the routing (mode id) falls back to ``default`` for names
    we do not recognise.
    """
    name = model_name or default
    think_override = None
    if "@think=" in name:
        name, think_str = name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            return None, None, None, f"Invalid think level: {think_str}", None

    requested = name
    canonical = MODEL_ALIASES.get(name, name)
    cfg = MODELS.get(canonical)
    if cfg is None:
        from .gemini import log
        log(f"Unknown model '{requested}', routing via '{default}'")
        cfg = MODELS[default]

    mode_id = cfg["mode"]
    think_mode = think_override if think_override is not None else cfg["think"]
    return requested, mode_id, think_mode, None, cfg.get("extra")


def list_model_ids() -> list:
    """Public model ids, including aliases, with no duplicates."""
    ids = list(MODELS)
    for alias in MODEL_ALIASES:
        if alias not in ids:
            ids.append(alias)
    return ids
