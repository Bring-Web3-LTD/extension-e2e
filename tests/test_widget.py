"""The widget: a badge first, the offer when you click it.

QA_TEST_PLAN section 7.1. A server-gated mode (`isWidgetEnabled`) where the
retailer gets a small pulsing badge in the corner instead of the offer opening
straight away. Click it and the badge zooms out while the offer scales up in
its place.

Worth its own file because the badge is a different surface with different
rules, and one of them is easy to get backwards: the badge's own X is a
*dismiss* — it closes the widget and reports `isWidget` on the analytics event
— while the expanded offer's X behaves like any other close, silencing the
retailer. Collapsing back to the badge is deliberately not a thing.

Skips rather than fails when the environment has the widget off: a full popup
is the other correct answer, and it is covered everywhere else.
"""
import pytest

from bring import netspy, popup, storage

pytestmark = pytest.mark.widget

MINUTE = 60_000
CLOSE_QUIET_MS = 30 * MINUTE
QUIET_TOLERANCE_MS = 2 * MINUTE


async def badge_on(page, retailer, timeout: float = 30):
    """The frame while it is still showing the badge, or skip saying why."""
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=timeout)
    if frame is None:
        pytest.skip(f"nothing appeared on {retailer} at all")
    if not await popup.is_widget(frame):
        pytest.skip("the widget is off for this platform on this environment "
                    "(the server sent the full popup instead)")
    return frame


async def test_the_badge_appears_collapsed(page, widget_retailer):
    """7.1 — the offer starts as a badge, not as the full card."""
    frame = await badge_on(page, widget_retailer)

    assert await popup.visible(frame, popup.WIDGET["badge"]), \
        "the widget is collapsed but has no badge to click"
    assert not await popup.visible(frame, popup.OFFER["activate"]), \
        "the offer is already open behind the badge; nothing was collapsed"

    element = await page.query_selector(f'iframe[id^="{popup.IFRAME_ID_PREFIX}-"]')
    box = await element.bounding_box() if element else None
    assert box and box["width"] < 300 and box["height"] < 300, (
        f"the collapsed widget is {box and box['width']}x{box and box['height']}px "
        f"— that is the expanded offer's size, not a badge")


async def test_clicking_the_badge_opens_the_offer(page, widget_retailer):
    """7.1 — the whole point: badge in, offer out."""
    frame = await badge_on(page, widget_retailer)

    assert await popup.expand_widget(frame), \
        "clicking the badge did not bring up the offer"

    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "the widget expanded but the offer has no activate button"
    assert not await popup.visible(frame, popup.WIDGET["collapsed"]), \
        "the badge is still on screen behind the expanded offer"

    element = await page.query_selector(f'iframe[id^="{popup.IFRAME_ID_PREFIX}-"]')
    box = await element.bounding_box() if element else None
    assert box and box["width"] > 200, \
        f"the offer opened but the iframe was never resized ({box})"


async def test_the_expanded_offer_can_be_activated(page, context, widget_retailer):
    """7.1 — activate works from the widget exactly as from the popup."""
    frame = await badge_on(page, widget_retailer)
    assert await popup.expand_widget(frame), "the widget would not open"

    assert await popup.click(frame, popup.OFFER["activate"], settle=6), \
        "the expanded widget has no activate button"

    entry = await storage.quiet_entry(context, widget_retailer)
    assert entry, "activating from the widget wrote no quietDomains entry"
    assert entry.get("phase") == "activated", \
        f"expected phase 'activated', got {entry.get('phase')!r}"


async def test_the_expanded_offer_reaches_the_terms(page, widget_retailer):
    """7.1 — terms are reachable from the widget, not only from the popup."""
    frame = await badge_on(page, widget_retailer)
    assert await popup.expand_widget(frame), "the widget would not open"

    assert await popup.click(frame, popup.OFFER["terms_link"]), \
        "the expanded widget has no 'Deal Terms' link"
    assert await popup.visible(frame, popup.OFFER["terms_box"]), \
        "'Deal Terms' did not open the terms view from the widget"


async def test_the_expanded_offer_reaches_the_optout(page, widget_retailer):
    """7.1 — and so is opt-out."""
    frame = await badge_on(page, widget_retailer)
    assert await popup.expand_widget(frame), "the widget would not open"

    assert await popup.click(frame, popup.OFFER["optout"], settle=2), \
        "the expanded widget has no opt-out control"
    assert await popup.visible(frame, popup.OPTOUT["card"]), \
        "opt-out did not open from the widget"


