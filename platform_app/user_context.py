"""Short-TTL cache of per-user gateway context.

authenticate_api_key previously hit the DB 6+ times per request. This cache
keeps the hot path to a single API-key lookup plus in-memory checks. Entries
expire quickly and are invalidated whenever a user changes their connection.
"""
import time
import threading

_LOCK = threading.Lock()
_CACHE = {}  # user_id -> (expires_at, dict)
TTL = 20  # seconds


def get(user_id: int, loader) -> dict:
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(user_id)
        if hit and hit[0] > now:
            return dict(hit[1])
    data = loader(user_id)
    with _LOCK:
        _CACHE[user_id] = (now + TTL, dict(data))
    return data


def invalidate(user_id: int) -> None:
    with _LOCK:
        _CACHE.pop(user_id, None)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
