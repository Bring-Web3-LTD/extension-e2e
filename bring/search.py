import asyncio

# What "Accept all" stores. Works regardless of the interface language, which
# is served in the local one and cannot be matched on.
CONSENT_COOKIES = [
    {"name": "SOCS", "value": "CAESHAgBEhIaAB", "domain": ".google.com", "path": "/"},
]
CONSENT_BUTTON = "#L2AGLb"

ENGINES = {
    "google": {
        "url": "https://www.google.com/search?q={term}",
        "home": "https://www.google.com",
        "box": "textarea[name=q], input[name=q]",
        "results": "#search, #rso, div[data-async-context]",
    },
    # Amazon is in the backend's SEARCH_ENGINE_ENTITIES, but the offer bar is
    # not live over it — a search there returns results and no bar, which is
    # indistinguishable from a broken bar. Left here because the backend still
    # lists it, and deliberately not the default.
    "amazon": {
        "url": "https://www.amazon.com/s?k={term}",
        "home": "https://www.amazon.com",
        "box": "#twotabsearchtextbox, input[name=field-keywords]",
        "results": "div.s-main-slot, [data-component-type='s-search-result']",
    },
}

BOT_MARKERS = ("/sorry/", "consent.google", "/captcha", "_____tmd_____",
               "/errors/validatecaptcha")
BOT_TITLES = ("unusual traffic", "before you continue", "are you a robot",
              "bevor sie zu google weitergehen", "robot check",
              "enter the characters you see")


def blocked_url(url: str) -> str:
    lowered = (url or "").lower()
    for marker in BOT_MARKERS:
        if marker.lower() in lowered:
            return marker
    return ""


def term_for(retailer_url: str) -> str:

    host = retailer_url.split("//")[-1].split("/")[0].lower()
    host = host.removeprefix("www.")
    return host.split(".")[0]


async def accept_consent(context) -> None:
    """Answer the consent gate before it is shown."""
    try:
        await context.add_cookies(CONSENT_COOKIES)
    except Exception:
        pass        # not fatal; the button below is the fallback


async def dismiss_consent(page, timeout_ms: int = 3000) -> bool:
    """Click through the dialog if it appeared anyway."""
    try:
        button = await page.wait_for_selector(CONSENT_BUTTON, timeout=timeout_ms)
    except Exception:
        return False            # not shown, which is the good case
    try:
        await button.click()
        await page.wait_for_load_state("domcontentloaded")
        return True
    except Exception:
        return False


async def blocked(page) -> str:
    url = (page.url or "").lower()
    for marker in BOT_MARKERS:
        if marker in url:
            return f"the search engine served a bot check instead of results ({marker})"
    try:
        title = (await page.title() or "").lower()
    except Exception:
        return ""
    for marker in BOT_TITLES:
        if marker in title:
            return f"the search engine served an interstitial instead of results ({title[:60]!r})"
    return ""


async def search(page, term: str, engine: str = "google") -> str:

    config = ENGINES.get(engine)
    if not config:
        return f"unknown search engine {engine!r}"

    await accept_consent(page.context)

    # Typed from the home page rather than navigated to `?q=`. Measured: a
    # direct navigation to the results URL is answered with `/sorry/` — a bot
    # check — on the first try, every try. Arriving at the home page and typing
    # is what a person does, and it is what gets results back.
    await page.goto(config["home"], wait_until="domcontentloaded")
    await dismiss_consent(page)

    reason = await blocked(page)
    if reason:
        return reason

    try:
        box = await page.wait_for_selector(config["box"], timeout=15_000)
        await box.click()
        await box.type(term, delay=90)
        await box.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
    except Exception as e:
        return f"{engine} did not offer a usable search box ({e})"

    await asyncio.sleep(1)
    reason = await blocked(page)
    if reason:
        return reason

    try:
        await page.wait_for_selector(config["results"], timeout=15_000)
    except Exception:
        # No results block. Either the engine changed its markup or it answered
        # with something else entirely; both mean this search proved nothing.
        return (f"{engine} did not return a recognisable results page for "
                f"{term!r} (landed on {page.url[:100]})")

    # The bar is injected after the results settle.
    await asyncio.sleep(1)
    return ""
