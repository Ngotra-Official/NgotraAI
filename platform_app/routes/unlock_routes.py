"""Ad Unlocker API routes."""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from ..auth import get_current_user
from ..unlock_service import (
    start_or_get_unlock_session,
    record_ad_click,
    verify_and_advance_ad_step
)

router = APIRouter(prefix="/api/unlock", tags=["unlock"])

class SessionActionRequest(BaseModel):
    session_id: str

@router.get("/session")
def get_unlock_session(current_user: dict = Depends(get_current_user)):
    return start_or_get_unlock_session(current_user["id"])

@router.post("/click")
def record_click(req: SessionActionRequest, current_user: dict = Depends(get_current_user)):
    result = record_ad_click(req.session_id, current_user["id"])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.post("/verify")
def verify_step(req: SessionActionRequest, current_user: dict = Depends(get_current_user)):
    result = verify_and_advance_ad_step(req.session_id, current_user["id"])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result
