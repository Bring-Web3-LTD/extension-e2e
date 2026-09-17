from bring import netspy, popup, retailers, storage
from bring.walk import Result
from tests.steps import (CLOSE_QUIET_MS, MINUTE, TOLERANCE_MS, activate,
                         revisit_expecting)

SITE = retailers.WIDGET_SITE


async def frame_box(tab):
    element = await tab.query_selector(f'iframe[id^="{popup.IFRAME_ID_PREFIX}-"]')
    return await element.bounding_box() if element else None


async def badge_on(walk, tab):
    """The collapsed badge on a fresh visit, or a failed Result saying why not."""
    await tab.goto(SITE, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(tab, timeout=90)
    if frame is None:
        row = await storage.quiet_entry(walk.context, SITE)
        if row:
            return None, Result.failed(f"nothing appeared; quietDomains explains "
                                       f"why ({row.get('phase')!r}) — leftover state")
        return None, Result.failed("nothing appeared, and nothing was silencing it")
    if not await popup.is_widget(frame):
        return None, Result.failed("the full popup came instead of the badge — "
                                   "the widget is off for this shop on this "
                                   "environment (isWidgetEnabled on /verify)")
    return frame, None


async def expanded(walk, tab):
    frame, bad = await badge_on(walk, tab)
    if bad:
        return None, bad
    if not await popup.expand_widget(frame):
        return None, Result.failed("clicking the badge did not open the offer")
    return frame, None


def make_collapsed(walk):
    async def check(site, tab):
        frame, bad = await badge_on(walk, tab)
        if bad:
            return bad
        if not await popup.visible(frame, popup.WIDGET["badge"]):
            return Result.failed("collapsed, but no badge to click")
        if await popup.visible(frame, popup.OFFER["activate"]):
            return Result.failed("the offer is already open behind the badge")
        box = await frame_box(tab)
        if not box or box["width"] >= 300 or box["height"] >= 300:
            return Result.failed(f"the collapsed frame is "
                                 f"{box and int(box['width'])}x{box and int(box['height'])}px, "
                                 f"the offer's size, not a badge's")
        return Result.passed(f"badge, {int(box['width'])}px wide")
    return check


def make_expands(walk):
    async def check(site, tab):
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        if not await popup.visible(frame, popup.OFFER["activate"]):
            return Result.failed("expanded, but no activate button")
        # The badge zooms out while the card scales up in its place; read
        # after the hand-over, not during it.
        await tab.wait_for_timeout(1500)
        if await popup.visible(frame, popup.WIDGET["collapsed"]):
            return Result.failed("the badge is still on screen behind the offer")
        box = await frame_box(tab)
        if not box or box["width"] <= 200:
            return Result.failed(f"the offer opened but the frame never grew ({box})")
        for name in ("terms_link", "optout"):
            if not await popup.visible(frame, popup.OFFER[name]):
                return Result.failed(f"{name} is not reachable from the widget")
        return Result.passed(f"offer open, {int(box['width'])}px wide, "
                             f"terms and opt-out reachable")
    return check


def make_activates(walk):
    async def check(site, tab):
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        result = await activate(site, tab)
        if not result.ok or result.detail.startswith("--"):
            return result
        row = await storage.await_quiet_entry(walk.context, SITE)
        if not row or row.get("phase") != "activated":
            return Result.failed(f"activated from the widget, but the row says "
                                 f"{row and row.get('phase')!r}")
        return Result.passed("confirmation shown, row activated")
    return check


def make_expanded_close_silences(walk, control):
    async def check(site, tab):
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        if not await popup.click(frame, popup.OFFER[control], settle=3):
            return Result.failed(f"the expanded offer has no {control}")
        entry = await storage.await_quiet_entry(walk.context, SITE)
        window = storage.window_ms(entry) if entry else None
        if not entry or entry.get("phase") != "quiet":
            return Result.failed("closing the expanded offer wrote no quiet row")
        if window is None or abs(window - CLOSE_QUIET_MS) > TOLERANCE_MS:
            return Result.failed(f"quiet for {window and window / MINUTE:.0f} "
                                 f"minutes, not the thirty a close writes")
        return Result.passed(f"quiet for {window / MINUTE:.0f} minutes")
    return check


def make_badge_dismiss_silences(walk):
    async def check(site, tab):
        frame, bad = await badge_on(walk, tab)
        if bad:
            return bad
        if not await popup.click(frame, popup.WIDGET["close"], settle=3):
            return Result.failed("the badge has no X of its own")
        if not await popup.wait_for_gone(tab, timeout=20):
            return Result.failed("dismissing the badge left the widget on the page")
        back = await revisit_expecting(walk.context, SITE, None, "silent")
        if not back.ok:
            return Result.failed("the badge returned straight after being dismissed")
        entry = await storage.await_quiet_entry(walk.context, SITE)
        window = storage.window_ms(entry) if entry else None
        if window is None or abs(window - CLOSE_QUIET_MS) > TOLERANCE_MS:
            return Result.failed(f"dismissing wrote {window and window / MINUTE:.0f} "
                                 f"minutes, not the thirty a close writes")
        return Result.passed(f"gone, stays away, quiet for {window / MINUTE:.0f} minutes")
    return check


def make_expanded_state_survives(walk):
    async def check(site, tab):
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        await tab.goto(SITE.rstrip("/") + "/?e2e-second-page=1",
                       wait_until="domcontentloaded")
        again = await popup.wait_for_popup(tab, timeout=60)
        if again is None:
            row = await storage.quiet_entry(walk.context, SITE)
            return Result.failed("nothing on the second page"
                                 + (f"; quietDomains says {row.get('phase')!r}" if row else ""))
        if await popup.is_widget(again):
            return Result.failed("back to the badge after an in-site navigation; "
                                 "the expanded state is supposed to be remembered")
        return Result.passed("re-opened expanded")
    return check


def events_of(analytics, kind):
    return [e for e in analytics.events()
            if isinstance(e, dict) and e.get("type") == kind]


def make_badge_dismiss_is_widget(walk, analytics):
    async def check(site, tab):
        frame, bad = await badge_on(walk, tab)
        if bad:
            return bad
        analytics.seen.clear()
        if not await popup.click(frame, popup.WIDGET["close"], settle=3):
            return Result.failed("the badge has no X")
        await tab.wait_for_timeout(2500)
        closes = events_of(analytics, "popup_close")
        if not closes:
            seen = [e.get("type") for e in analytics.events() if isinstance(e, dict)]
            return Result.failed(f"no popup_close event: {seen[:6]}")
        if not any(e.get("isWidget") for e in closes):
            return Result.failed("popup_close went out without isWidget, so it "
                                 "cannot be told from the popup's own close")
        return Result.passed("popup_close with isWidget")
    return check


def make_badge_reports_itself(walk, analytics):
    async def check(site, tab):
        analytics.seen.clear()
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        await netspy.await_call(walk.context, "analytics", timeout=8)
        seen = [e.get("type") for e in analytics.events() if isinstance(e, dict)]
        views = events_of(analytics, "page_view")
        if not views:
            return Result.failed(f"the badge load sent no page_view: {seen[:8]}")
        if not any(e.get("isWidget") for e in views):
            return Result.failed("page_view for a badge load is not flagged isWidget")
        clicks = events_of(analytics, "widget_click")
        if not clicks:
            return Result.failed(f"opening the badge sent no widget_click: {seen[:8]}")
        if not any(e.get("isWidget") for e in clicks):
            return Result.failed("widget_click went out without isWidget")
        return Result.passed("page_view and widget_click, both isWidget")
    return check


def make_expanded_close_not_widget(walk, analytics):
    async def check(site, tab):
        frame, bad = await expanded(walk, tab)
        if bad:
            return bad
        analytics.seen.clear()
        closed = (await popup.click(frame, popup.OFFER["close_x"], settle=3)
                  or await popup.click(frame, popup.OFFER["close_cancel"], settle=3))
        if not closed:
            return Result.failed("the expanded widget has no close control")
        await storage.await_quiet_entry(walk.context, SITE)
        closes = events_of(analytics, "popup_close")
        if not closes:
            return Result.failed("closing the expanded widget sent no popup_close")
        if any(e.get("isWidget") for e in closes):
            return Result.failed("the expanded close was reported with isWidget — "
                                 "the badge's dismiss and the offer's close are "
                                 "no longer distinguishable")
        return Result.passed("popup_close without isWidget")
    return check


async def act(walk):
    walk.begin("ACT 6", f"the widget, on {retailers.label(SITE)}")
    analytics = netspy.PageCalls()
    await analytics.watch(walk.context, "**/analytics")
    await netspy.install(walk.context, netspy.PASS)

    steps = [
        ("the badge appears collapsed", make_collapsed(walk)),
        ("clicking the badge opens the offer", make_expands(walk)),
        ("activate works from the expanded widget", make_activates(walk)),
        ("the expanded offer's X silences the shop for thirty minutes",
         make_expanded_close_silences(walk, "close_x")),
        ("the expanded offer's Close button does the same",
         make_expanded_close_silences(walk, "close_cancel")),
        ("the badge's own X silences the shop for thirty minutes",
         make_badge_dismiss_silences(walk)),
        ("the expanded state survives an in-site navigation",
         make_expanded_state_survives(walk)),
        ("dismissing the badge is reported as the widget",
         make_badge_dismiss_is_widget(walk, analytics)),
        ("the badge reports itself on load and on click",
         make_badge_reports_itself(walk, analytics)),
        ("the expanded offer's close is not reported as the widget",
         make_expanded_close_not_widget(walk, analytics)),
    ]
    try:
        for title, work in steps:
            await walk.one(title, work, site=SITE)
            await walk.clear(f"clearing after {retailers.label(SITE)}")
    finally:
        try:
            await walk.context.unroute("**/analytics")
        except Exception:
            pass
