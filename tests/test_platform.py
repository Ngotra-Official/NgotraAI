"""Comprehensive test suite for Ngotra AI Platform."""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from platform_app.main import app
from platform_app.database import get_db_connection, get_setting

def test_full_platform():
    client = TestClient(app)

    print(">>> 1. Testing Root Pages & Page Guards (HTML)...")
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "Ngotra" in res_index.text
    print("   ✓ Landing page OK")

    # Anonymous visitors must be redirected on the server, before any page loads.
    res_dash_anon = client.get("/dashboard", follow_redirects=False)
    assert res_dash_anon.status_code == 303
    assert res_dash_anon.headers["location"] == "/"
    res_admin_anon = client.get("/admin", follow_redirects=False)
    assert res_admin_anon.status_code == 303
    assert res_admin_anon.headers["location"] == "/"
    print("   ✓ Anonymous dashboard/admin requests redirect to landing page")

    # Authenticated admin can load both shells.
    res_login_page = client.post("/api/auth/login", json={
        "email": "admin@ngotra.site",
        "password": "Admin@Ngotra2026!"
    })
    assert res_login_page.status_code == 200, res_login_page.text
    page_headers = {"Authorization": f"Bearer {res_login_page.json()['token']}"}

    res_dash = client.get("/dashboard", headers=page_headers)
    assert res_dash.status_code == 200
    assert "Developer Dashboard" in res_dash.text
    print("   ✓ Dashboard page OK (authenticated)")

    res_admin = client.get("/admin", headers=page_headers)
    assert res_admin.status_code == 200
    assert "Admin Control Center" in res_admin.text
    print("   ✓ Admin page OK (authenticated admin)")

    print("\n>>> 2. Testing Public Config...")
    res_conf = client.get("/api/public/config")
    assert res_conf.status_code == 200
    conf_data = res_conf.json()
    assert conf_data["site_domain"] == "ngotra.site"
    assert conf_data["ads_required_count"] == 3
    assert conf_data["tokens_per_cycle"] == 500000
    assert conf_data["ad_cycle_cooldown_hours"] == 1
    print("   ✓ Public config OK:", conf_data["site_domain"])

    print("\n>>> 3. Testing Authentication...")
    # Admin login
    res_login = client.post("/api/auth/login", json={
        "email": "admin@ngotra.site",
        "password": "Admin@Ngotra2026!"
    })
    assert res_login.status_code == 200, res_login.text
    admin_token = res_login.json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print("   ✓ Admin login OK")

    # Register new normal user
    test_email = f"user_{int(time.time())}@example.com"
    res_reg = client.post("/api/auth/register", json={
        "email": test_email,
        "password": "TestPassword123!"
    })
    assert res_reg.status_code == 200, res_reg.text
    reg_data = res_reg.json()
    user_token = reg_data["token"]
    user_key = reg_data["initial_key"]
    user_headers = {"Authorization": f"Bearer {user_token}"}
    print(f"   ✓ New user registered OK: {test_email}, Key: {user_key[:14]}...")

    res_admin_as_user = client.get("/admin", headers=user_headers, follow_redirects=False)
    assert res_admin_as_user.status_code == 303
    assert res_admin_as_user.headers["location"] == "/dashboard"
    print("   ✓ Non-admin user redirected away from admin pages")

    print("\n>>> 4. Testing Initial Empty Balance (Before Watching Ads)...")
    res_status = client.get("/api/user/status", headers=user_headers)
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["is_unlocked"] is False
    assert status_data["token_balance"] == 0
    assert status_data["can_watch_ads"] is True
    print("   ✓ New user starts with 0 tokens (must watch 3 ads)")

    # Test API request with expired key -> Expect HTTP 402
    res_api_expired = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {user_key}"}, json={
        "model": "gemini-3.7-flash",
        "messages": [{"role": "user", "content": "hi"}]
    })
    assert res_api_expired.status_code == 402, res_api_expired.text
    err_detail = res_api_expired.json()["detail"]["error"]
    assert err_detail["type"] == "no_tokens"
    assert "https://ngotra.site/dashboard" in err_detail["unlock_url"]
    print("   ✓ Empty-balance key correctly received HTTP 402 with unlock instructions!")

    # A leftover time pass must never grant access without tokens.
    conn = get_db_connection()
    conn.execute("UPDATE users SET access_expires_at = ? WHERE id = ?", (time.time() + 7200, reg_data["user"]["id"]))
    conn.commit()
    conn.close()
    res_old_pass = client.get("/v1/models", headers={"Authorization": f"Bearer {user_key}"})
    assert res_old_pass.status_code == 402, res_old_pass.text
    print("   ✓ Old time-pass timestamp does NOT bypass the token requirement")

    # No VIP/admin bypass: temporarily zero the admin balance, then restore it.
    conn = get_db_connection()
    admin_balance = conn.execute("SELECT token_balance FROM users WHERE id = 1").fetchone()["token_balance"]
    conn.execute("UPDATE users SET token_balance = 0 WHERE id = 1")
    conn.commit()
    conn.close()
    try:
        res_admin_api = client.get("/v1/models", headers={"Authorization": "Bearer sk-ngo-admin-master-key"})
        assert res_admin_api.status_code == 402, res_admin_api.text
    finally:
        conn = get_db_connection()
        conn.execute("UPDATE users SET token_balance = ? WHERE id = 1", (admin_balance,))
        conn.commit()
        conn.close()
    print("   ✓ Admin/VIP keys with 0 tokens are also blocked (no bypass)")

    print("\n>>> 5. Testing 3-Step Ad Unlock Flow...")
    # Get unlock session
    res_sess = client.get("/api/unlock/session", headers=user_headers)
    assert res_sess.status_code == 200
    sess = res_sess.json()
    sess_id = sess["session_id"]
    print(f"   ✓ Started ad session {sess_id}, step: {sess['current_step']}")

    # Ad 1: Click & Verify
    client.post("/api/unlock/click", headers=user_headers, json={"session_id": sess_id})
    # Fast forward step_started_at in db to bypass the 10s wait in unit test
    conn = get_db_connection()
    conn.execute("UPDATE ad_sessions SET step_started_at = ? WHERE session_id = ?", (time.time() - 15, sess_id))
    conn.commit()
    conn.close()

    res_v1 = client.post("/api/unlock/verify", headers=user_headers, json={"session_id": sess_id})
    assert res_v1.status_code == 200
    assert res_v1.json()["current_step"] == 1
    assert res_v1.json()["completed"] is False
    print("   ✓ Ad 1 verified! Progress: 1/3")

    # Ad 2: Click & Verify
    client.post("/api/unlock/click", headers=user_headers, json={"session_id": sess_id})
    conn = get_db_connection()
    conn.execute("UPDATE ad_sessions SET step_started_at = ? WHERE session_id = ?", (time.time() - 15, sess_id))
    conn.commit()
    conn.close()

    res_v2 = client.post("/api/unlock/verify", headers=user_headers, json={"session_id": sess_id})
    assert res_v2.status_code == 200
    assert res_v2.json()["current_step"] == 2
    assert res_v2.json()["completed"] is False
    print("   ✓ Ad 2 verified! Progress: 2/3")

    # Ad 3: Click & Verify (Final)
    client.post("/api/unlock/click", headers=user_headers, json={"session_id": sess_id})
    conn = get_db_connection()
    conn.execute("UPDATE ad_sessions SET step_started_at = ? WHERE session_id = ?", (time.time() - 15, sess_id))
    conn.commit()
    conn.close()

    res_v3 = client.post("/api/unlock/verify", headers=user_headers, json={"session_id": sess_id})
    assert res_v3.status_code == 200
    v3_data = res_v3.json()
    assert v3_data["completed"] is True
    assert v3_data["unlocked"] is True
    assert v3_data["tokens_added"] == 500000
    print(f"   ✓ Ad 3 verified! TOKENS GRANTED! {v3_data['message']}")

    # Check user status after unlock
    res_status2 = client.get("/api/user/status", headers=user_headers)
    status2_data = res_status2.json()
    assert status2_data["is_unlocked"] is True
    assert status2_data["token_balance"] == 500000
    assert status2_data["can_watch_ads"] is False
    assert status2_data["cooldown_remaining_seconds"] > 3500  # ~3600 seconds (1 hour)
    print(f"   ✓ Verified balance: {status2_data['token_balance']:,} tokens, cooldown {status2_data['cooldown_remaining_formatted']}")

    # A new ad cycle must be blocked during the cooldown
    res_cycle = client.get("/api/unlock/session", headers=user_headers)
    assert res_cycle.status_code == 200
    assert res_cycle.json()["can_unlock"] is False
    print("   ✓ Ad cycle correctly blocked during the 1-hour cooldown")

    print("\n>>> 6. Testing Admin Management Endpoints...")
    res_stats = client.get("/api/admin/stats", headers=admin_headers)
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["total_users"] >= 2
    print(f"   ✓ Admin stats OK: {stats['total_users']} users, {stats['active_unlocks']} funded users")

    # Admin manual token grant
    user_id = reg_data["user"]["id"]
    res_ext = client.post(f"/api/admin/users/{user_id}/extend", headers=admin_headers, json={"tokens": 500000})
    assert res_ext.status_code == 200
    print("   ✓ Admin manual token grant (+500k) OK")

    # Admin reset token balance
    res_rst = client.post(f"/api/admin/users/{user_id}/reset", headers=admin_headers)
    assert res_rst.status_code == 200
    print("   ✓ Admin reset user token balance OK")

    # Give user tokens for step 7
    client.post(f"/api/admin/users/{user_id}/extend", headers=admin_headers, json={"tokens": 500000})

    # Admin update settings
    res_upd = client.post("/api/admin/settings", headers=admin_headers, json={
        "settings": {
            "site_domain": "ngotra.site",
            "ads_required_count": "3",
            "tokens_per_cycle": "500000",
            "ad_cycle_cooldown_hours": "1",
            "adsterra_direct_links": "https://www.profitablecpmrate.com/example1\nhttps://www.profitablecpmrate.com/example2"
        }
    })
    assert res_upd.status_code == 200
    print("   ✓ Admin settings update OK")

    # Gate: connect a verified Gemini account before API access.
    from unittest import mock
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_cookie = ?, gemini_cookie_verified_at = ? WHERE id = ?",
        ("__Secure-1PSID=test; SAPISID=test; SID=test", time.time(), user_id),
    )
    conn.commit()
    conn.close()
    valid_conn = {"connected": True, "valid": True, "reason": "ok"}
    cookie_patch = mock.patch("platform_app.gemini_connection.connection_state", return_value=valid_conn)
    cookie_patch.start()

    print("\n>>> 7. Testing OpenAI Compatible Endpoints...")
    # /v1/models
    res_models = client.get("/v1/models", headers={"Authorization": f"Bearer {user_key}"})
    assert res_models.status_code == 200
    models_list = res_models.json()["data"]
    model_ids = [m["id"] for m in models_list]
    assert "gemini-3.7-flash" in model_ids
    assert "gemini-3.5-flash-thinking" in model_ids
    print(f"   ✓ /v1/models OK: {len(model_ids)} models available")

    print("\n=======================================================")
    print("   ALL TESTS PASSED! Ngotra AI Platform is 100% Operational!   ")
    print("=======================================================")

if __name__ == "__main__":
    test_full_platform()
