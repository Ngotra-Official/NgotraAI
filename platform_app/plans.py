"""Named rate-limit plans (quotas) for users.

Each plan has a name, a monthly token allowance and a requests-per-minute cap.
Users are on the Free plan by default; admins can change it. Ad rewards add to
the token balance but still respect the plan's monthly ceiling.
"""
import time
from typing import Dict, Any

from .database import get_db_connection

PLANS: Dict[str, Dict[str, Any]] = {
    "free": {
        "name": "Free",
        "monthly_tokens": 2_000_000,
        "requests_per_minute": 20,
        "description": "Watch ads to earn tokens. 2M tokens/month, 20 req/min.",
    },
    "starter": {
        "name": "Starter",
        "monthly_tokens": 10_000_000,
        "requests_per_minute": 60,
        "description": "10M tokens/month, 60 req/min.",
    },
    "pro": {
        "name": "Pro",
        "monthly_tokens": 50_000_000,
        "requests_per_minute": 180,
        "description": "50M tokens/month, 180 req/min.",
    },
    "unlimited": {
        "name": "Unlimited",
        "monthly_tokens": 0,          # 0 = no monthly cap
        "requests_per_minute": 0,     # 0 = no per-minute cap
        "description": "No quotas.",
    },
}


def month_key() -> int:
    """Current month as YYYYMM integer."""
    t = time.gmtime()
    return t.tm_year * 100 + t.tm_mon


def get_user_plan(user_id: int) -> str:
    conn = get_db_connection()
    row = conn.execute("SELECT plan FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return (row["plan"] if row else "") or "free"


def set_user_plan(user_id: int, plan: str) -> None:
    if plan not in PLANS:
        raise ValueError(f"Unknown plan: {plan}")
    conn = get_db_connection()
    conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
    conn.commit()
    conn.close()


def plan_details(plan: str) -> Dict[str, Any]:
    cfg = PLANS.get(plan, PLANS["free"])
    return dict(cfg, id=plan)


def monthly_usage(user_id: int) -> int:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT used FROM user_monthly_usage WHERE user_id = ? AND month = ?",
        (user_id, month_key()),
    ).fetchone()
    conn.close()
    return int(row["used"] if row else 0)


def add_monthly_usage(user_id: int, tokens: int) -> None:
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO user_monthly_usage (user_id, month, used)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, month) DO UPDATE SET used = used + excluded.used
    """, (user_id, month_key(), max(0, int(tokens))))
    conn.commit()
    conn.close()


def check_quota(user_id: int) -> Dict[str, Any]:
    """Return {"allowed", "reason", "plan", "monthly_limit", "used"}."""
    plan = get_user_plan(user_id)
    details = plan_details(plan)
    limit = int(details["monthly_tokens"])
    used = monthly_usage(user_id)
    if limit and used >= limit:
        return {
            "allowed": False,
            "reason": (
                f"Monthly quota reached on the {details['name']} plan "
                f"({used:,}/{limit:,} tokens). Upgrade your plan or wait for next month."
            ),
            "plan": plan,
            "monthly_limit": limit,
            "used": used,
        }
    return {"allowed": True, "plan": plan, "monthly_limit": limit, "used": used}


def quota_status(user_id: int) -> Dict[str, Any]:
    details = plan_details(get_user_plan(user_id))
    return {
        "plan": details["id"],
        "plan_name": details["name"],
        "description": details["description"],
        "monthly_limit": details["monthly_tokens"],
        "monthly_used": monthly_usage(user_id),
        "requests_per_minute": details["requests_per_minute"],
    }
