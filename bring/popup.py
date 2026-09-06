"""Finding the extension's iframe on a page, and driving what is inside it.

Everything the user can touch lives in one iframe the content script injects.
It carries a stable id — `bringweb3-iframe-<extensionId>` (SDK constants.ts) —
and the app inside it is a small router: `/` is the offer, `/activated` the
confirmation, `/offerbar` the bar, `/notification` the reward, `/framed` the
framed variant. Which route is loaded is the surface under test, so it is read
off the frame URL rather than guessed from what happens to be on screen.

Selectors are the ids the iframe app already ships. They are not test hooks
added for this suite, which cuts both ways: they are stable across redesigns of
the styling, and a renamed id is a real break worth failing on.
"""
import asyncio

IFRAME_ID_PREFIX = "bringweb3-iframe"
OFFERBAR_CONTAINER_ID = "bringweb3-offerbar-container"

# Where the iframe is served from: its own CloudFront host in a deployed
# environment, or a dev server when somebody is running one locally. Matched on
# the host alone — a local dev URL is `http://localhost:5173/...` and carries no
# other distinguishing mark, so requiring one would quietly find nothing every
# time the suite was pointed at a dev server.
FRAME_MARKERS = ("extension.bringweb3.io", "localhost", "127.0.0.1")

# ── what can be clicked, per surface ────────────────────────────────

OFFER = {
    "container": "#offer-container",
    "activate": "#activate-btn",
    "close_x": "#close-btn-icon",
    "close_cancel": "#cancel-btn",
    "optout": "#opt-out-btn",
    "terms_link": "#offer-terms-link",
    "tou_link": "#offer-tou-link",
    "agree_text": "#offer-agree-text",
    "details_text": "#offer-details-text",
    "connect_wallet": "#connect-wallet-btn",
    "wallet_address": "#wallet-address",
    "terms_box": "#offer-terms-box",
    "terms_back": "#offer-terms-back-btn",
}

ACTIVATED = {
    "container": "#activated-container",
    "title": "#activated-title",
    "text": "#activated-text",
    "close_x": "#close-btn-icon",
    "wallet_address": "#activated-wallet-address",
    "backed_by": "#activated-backed-by",
}

WIDGET = {
    "collapsed": "#bring-widget-collapsed",
    "badge": "#bring-widget-badge",
    "close": "#bring-widget-close",
    "expanded": "#bring-widget-expanded",
}

OFFERBAR = {
    "container": "#offerbar-container",
    "activate": "#offerbar-activate-btn",
    "close_top": "#offerbar-close-btn-top",
    "close_bottom": "#offerbar-close-btn-bottom",
    "optout": "#offerbar-opt-out-btn",
    "cashback": "#offerbar-cashback-amount",
    "spacer": "#offerbar-spacer",
}

NOTIFICATION = {
    "simple": "#notification-container-simple",
    "pairing": "#notification-container-pairing",
    "cta": "#notification-cta-btn",
    "details": "#notification-details-btn",
    "stop_reminders": "#notification-stop-reminders-btn",
    "earned": "#notification-earned",
    "claimable": "#notification-claimable",
    "total": "#notification-total",
    "expiration": "#notification-expiration",
    "connect_prompt": "#notification-connect-prompt",
    "close_x": "#close-btn-icon",
}

OPTOUT = {
    "container": "#opt-out-container",
    "card": "#opt-out-card",
    "apply": "#opt-out-apply-btn",
    "back": "#opt-out-back-btn",
    "close": "#opt-out-close-btn",
    "confirmation": "#opt-out-confirmation-card",
    "this_site": "#websiteOption0",
    "all_sites": "#websiteOption1",
    "for_24h": "#durationOption0",
    "for_30d": "#durationOption1",
    "forever": "#durationOption2",
}

# What the popup sends for each duration, so a test can say which window it
# expects without restating the numbers the iframe already owns.
OPTOUT_WINDOWS_MS = {
    "for_24h": 24 * 60 * 60 * 1000,
    "for_30d": 30 * 24 * 60 * 60 * 1000,
    "forever": 999_999_999_999_999,
}

# Which router path each surface is served on.
ROUTES = {
    "offer": ("/", ""),
    "activated": ("/activated",),
    "offerbar": ("/offerbar",),
    "notification": ("/notification",),
    "framed": ("/framed",),
}


def is_bring_frame(frame) -> bool:
    url = frame.url or ""
    return any(marker in url for marker in FRAME_MARKERS)


def route_of(frame) -> str:
    """Which surface this frame is — 'offer', 'activated', 'notification'…"""
    path = (frame.url or "").split("?")[0].split("#")[0]
    # The path carries the version and environment prefix, so match on the tail.
    tail = "/" + path.rstrip("/").rsplit("/", 1)[-1] if "/" in path else ""
    for name, suffixes in ROUTES.items():
        if name == "offer":
            continue
        if any(tail == suffix for suffix in suffixes):
            return name
    return "offer"


def frames(page, route: str = None) -> list:
    """Every Bring frame on the page, optionally only one surface."""
    found = [f for f in page.frames if is_bring_frame(f)]
    if route:
        found = [f for f in found if route_of(f) == route]
    return found


async def rendered(frame) -> bool:
    """Whether the app inside the frame has actually drawn something.

    A frame exists from the moment the iframe is appended, well before the
    bundle has loaded and verified its token. Treating existence as appearance
    makes a broken popup look present and a slow one look instant.
    """
    try:
        return await frame.evaluate(
            "() => !!document.body && document.body.innerText.trim().length > 0"
        )
    except Exception:
        return False


async def wait_for_popup(page, timeout: float = 30, route: str = None):
    """The Bring frame once it is on screen and has rendered, or None.

    None rather than an exception: "no popup appeared" is a finding the test
    should state in its own words, with the retailer named, not a timeout
    traceback from inside a helper.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in frames(page, route):
            if await rendered(frame):
                return frame
        await asyncio.sleep(0.25)
    return None


async def wait_for_gone(page, timeout: float = 10, route: str = None) -> bool:
    """Whether every Bring frame has left the page within *timeout*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if not frames(page, route):
            return True
        await asyncio.sleep(0.2)
    return not frames(page, route)


async def iframe_src(page) -> str:
    """The injected iframe's `src`, exactly as the content script built it.

    Section 1.10 is entirely about this string: which params the server set,
    which the client filled in, and that the token rides in the fragment rather
    than the query.
    """
    element = await page.query_selector(f'iframe[id^="{IFRAME_ID_PREFIX}-"]')
    return await element.get_attribute("src") if element else ""


async def click(frame, selector: str, *, settle: float = 1.5) -> bool:
    """Click something inside the frame; False when it is not there to click."""
    element = await frame.query_selector(selector)
    if not element or not await element.is_visible():
        return False
    await element.click()
    await asyncio.sleep(settle)
    return True


async def visible(frame, selector: str) -> bool:
    try:
        element = await frame.query_selector(selector)
        return bool(element) and await element.is_visible()
    except Exception:
        return False


async def text(frame, selector: str) -> str:
    try:
        element = await frame.query_selector(selector)
        return (await element.inner_text()).strip() if element else ""
    except Exception:
        return ""


async def body_text(frame) -> str:
    try:
        return (await frame.evaluate("() => document.body.innerText")) or ""
    except Exception:
        return ""
