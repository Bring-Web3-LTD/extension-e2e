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
    # Its own opt-out (pages/Offerbar/Optout), not the popup's.
    "optout_panel": "#optout-container",
    "optout_title": "#optout-title",
    "optout_24h": "#optout-24-hours-btn",
    "optout_30d": "#optout-30-days-btn",
    "optout_forever": "#optout-forever-btn",
    "optout_back": "#optout-back-btn",
}

# The top bar. Same feature as the offer bar, different layout and a different
# route: the server sends `framed` alongside `isOfferBar`, and the SDK prefers
# it (`page: popupData.framed ? 'framed' : (isOfferBar ? 'offerbar' : ...)`).
# Its ids share nothing with the offer bar's, which is why looking for
# `#offerbar-*` on a top bar finds an empty page and reports a missing bar.
TOPBAR = {
    "container": "#tb-container",
    "activate": "#tb-activate-btn",
    "close_top": "#tb-close-btn",
    "close_bottom": "#tb-close-btn",
    "optout": "#tb-opt-out-btn",
    "offer_text": "#tb-offer-text",
    "retailer_name": "#tb-retailer-name",
    # Its own opt-out (pages/Framed/Optout), not the popup's.
    "optout_panel": "#tb-optout-banner",
    "optout_title": "#tb-optout-title",
    "optout_24h": "#tb-optout-24-hours-btn",
    "optout_30d": "#tb-optout-30-days-btn",
    "optout_forever": "#tb-optout-forever-btn",
    "optout_back": "#tb-optout-back-btn",
}

# How the top bar makes room: `resizePage` writes these three onto `body` with
# `!important` and restores whatever was there on cleanup. So "it gave the space
# back" is exactly "these inline properties are gone again" — which the offer
# bar answers differently, with a spacer element.
BODY_RESERVATION = ("() => { const s = document.body.style;"
                    "  return { width: s.width, height: s.height,"
                    "           transform: s.transform }; }")

NOTIFICATION = {
    "simple": "#notification-container-simple",
    "pairing": "#notification-container-pairing",
    # Two containers, two buttons, same job. `promptPairing` decides which is
    # rendered (Notification.tsx), so anything asking "what does the button
    # say" has to accept either — looking only for the pairing one reads an
    # empty string on every wallet-connected notification.
    "cta": "#notification-cta-btn, #notification-details-btn",
    "cta_pairing": "#notification-cta-btn",
    "details": "#notification-details-btn",
    "stop_reminders": "#notification-stop-reminders-btn",
    "earned": "#notification-earned",
    "claimable": "#notification-claimable",
    "total": "#notification-total",
    "expiration": "#notification-expiration",
    "connect_prompt": "#notification-connect-prompt",
    "close_x": "#close-btn-icon",
}

# There are three opt-out screens, one per surface, and they share no ids: the
# popup's two-step card (components/OptOut), and one apiece for the two bars
# (pages/Framed/Optout, pages/Offerbar/Optout). The bars' are a single line —
# pick a duration and it applies, with no Apply button — because a bar is 71px
# tall. This map is the popup's; a bar's lives in its own selector map above,
# and using this one against a bar finds nothing and reports a working opt-out
# as missing.
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

