"""Shared helpers for OpenAI-compatible request options.

Gemini Web has no direct controls for most OpenAI sampling parameters, so the
ones that can be honoured are applied to the prompt/output here. Unknown
options are accepted and ignored instead of causing hard failures, which keeps
the gateway compatible with a wide range of clients.
"""
import json
from typing import Any, Dict, List, Optional


def token_count(text: Optional[str]) -> int:
    """Cheap, deterministic token estimate (4 characters ~= 1 token)."""
    return len(text or "") // 4


def get_max_tokens(body: Dict[str, Any]) -> int:
    value = body.get("max_completion_tokens")
    if value is None:
        value = body.get("max_tokens")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def get_stop(body: Dict[str, Any]) -> List[str]:
    stop = body.get("stop")
    if isinstance(stop, str):
        return [stop] if stop else []
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str) and s]
    return []


def augment_prompt(prompt: str, body: Dict[str, Any]) -> str:
    """Append constraints from options that must be expressed in the prompt."""
    extras: List[str] = []
    fmt = body.get("response_format")
    if isinstance(fmt, dict):
        fmt_type = fmt.get("type")
        if fmt_type == "json_object":
            extras.append("Respond with a single valid JSON object and nothing else. No markdown fences.")
        elif fmt_type == "json_schema":
            schema = (fmt.get("json_schema") or {}).get("schema")
            if schema:
                extras.append(
                    "Respond with JSON that strictly matches this schema and nothing else:\n"
                    + json.dumps(schema, ensure_ascii=False)
                )
    if not extras:
        return prompt
    return prompt + "\n\n" + "\n".join(extras)


def clip_text(text: str, body: Dict[str, Any]) -> str:
    """Apply stop sequences and max_tokens to a completed response."""
    if not text:
        return text
    stop_at = [text.find(s) for s in get_stop(body) if text.find(s) >= 0]
    if stop_at:
        text = text[:min(stop_at)]
    max_tokens = get_max_tokens(body)
    if max_tokens > 0:
        limit_chars = max_tokens * 4
        if len(text) > limit_chars:
            text = text[:limit_chars]
    return text


def stream_exceeds_limit(full_text: str, body: Dict[str, Any]) -> bool:
    """True when an incremental stream has hit a stop sequence or max_tokens."""
    if not full_text:
        return False
    if any(s in full_text for s in get_stop(body)):
        return True
    max_tokens = get_max_tokens(body)
    return bool(max_tokens and token_count(full_text) >= max_tokens)


def build_usage(prompt: str, text: str) -> Dict[str, int]:
    p = token_count(prompt)
    c = token_count(text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
