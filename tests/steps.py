from bring import popup, search, storage
from bring.walk import Result

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# What each control is specified to buy. Asserted against these rather than
# against "something plausible", which is what a client storing an invented
# value would also satisfy.
CLOSE_QUIET_MS = 30 * MINUTE
ACTIVATE_QUIET_MS = 2 * HOUR
TOLERANCE_MS = 2 * MINUTE

UNRESOLVED = ("undefined", "NaN", "{{")


def unresolved(text: str) -> str:
    """The first placeholder left in rendered text, or ''."""
    for marker in UNRESOLVED:
        if marker in text:
            return marker
    return ""


async def visit_and_open(site, tab):
    await tab.goto(site, wait_until="domcontentloaded")
    frame = await popup.wait_for_offer(tab, timeout=90)
    if not frame:
        marker = search.blocked_url(tab.url)
        if marker:
            return Result.blocked(f"bot check ({marker!r}) instead of the shop")
        try:
            title = (await tab.title() or "")[:40]
        except Exception:
            title = ""
        return Result.failed(f"no popup appeared; landed on {tab.url[:60]} {title!r}")
    if not await popup.visible(frame, popup.OFFER["activate"]):
        return Result.failed("popup has no activate button")
    return Result.passed("offer shown")


def offer_frame(tab):
    frames = popup.frames(tab)
    return frames[0] if frames else None


async def activate(site, tab):
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    if not await popup.click(frame, popup.OFFER["activate"], settle=5):
        return Result.failed("could not click activate")
    confirmed = await popup.wait_for_confirmation(tab)
    if not confirmed:
        marker = search.blocked_url(tab.url)
        if marker:
            return Result.blocked(
                f"the shop answered the affiliate hop with a bot check "
                f"({marker!r}), so no confirmation could be shown")
        return Result.failed("no confirmation screen")
    body = await popup.body_text(confirmed)
    bad = unresolved(body)
    if bad:
        return Result.failed(f"{bad!r} on the confirmation: {body[:60]!r}")
    return Result.passed("confirmation shown")


async def close_popup(site, tab, control="close_x"):
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("no popup to close")
    if not await popup.click(frame, popup.OFFER[control], settle=3):
        return Result.failed(f"could not click {control}")
    if not await popup.wait_for_gone(tab, timeout=20):
        return Result.failed("the popup is still on the page")
    return Result.passed("closed")


async def open_optout(site, tab):
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    if not await popup.click(frame, popup.OFFER["optout"], settle=2):
        return Result.failed("no opt-out control")
    if not await popup.await_visible(frame, popup.OPTOUT["card"], timeout=40):
        return Result.failed("the opt-out screen did not open")
    return Result.passed("opt-out screen open")


def window_check(entry, expected, label):
    """A failed Result when the row's window is not *expected*, else None."""
    window = storage.window_ms(entry)
    if window is None:
        return Result.failed(f"malformed range: {entry.get('time')!r}")
    if abs(window - expected) > TOLERANCE_MS:
        return Result.failed(
            f"{label}: expected {expected / HOUR:.2f}h, stored {window / HOUR:.2f}h")
    return None


async def revisit(context, site, timeout=60):
    """Open the shop in a new tab; the surface that appeared, or None."""
    tab = await context.new_page()
    try:
        await tab.goto(site, wait_until="domcontentloaded")
        shown = await popup.wait_for_popup(tab, timeout=timeout)
        return popup.route_of(shown) if shown else None
    finally:
        await tab.close()


async def revisit_expecting(context, site, want_route, what):
    route = await revisit(context, site,
                          timeout=popup.ABSENT if want_route is None else 60)
    if want_route is None:
        if route:
            return Result.failed(f"expected silence, got the {route} surface")
        return Result.passed("silent")
    if route != want_route:
        return Result.failed(f"expected {what}, got {route!r}")
    return Result.passed(what)
