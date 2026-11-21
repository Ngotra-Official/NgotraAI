"""Named, per-user, per-plan request rate limiting (sliding window)."""
import time
import threading

from .plans import plan_details, get_user_plan

_windows = {}          # user_id -> list of request timestamps
_lock = threading.Lock()


def is_allowed(user_id: int) -> tuple:
    """Return (allowed, retry_after_seconds)."""
    rpm = int(plan_details(get_user_plan(user_id))["requests_per_minute"])
    if rpm <= 0:
        return True, 0
    now = time.time()
    with _lock:
        q = _windows.setdefault(user_id, [])
        cutoff = now - 60
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= rpm:
            return False, max(1, int(q[0] + 60 - now) + 1)
        q.append(now)
        return True, 0


def cleanup(max_idle: int = 300):
    now = time.time()
    with _lock:
        for uid in list(_windows):
            q = _windows[uid]
            if not q or q[-1] < now - max_idle:
                del _windows[uid]
