"""Public configuration and health routes."""
from fastapi import APIRouter
from gemini_web2api.models import MODELS
from ..database import get_setting

router = APIRouter(prefix="/api/public", tags=["public"])

@router.get("/config")
def public_config():
    enable_ads = get_setting("enable_ads", "true").lower() == "true"
    enable_banners = get_setting("enable_banners", "true").lower() == "true"
    enable_popunder = get_setting("enable_popunder", "false").lower() == "true"

    return {
        "site_name": get_setting("site_name", "Ngotra AI"),
        "site_domain": get_setting("site_domain", "ngotra.site"),
        "api_base_url": get_setting("api_base_url", "https://ngotra.site/v1"),
        "ads_required_count": int(get_setting("ads_required_count", "3")),
        "tokens_per_cycle": int(get_setting("tokens_per_cycle", "500000")),
        "ad_cycle_cooldown_hours": float(get_setting("ad_cycle_cooldown_hours", "1")),
        "enable_ads": enable_ads,
        "enable_banners": enable_banners,
        "enable_popunder": enable_popunder,
        "banner_728x90": get_setting("adsterra_banner_728x90", "") if enable_banners else "",
        "banner_300x250": get_setting("adsterra_banner_300x250", "") if enable_banners else "",
        "banner_468x60": get_setting("adsterra_banner_468x60", "") if enable_banners else "",
        "popunder_code": get_setting("adsterra_popunder_code", "") if enable_popunder else "",
        "socialbar_code": get_setting("adsterra_socialbar_code", "") if enable_ads else "",
        "social_links": {
            "discord": get_setting("social_discord", "https://discord.gg/ngotra"),
            "telegram": get_setting("social_telegram", "https://t.me/ngotra_ai"),
            "youtube": get_setting("social_youtube", "https://youtube.com/@ngotra_ai"),
            "instagram": get_setting("social_instagram", "https://instagram.com/ngotra_ai"),
            "x": get_setting("social_x", "https://x.com/ngotra_ai")
        },
        "models": list(MODELS.keys())
    }
