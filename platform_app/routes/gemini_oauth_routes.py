"""Google OAuth connect routes for 'Connect Gemini Account'."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse

from ..auth import get_current_user
from ..database import clear_user_oauth
from ..gemini_oauth import begin_auth, complete_auth, connection_status, is_configured

router = APIRouter(tags=["gemini-oauth"])


@router.get("/api/auth/gemini/connect")
def gemini_connect(request: Request, current_user: dict = Depends(get_current_user)):
    url = begin_auth(current_user["id"], str(request.base_url))
    return RedirectResponse(url=url, status_code=303)


@router.get("/api/auth/gemini/callback")
def gemini_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    dashboard = "/dashboard/settings"
    if error:
        return RedirectResponse(url=dashboard + "?gemini=error", status_code=303)
    # Identify the user from the signed state we generated. get_current_user
    # cannot be used here because the browser arrives without the API header,
    # but the session cookie is present.
    from ..auth import get_current_user_optional
    user = get_current_user_optional(request)
    if not user:
        return RedirectResponse(url="/?gemini=login", status_code=303)

    result = complete_auth(user["id"], code, state, str(request.base_url))
    if result.get("ok"):
        return RedirectResponse(url=dashboard + "?gemini=connected", status_code=303)
    return RedirectResponse(url=dashboard + "?gemini=error", status_code=303)


@router.get("/api/user/gemini-connection")
def gemini_connection(current_user: dict = Depends(get_current_user)):
    return connection_status(current_user["id"])


@router.post("/api/user/gemini-connection/verify")
def gemini_connection_verify(current_user: dict = Depends(get_current_user)):
    return connection_status(current_user["id"])


@router.delete("/api/user/gemini-connection")
def gemini_disconnect(current_user: dict = Depends(get_current_user)):
    clear_user_oauth(current_user["id"])
    return {"success": True, "message": "Gemini account disconnected."}


@router.get("/api/public/gemini-oauth-config")
def gemini_oauth_public_config():
    return {"configured": is_configured()}
