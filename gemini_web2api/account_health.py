"""Per-account upstream health tracking.

When Google rate-limits or rejects one Gemini account, we cool it down and let
the next request use another account (shared pool) instead of failing or
hanging. State is in-memory and process-local, which is fine for a single
worker; multiple workers simply have independent cooldowns.
"""
import hashlib
import threading
import time

_lock = threading.Lock()
_accounts = {}  # fingerprint -> {"until": float, "fails": int, "reason": str}


def fingerprint(cookie: str) -> str:
    return hashlib.sha256((cookie or "").encode()).hexdigest()[:16]


def mark_failure(cookie: str, reason: str = "", cooldown: int = 60) -> None:
    key = fingerprint(cookie)
    if not key or key == fingerprint(""):
        return
    with _lock:
        entry = _accounts.setdefault(key, {"until": 0.0, "fails": 0, "reason": ""})
        entry["fails"] += 1
        entry["reason"] = reason or "failure"
        # Longer cooldown after repeated failures.
        delay = cooldown * min(4, max(1, entry["fails"]))
        entry["until"] = max(entry["until"], time.time() + delay)


def mark_success(cookie: str) -> None:
    key = fingerprint(cookie)
    if not key:
        return
    with _lock:
        _accounts.pop(key, None)


def health(cookie: str) -> dict:
    key = fingerprint(cookie)
    with _lock:
        entry = _accounts.get(key)
        if not entry:
            return {"ok": True, "fails": 0, "reason": ""}
        remaining = max(0, int(entry["until"] - time.time()))
        return {
            "ok": remaining == 0,
            "cooldown_remaining": remaining,
            "fails": entry["fails"],
            "reason": entry["reason"],
        }


def pick_healthy(candidates: list) -> str:
    """Return the first cookie that is not cooling down."""
    for cookie in candidates:
        if cookie and health(cookie)["ok"]:
            return cookie
    return ""
