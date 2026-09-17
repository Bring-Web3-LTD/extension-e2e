BOT_MARKERS = ("/sorry/", "consent.google", "/captcha", "_____tmd_____",
               "/errors/validatecaptcha")


def blocked_url(url: str) -> str:
    lowered = (url or "").lower()
    for marker in BOT_MARKERS:
        if marker.lower() in lowered:
            return marker
    return ""


