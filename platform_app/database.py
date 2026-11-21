"""SQLite Database layer for Ngotra AI Platform."""
import os
import sqlite3
import time
import base64
import hashlib
from typing import Any, Dict, List, Optional
from passlib.context import CryptContext

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DB_DIR, "platform.db")

# Per-user Gemini cookies are encrypted at rest. Override with the
# NGOTRA_COOKIE_KEY environment variable in production.
_COOKIE_SECRET = os.environ.get(
    "NGOTRA_COOKIE_KEY", "ngotra-ai-jwt-secret-key-prod-ngotra.site-2026"
)
_COOKIE_FERNET = (
    Fernet(base64.urlsafe_b64encode(hashlib.sha256(_COOKIE_SECRET.encode()).digest()))
    if _HAS_FERNET else None
)


def encrypt_cookie(cookie: str) -> str:
    if not cookie:
        return ""
    if _COOKIE_FERNET:
        return _COOKIE_FERNET.encrypt(cookie.encode()).decode()
    # Fallback only when cryptography is unavailable.
    return base64.b64encode(cookie.encode()).decode()


def decrypt_cookie(value: str) -> str:
    if not value:
        return ""
    try:
        if _COOKIE_FERNET and value.startswith("gAAAAA"):
            return _COOKIE_FERNET.decrypt(value.encode()).decode()
        return base64.b64decode(value).decode()
    except Exception:
        return ""

