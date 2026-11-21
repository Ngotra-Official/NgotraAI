"""Plan/quota info and admin plan management."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import get_current_user, get_current_admin
from ..plans import PLANS, quota_status, set_user_plan, get_user_plan, plan_details

router = APIRouter(tags=["plans"])


class PlanRequest(BaseModel):
    plan: str
    user_id: int


@router.get("/api/user/plan")
def my_plan(current_user: dict = Depends(get_current_user)):
    return quota_status(current_user["id"])


@router.get("/api/public/plans")
def public_plans():
    return {"plans": [plan_details(p) for p in PLANS]}


@router.get("/api/admin/plans")
def admin_plans(admin: dict = Depends(get_current_admin)):
    return {"plans": [plan_details(p) for p in PLANS]}


@router.post("/api/admin/plans/assign")
def assign_plan(req: PlanRequest, admin: dict = Depends(get_current_admin)):
    try:
        set_user_plan(req.user_id, req.plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "user_id": req.user_id, "plan": get_user_plan(req.user_id)}