# Any one of these means the app has finished mounting something a test can
# act on. Kept as one selector list rather than a per-surface check because
# "has it rendered" is asked before anyone knows which surface arrived.
SURFACE_MARKERS = ", ".join((
    "#offer-container",              # the offer
    "#bring-widget-collapsed",       # the widget badge
    "#bring-widget-expanded",
    "#activated-container",          # the confirmation
    "#offerbar-container",           # the offer bar
    "#tb-container",                 # the top bar, its other layout
    "#opt-out-container",
    "#notification-container-simple",
    "#notification-container-pairing",
    "#onestep-activate-btn",         # the Argent one-step variant
    "#activate-btn",
))

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

    Drawn means "one of the app's own surfaces has mounted". Not text: the
    widget's badge is an icon with none, and `innerText` is layout-dependent so
    it reads empty in the 1x1 iframe the content script injects before the app
    asks to be resized. And not merely "body has children" either — the React
    root div exists from the first byte of HTML, so that returns true while the
    frame is still blank, and callers then look for buttons that have not been
    rendered yet.
    """
    try:
        return await frame.evaluate(
            "(sel) => !!document.querySelector(sel)", SURFACE_MARKERS)
    except Exception:
        return False


async def is_widget(frame) -> bool:
    """Whether this frame is showing the collapsed widget badge.

    Server-gated (`isWidgetEnabled`), so the same retailer is a full popup on
    one environment and a badge on another. Anything that wants the offer has
    to ask, rather than assume.
    """
    return await visible(frame, WIDGET["collapsed"])


async def expand_widget(frame, *, timeout: float = 15) -> bool:
    """Click the badge and wait for the offer behind it.

    Returns False when there was no badge to click — which is not a failure,
    only the other of the two shapes the server can send.

    The click is retried: the badge zooms out while the offer scales up in its
    place, and a click that lands during that hand-off can be swallowed. One
    retry costs a second and removes a flake that would otherwise show up as
    "the offer has no activate button" on a perfectly good popup.
    """
    if not await is_widget(frame):
        return False

    deadline = asyncio.get_event_loop().time() + timeout
    attempts = 0
    while asyncio.get_event_loop().time() < deadline:
        if await visible(frame, OFFER["activate"]):
            return True
        if not await is_widget(frame):
            # Mid-animation: the badge has gone but the offer has not mounted.
            await asyncio.sleep(0.3)
            continue
        if attempts < 3:
            attempts += 1
            await click(frame, WIDGET["badge"], settle=1.0, force=True)
        await asyncio.sleep(0.4)

    return await visible(frame, OFFER["activate"])


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


async def wait_for_offer(page, timeout: float = 30):
    """The offer itself, expanding the widget badge first when there is one.

    Most of the suite is about what the offer does — activate, close, opt-out,
    terms — and none of that is reachable while the widget is collapsed. So the
    badge is opened here rather than in thirty tests, and a run against an
    environment with the widget off behaves identically.
    """
    frame = await wait_for_popup(page, timeout=timeout)
    if frame is None:
        return None
    if await is_widget(frame):
        await expand_widget(frame)
    return frame


async def wait_for_confirmation(page, timeout: float = 60):
    """The activated confirmation, after the redirect that activation triggers.

    Activating does not leave the tab where it was. The SDK sends it through
    the affiliate network — measured: `redirect.viglink.com/?...&u=<retailer>`
    — which then bounces back to the shop, and the confirmation is injected on
    what lands. A test that starts looking the moment the button is clicked is
    watching a tab that is busy navigating, and thirty seconds is not always
    enough for the hop.

    So the navigation is allowed to settle first, and the wait afterwards is
    generous. The activation itself is already provable from `quietDomains`;
    this is only about catching the screen it produces.
    """
    for state in ("domcontentloaded", "load"):
        try:
            await page.wait_for_load_state(state, timeout=20_000)
        except Exception:
            break        # still navigating, or already past it; the poll copes

    return await wait_for_popup(page, timeout=timeout, route="activated")


#: The two layouts the server can answer a search with. Both are "the bar".
BAR_ROUTES = ("offerbar", "framed")


def bars(page) -> list:
    """Every bar frame, whichever layout the server chose."""
    return [f for f in page.frames
            if is_bring_frame(f) and route_of(f) in BAR_ROUTES]


async def controls_for(frame):
    """The selector set this bar actually uses, by looking at what it rendered.

    Asking the frame rather than trusting the route: the two layouts are the
    same feature and a test should not care which arrived, but they share no
    ids, so something has to decide.
    """
    if await visible(frame, TOPBAR["container"]):
        return TOPBAR
    return OFFERBAR


async def wait_for_bar(page, timeout: float = 25):
    """The bar in either layout, once it has rendered."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in bars(page):
            if await rendered(frame):
                return frame
        await asyncio.sleep(0.25)
    return None


async def bars_gone(page, timeout: float = 10) -> bool:
    """Whether the bar has left, in whichever layout it arrived."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if not bars(page):
            return True
        await asyncio.sleep(0.2)
    return not bars(page)


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


async def click(frame, selector: str, *, settle: float = 1.5,
                force: bool = False) -> bool:
    """Click something inside the frame; False when it is not there to click.

    :param force: skip Playwright's stability check. Needed for anything that
        animates continuously — the widget badge pulses by design, so the
        default check waits for it to stop moving and it never does. Visibility
        is still asserted above, so this does not click a hidden element; it
        only stops waiting for a permanent animation to end.
    """
    element = await frame.query_selector(selector)
    if not element or not await element.is_visible():
        return False
    try:
        await element.click(force=force, timeout=8000)
    except Exception:
        if force:
            raise
        # A pulsing or sliding element that never settles: say so by retrying
        # once with the check off rather than reporting "no such button".
        await element.click(force=True, timeout=8000)
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