def get_db_connection() -> sqlite3.Connection:
    """Open a connection tuned for many concurrent readers/writers.

    WAL lets readers and writers work at the same time, and busy_timeout makes
    writers wait for the lock instead of failing immediately under load.
    """
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def cleanup_old_data(log_days: int = 30, session_days: int = 7) -> None:
    """Bound table growth so long-running deployments stay fast."""
    conn = get_db_connection()
    now = time.time()
    conn.execute("DELETE FROM api_logs WHERE created_at < ?", (now - log_days * 86400,))
    conn.execute(
        "DELETE FROM ad_sessions WHERE status != 'pending' AND created_at < ?",
        (now - session_days * 86400,),
    )
    conn.commit()
    conn.close()

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        is_active INTEGER DEFAULT 1,
        is_vip INTEGER DEFAULT 0,
        token_balance INTEGER DEFAULT 0,
        total_tokens_granted INTEGER DEFAULT 0,
        total_tokens_used INTEGER DEFAULT 0,
        last_ad_cycle_at REAL DEFAULT 0.0,
        gemini_cookie TEXT DEFAULT '',
        gemini_cookie_at REAL DEFAULT 0.0,
        gemini_cookie_verified_at REAL DEFAULT 0.0,
        gemini_auth_user TEXT DEFAULT '',
        gemini_xsrf_token TEXT DEFAULT '',
        gemini_auth_checked INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'free',
        gemini_api_key TEXT DEFAULT '',
        gemini_api_key_at REAL DEFAULT 0.0,
        gemini_oauth_access_token TEXT DEFAULT '',
        gemini_oauth_refresh_token TEXT DEFAULT '',
        gemini_oauth_expiry REAL DEFAULT 0.0,
        gemini_oauth_email TEXT DEFAULT '',
        gemini_oauth_state TEXT DEFAULT '',
        total_ads_watched INTEGER DEFAULT 0,
        total_requests INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        last_login_at REAL
    );

    CREATE TABLE IF NOT EXISTS user_monthly_usage (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        month INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, month)
    );

    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        key_prefix TEXT NOT NULL,
        key_secret TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        total_requests INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        last_used_at REAL,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ad_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT UNIQUE NOT NULL,
        current_step INTEGER DEFAULT 0,
        required_steps INTEGER DEFAULT 3,
        step_started_at REAL,
        duration_minutes INTEGER DEFAULT 120,
        tokens_to_grant INTEGER DEFAULT 500000,
        status TEXT DEFAULT 'pending',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS api_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        key_prefix TEXT,
        endpoint TEXT,
        model TEXT,
        status_code INTEGER,
        duration_ms REAL,
        tokens_estimated INTEGER DEFAULT 0,
        ip_address TEXT,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
    CREATE INDEX IF NOT EXISTS idx_api_keys_secret ON api_keys(key_secret);
    CREATE INDEX IF NOT EXISTS idx_api_logs_created ON api_logs(created_at);
    CREATE INDEX IF NOT EXISTS idx_api_logs_user ON api_logs(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
    CREATE INDEX IF NOT EXISTS idx_ad_sessions_user ON ad_sessions(user_id);
    """)

    # Migration for databases created with the old time-based pass system
    existing_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    token_columns = {
        "token_balance": "ALTER TABLE users ADD COLUMN token_balance INTEGER DEFAULT 0",
        "total_tokens_granted": "ALTER TABLE users ADD COLUMN total_tokens_granted INTEGER DEFAULT 0",
        "total_tokens_used": "ALTER TABLE users ADD COLUMN total_tokens_used INTEGER DEFAULT 0",
        "last_ad_cycle_at": "ALTER TABLE users ADD COLUMN last_ad_cycle_at REAL DEFAULT 0.0",
        "gemini_cookie": "ALTER TABLE users ADD COLUMN gemini_cookie TEXT DEFAULT ''",
        "gemini_cookie_at": "ALTER TABLE users ADD COLUMN gemini_cookie_at REAL DEFAULT 0.0",
        "gemini_cookie_verified_at": "ALTER TABLE users ADD COLUMN gemini_cookie_verified_at REAL DEFAULT 0.0",
        "gemini_auth_user": "ALTER TABLE users ADD COLUMN gemini_auth_user TEXT DEFAULT ''",
        "gemini_xsrf_token": "ALTER TABLE users ADD COLUMN gemini_xsrf_token TEXT DEFAULT ''",
        "gemini_auth_checked": "ALTER TABLE users ADD COLUMN gemini_auth_checked INTEGER DEFAULT 0",
        "plan": "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
        "gemini_api_key": "ALTER TABLE users ADD COLUMN gemini_api_key TEXT DEFAULT ''",
        "gemini_api_key_at": "ALTER TABLE users ADD COLUMN gemini_api_key_at REAL DEFAULT 0.0",
        "gemini_oauth_access_token": "ALTER TABLE users ADD COLUMN gemini_oauth_access_token TEXT DEFAULT ''",
        "gemini_oauth_refresh_token": "ALTER TABLE users ADD COLUMN gemini_oauth_refresh_token TEXT DEFAULT ''",
        "gemini_oauth_expiry": "ALTER TABLE users ADD COLUMN gemini_oauth_expiry REAL DEFAULT 0.0",
        "gemini_oauth_email": "ALTER TABLE users ADD COLUMN gemini_oauth_email TEXT DEFAULT ''",
        "gemini_oauth_state": "ALTER TABLE users ADD COLUMN gemini_oauth_state TEXT DEFAULT ''",
    }
    for column_name, alter_sql in token_columns.items():
        if column_name not in existing_columns:
            cursor.execute(alter_sql)

    session_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(ad_sessions)").fetchall()}
    if "tokens_to_grant" not in session_columns:
        cursor.execute("ALTER TABLE ad_sessions ADD COLUMN tokens_to_grant INTEGER DEFAULT 500000")

    # The old time-pass system is gone: clear any leftover pass timestamps so
    # they can never grant access again (including on an old running process).
    if "access_expires_at" in existing_columns:
        cursor.execute("UPDATE users SET access_expires_at = 0 WHERE access_expires_at > 0")

    # Seed Default Settings
    default_settings = {
        "site_name": "Ngotra AI",
        "site_domain": "ngotra.site",
        "api_base_url": "https://ngotra.site/v1",
        "ads_required_count": "3",
        "tokens_per_cycle": "500000",
        "ad_cycle_cooldown_hours": "1",
        "ad_cooldown_seconds": "10",
        "adsterra_direct_links": "https://www.profitablecpmrate.com/example_direct_link",
        "adsterra_banner_728x90": """<div class="ad-placeholder w-full h-[90px] bg-slate-100 border border-dashed border-slate-300 rounded flex flex-col items-center justify-center text-xs text-slate-500">
  <span class="font-semibold text-slate-700">Adsterra 728x90 Leaderboard Banner</span>
  <span>Configure script or iframe in Admin Settings</span>
</div>""",
        "adsterra_banner_300x250": """<div class="ad-placeholder w-full h-[250px] bg-slate-100 border border-dashed border-slate-300 rounded flex flex-col items-center justify-center text-xs text-slate-500 p-4 text-center">
  <span class="font-semibold text-slate-700 mb-1">Adsterra 300x250 Rectangle Banner</span>
  <span>Replace this placeholder with your Adsterra Banner script in Admin Settings</span>
</div>""",
        "adsterra_banner_468x60": """<div class="ad-placeholder w-full h-[60px] bg-slate-100 border border-dashed border-slate-300 rounded flex flex-col items-center justify-center text-xs text-slate-500">
  <span class="font-semibold text-slate-700">Adsterra 468x60 Banner</span>
</div>""",
        "adsterra_popunder_code": "",
        "adsterra_socialbar_code": "",
        "enable_ads": "true",
        "enable_banners": "true",
        "enable_popunder": "false",
        "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
        "gemini_cookie": "",
        "gemini_proxy": "",
        "google_client_id": "",
        "google_client_secret": "",
        "default_model": "gemini-3.7-flash",
        "social_discord": "https://discord.gg/ngotra",
        "social_telegram": "https://t.me/ngotra_ai",
        "social_youtube": "https://youtube.com/@ngotra_ai",
        "social_instagram": "https://instagram.com/ngotra_ai",
        "social_x": "https://x.com/ngotra_ai"
    }

    for k, v in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)", (k, v))

    # Seed Default Admin if none exists
    cursor.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
    admin = cursor.fetchone()
    if not admin:
        default_admin_email = "admin@ngotra.site"
        # Admin@Ngotra2026!
        hashed = pwd_context.hash("Admin@Ngotra2026!")
        now = time.time()
        cursor.execute("""
            INSERT INTO users (email, password_hash, role, is_active, is_vip, token_balance, total_tokens_granted, created_at)
            VALUES (?, ?, 'admin', 1, 1, 0, 0, ?)
        """, (default_admin_email, hashed, now))
        
        # Also create a default master admin key
        admin_id = cursor.lastrowid
        master_key = "sk-ngo-admin-master-key"
        cursor.execute("""
            INSERT INTO api_keys (user_id, key_prefix, key_secret, name, is_active, created_at)
            VALUES (?, ?, ?, 'Admin Master Key', 1, ?)
        """, (admin_id, "sk-ngo-admin", master_key, now))

    conn.commit()
    conn.close()

# Setting Helpers
def get_setting(key: str, default: str = "") -> str:
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row and row["value"] is not None else default

def get_all_settings() -> Dict[str, str]:
    conn = get_db_connection()
    rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] or "" for row in rows}

def update_settings(settings: Dict[str, str]):
    conn = get_db_connection()
    for k, v in settings.items():
        conn.execute("""
            INSERT INTO system_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (k, str(v)))
    conn.commit()
    conn.close()