async def test_the_badge_dismiss_does_not_silence_the_retailer(page, context,
                                                               widget_retailer):
    """7.1 — the badge's own X is a dismiss, not the popup's close.

    They look alike and mean different things: dismissing the badge should put
    the widget away for now, while the expanded offer's X is a real close that
    silences the retailer for half an hour. Writing quietDomains here would
    silence a retailer the user never actually saw an offer for.
    """
    frame = await badge_on(page, widget_retailer)

    assert await popup.click(frame, popup.WIDGET["close"], settle=3), \
        "the badge has no X to dismiss it with"
    assert await popup.wait_for_gone(page, timeout=8), \
        "dismissing the badge left the widget on the page"

    entry = await storage.quiet_entry(context, widget_retailer)
    assert entry is None, (
        f"dismissing the badge silenced {widget_retailer} "
        f"({entry.get('domain')!r}, phase {entry.get('phase')!r}); that is the "
        f"expanded offer's close, not the badge's dismiss")


async def test_the_expanded_offer_close_does_silence(page, context, widget_retailer):
    """7.1 — and the expanded offer's X behaves like a standard close.

    Half an hour of quiet, and no collapsing back to the badge — the
    reversible-collapse path is deliberately not implemented.
    """
    frame = await badge_on(page, widget_retailer)
    assert await popup.expand_widget(frame), "the widget would not open"

    closed = (await popup.click(frame, popup.OFFER["close_x"], settle=3)
              or await popup.click(frame, popup.OFFER["close_cancel"], settle=3))
    assert closed, "the expanded widget has no close control"

    assert await popup.wait_for_gone(page, timeout=8), \
        "closing the expanded offer collapsed it back to the badge instead of " \
        "closing it"

    entry = await storage.quiet_entry(context, widget_retailer)
    assert entry, "closing the expanded widget wrote no quietDomains entry"
    assert entry.get("phase") == "quiet", \
        f"expected phase 'quiet' after a close, got {entry.get('phase')!r}"

    window = storage.window_ms(entry)
    assert window is not None and abs(window - CLOSE_QUIET_MS) <= QUIET_TOLERANCE_MS, (
        f"closing the expanded widget should silence {widget_retailer} for 30 minutes; "
        f"it stored {window and window / MINUTE:.1f}")


async def test_the_expanded_state_survives_navigation(page, context, widget_retailer):
    """7.1 — once expanded, it stays expanded while the user browses the site.

    Remembered per platform in the iframe's sessionStorage, so moving to
    another page of the same shop re-opens the offer rather than making the
    user click the badge again.
    """
    frame = await badge_on(page, widget_retailer)
    assert await popup.expand_widget(frame), "the widget would not open"

    await page.goto(widget_retailer.rstrip("/") + "/?e2e-second-page=1",
                    wait_until="domcontentloaded")
    again = await popup.wait_for_popup(page, timeout=25)
    if again is None:
        pytest.skip("no widget on the second page, so there is no state to check")

    collapsed = await popup.is_widget(again)
    assert not collapsed, \
        "the widget went back to the badge after an in-site navigation; the " \
        "expanded state is supposed to be remembered"


async def test_dismissing_the_badge_reports_it_as_the_widget(page, context, widget_retailer):
    """7.1 — the dismiss is reported as the widget's own, not the popup's close.

    `popup_close` with `isWidget` set. Getting this wrong does not break
    anything a user sees, which is why it needs a test: it silently merges two
    different user actions in the numbers.
    """
    analytics = netspy.PageCalls()
    await analytics.watch(context, "**/analytics")

    frame = await badge_on(page, widget_retailer)
    assert await popup.click(frame, popup.WIDGET["close"], settle=3)
    await page.wait_for_timeout(2500)

    events = analytics.events()
    if not events:
        pytest.skip("no analytics events were sent while the badge was dismissed")

    closes = [e for e in events if isinstance(e, dict)
              and e.get("type") == "popup_close"]
    assert closes, \
        f"dismissing the badge sent no popup_close event: " \
        f"{[e.get('type') for e in events if isinstance(e, dict)][:6]}"
    assert any(e.get("isWidget") for e in closes), \
        "the badge's dismiss was reported without isWidget, so it is " \
        "indistinguishable from the popup's own close"
