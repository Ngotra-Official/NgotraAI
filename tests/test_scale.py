"""Concurrency and scale-safety tests."""
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from platform_app.database import get_db_connection
from platform_app.unlock_service import verify_and_advance_ad_step
from platform_app.main import _rate_limiter
from gemini_web2api.config import CONFIG
from gemini_web2api.gemini import load_cookie, request_cookie_index


def test_ad_reward_is_atomic_under_concurrency():
    email = f"scale_{uuid.uuid4().hex[:10]}@example.com"
    now = time.time()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO users (email, password_hash, role, is_active, is_vip, token_balance, created_at) "
        "VALUES (?, ?, 'user', 1, 0, 0, ?)",
        (email, "x", now),
    )
    uid = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
    sid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO ad_sessions (user_id, session_id, current_step, required_steps, "
        "tokens_to_grant, step_started_at, status, created_at) VALUES (?, ?, 2, 3, 500000, ?, 'pending', ?)",
        (uid, sid, now - 30, now),
    )
    conn.commit()
    conn.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: verify_and_advance_ad_step(sid, uid), range(6)))

    successes = [r for r in results if r.get("success") and r.get("completed")]
    assert len(successes) == 1, results
    assert successes[0]["tokens_added"] == 500000

    conn = get_db_connection()
    balance = conn.execute("SELECT token_balance FROM users WHERE id = ?", (uid,)).fetchone()["token_balance"]
    ads = conn.execute("SELECT total_ads_watched FROM users WHERE id = ?", (uid,)).fetchone()["total_ads_watched"]
    sessions = conn.execute(
        "SELECT COUNT(*) c FROM ad_sessions WHERE session_id = ? AND status = 'completed'", (sid,)
    ).fetchone()["c"]
    conn.close()
    assert balance == 500000
    assert ads == 1
    assert sessions == 1


def test_rate_limiter_drops_idle_windows():
    limiter = type(_rate_limiter)()
    limiter.is_allowed("127.0.0.1", "test", 5, 60)
    assert len(limiter._windows) == 1
    limiter.cleanup(max_idle_sec=-1)
    assert len(limiter._windows) == 0


def test_cookie_pool_uses_request_selection():
    original = dict(CONFIG)
    token = request_cookie_index.set(1)
    try:
        CONFIG["cookie_pool"] = [
            "SAPISID=one; other=1",
            "SAPISID=two; other=2",
            "SAPISID=three; other=3",
        ]
        cookie, sapisid = load_cookie()
        assert sapisid == "two"
        assert "SAPISID=two" in cookie
    finally:
        request_cookie_index.reset(token)
        CONFIG.clear()
        CONFIG.update(original)


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            print(f">>> {_name}")
            _fn()
            print("    OK")
    print("\nALL SCALE TESTS PASSED")