def get_cookie_verified_at(user_id: int) -> float:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_cookie_verified_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return float(row["gemini_cookie_verified_at"] or 0.0) if row else 0.0


def mark_cookie_verified(user_id: int, verified: bool = True) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_cookie_verified_at = ? WHERE id = ?",
        (time.time() if verified else 0.0, user_id),
    )
    conn.commit()
    conn.close()


def set_user_cookie(user_id: int, cookie: str, verified: bool = False, auth_user: str = "", xsrf_token: str = "") -> float:
    """Store an encrypted Gemini cookie for a user. Returns update timestamp."""
    now = time.time()
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_cookie = ?, gemini_cookie_at = ?, "
        "gemini_cookie_verified_at = ?, gemini_auth_user = ?, gemini_xsrf_token = ?, "
        "gemini_auth_checked = 1 WHERE id = ?",
        (encrypt_cookie(cookie.strip()), now, now if verified else 0.0,
         (auth_user or "").strip(), (xsrf_token or "").strip(), user_id),
    )
    conn.commit()
    conn.close()
    return now


def get_user_cookie(user_id: int) -> str:
    """Return the decrypted Gemini cookie for a user, or '' if none."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT gemini_cookie FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return decrypt_cookie(row["gemini_cookie"]) if row else ""


def set_user_oauth(user_id: int, access_token: str, refresh_token: str,
                   expires_in: int, email: str = "") -> None:
    conn = get_db_connection()
    conn.execute("""
        UPDATE users
        SET gemini_oauth_access_token = ?, gemini_oauth_refresh_token = ?,
            gemini_oauth_expiry = ?, gemini_oauth_email = ?, gemini_oauth_state = ''
        WHERE id = ?
    """, (
        encrypt_cookie(access_token),
        encrypt_cookie(refresh_token) if refresh_token else "",
        time.time() + max(0, int(expires_in) - 60),
        email or "",
        user_id,
    ))
    conn.commit()
    conn.close()


def set_oauth_state(user_id: int, state: str) -> None:
    conn = get_db_connection()
    conn.execute("UPDATE users SET gemini_oauth_state = ? WHERE id = ?", (state, user_id))
    conn.commit()
    conn.close()


def clear_user_oauth(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute("""
        UPDATE users SET gemini_oauth_access_token = '', gemini_oauth_refresh_token = '',
            gemini_oauth_expiry = 0, gemini_oauth_email = '', gemini_oauth_state = ''
        WHERE id = ?
    """, (user_id,))
    conn.commit()
    conn.close()


def set_user_api_key(user_id: int, key: str) -> float:
    now = time.time()
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_api_key = ?, gemini_api_key_at = ? WHERE id = ?",
        (encrypt_cookie(key.strip()), now, user_id),
    )
    conn.commit()
    conn.close()
    return now


def get_user_api_key(user_id: int) -> str:
    conn = get_db_connection()
    row = conn.execute("SELECT gemini_api_key FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return decrypt_cookie(row["gemini_api_key"]) if row else ""


def clear_user_api_key(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_api_key = '', gemini_api_key_at = 0 WHERE id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def clear_user_cookie(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET gemini_cookie = '', gemini_cookie_at = 0, "
        "gemini_cookie_verified_at = 0, gemini_auth_user = '', gemini_xsrf_token = '', "
        "gemini_auth_checked = 0 WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


# Auto-initialize database schema and defaults
init_db()
