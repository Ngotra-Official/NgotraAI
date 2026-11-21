"""Authentication endpoints."""
import time
import uuid
import secrets
import re
from fastapi import APIRouter, HTTPException, Depends, Response, status
from pydantic import BaseModel
from ..database import get_db_connection
from ..auth import verify_password, get_password_hash, create_access_token, get_current_user
from ..unlock_service import get_user_access_status

router = APIRouter(prefix="/api/auth", tags=["auth"])

class RegisterRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

def validate_email_format(email: str):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Invalid email address format")

@router.post("/register")
def register(req: RegisterRequest, response: Response):
    email = req.email.strip().lower()
    validate_email_format(email)
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Email is already registered")

    now = time.time()
    hashed = get_password_hash(req.password)
    # New user starts with 0 tokens; they earn tokens by completing the 3-ad cycle.
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (email, password_hash, role, is_active, is_vip, created_at, last_login_at)
        VALUES (?, ?, 'user', 1, 0, ?, ?)
    """, (email, hashed, now, now))
    user_id = cursor.lastrowid

    # Automatically generate first API Key
    key_secret = f"sk-ngo-{secrets.token_hex(16)}"
    key_prefix = key_secret[:12] + "..."
    cursor.execute("""
        INSERT INTO api_keys (user_id, key_prefix, key_secret, name, is_active, created_at)
        VALUES (?, ?, ?, 'Default Key', 1, ?)
    """, (user_id, key_prefix, key_secret, now))

    conn.commit()
    conn.close()

    token = create_access_token({"sub": email, "user_id": user_id, "role": "user"})
    response.set_cookie(
        key="ngotra_token",
        value=token,
        httponly=True,
        max_age=30 * 86400,
        samesite="lax",
        secure=False
    )

    return {
        "success": True,
        "token": token,
        "user": {
            "id": user_id,
            "email": email,
            "role": "user",
            "is_vip": False
        },
        "initial_key": key_secret
    }

@router.post("/login")
def login(req: LoginRequest, response: Response):
    email = req.email.strip().lower()
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if not row or not verify_password(req.password, row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not row["is_active"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Your account has been deactivated. Please contact support.")

    now = time.time()
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    conn.close()

    user = dict(row)
    token = create_access_token({"sub": email, "user_id": user["id"], "role": user["role"]})
    response.set_cookie(
        key="ngotra_token",
        value=token,
        httponly=True,
        max_age=30 * 86400,
        samesite="lax",
        secure=False
    )

    return {
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "is_vip": bool(user["is_vip"])
        }
    }

@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    status_info = get_user_access_status(current_user)
    return {
        "user": {
            "id": current_user["id"],
            "email": current_user["email"],
            "role": current_user["role"],
            "is_vip": bool(current_user["is_vip"]),
            "created_at": current_user["created_at"],
            "access_status": status_info
        }
    }

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("ngotra_token")
    return {"success": True, "message": "Logged out successfully"}
