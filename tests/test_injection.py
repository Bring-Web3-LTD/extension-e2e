"""Where the popup is injected, and what happens when the host page fights it.

QA_TEST_PLAN sections 1.11 (self-heal), 1.12 (top frame only), 1.6 (SPA).

These use controlled markup served on the retailer's own URL — see bring/pages.
A real shop does not hydrate-and-wipe on request, does not always carry a
captcha frame, and never has a route we can push at will.
"""
import pytest

from bring import pages, popup, retailers

pytestmark = pytest.mark.popup


async def test_popup_survives_a_host_that_wipes_it(context, retailer):
    """1.11 — a page that rebuilds its DOM after load must not kill the popup.

    React and Remix hosts replace the body when they hydrate, taking our nodes
    with them. The extension re-injects: at most three times, and only until
    about two seconds after load, so it recovers without flickering forever.
    """
    origin = retailers.origin(retailer)
    await pages.serve(context, f"{origin}/**", pages.hydration_wipe(delay_ms=1200))

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")

    first = await popup.wait_for_popup(page, timeout=30)
    assert first, "no popup appeared on the hydrating page at all"

    wiped = await pages.wait_for(page, "window.__wiped", timeout=10)
    assert wiped, "the test page never performed its wipe"

    healed = await popup.wait_for_popup(page, timeout=15)
    frames_now = len(popup.frames(page))
    await page.close()

    assert healed, "the host wiped the popup and it never came back"
    assert frames_now == 1, \
        f"self-heal left {frames_now} popups on the page instead of one"


async def test_our_own_close_is_not_treated_as_a_wipe(context, retailer):
    """1.11 — closing the popup ourselves must not trigger a re-injection.

    Same DOM removal, opposite meaning. Getting this wrong is worse than no
    self-heal: the user closes the popup and it immediately returns.
    """
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["close_x"], settle=3)
    came_back = await popup.wait_for_popup(page, timeout=6)
    await page.close()

    assert came_back is None, \
        "the popup re-injected itself after the user closed it"


async def test_one_popup_on_a_page_with_a_same_origin_frame(context, retailer):
    """1.12 — the content script runs in every frame; only the top one injects."""
    origin = retailers.origin(retailer)
    await pages.serve_site(context, origin,
                           {"/e2e-inner": pages.INNER},
                           pages.with_same_origin_frame())

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    count = len(popup.frames(page))
    await page.close()

    assert frame, "no popup appeared on a page carrying its own iframe"
    assert count == 1, \
        f"{count} popups on a page with a same-origin frame — one per frame"


async def test_popup_still_appears_beside_a_third_party_frame(context, retailer):
    """1.12 — a captcha or ad frame must not be able to answer first.

    The regression: a third-party frame replied to the injection request, the
    top frame was told 'domain mismatch', and no popup appeared at all on any
    retailer page carrying a captcha.
    """
    origin = retailers.origin(retailer)
    await pages.serve(context, f"{origin}/**", pages.with_third_party_frame())

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    count = len(popup.frames(page))
    await page.close()

    assert frame, \
        ("no popup on a retailer page carrying a third-party frame — a frame "
         "that is not the top one answered the injection request")
    assert count == 1, f"{count} popups beside a third-party frame"


async def test_spa_route_change_leaves_exactly_one_popup(context, retailer):
    """1.6 — an in-app route change closes the old popup before showing a new one."""
    origin = retailers.origin(retailer)
    await pages.serve(context, f"{origin}/**", pages.spa())

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_popup(page, timeout=30), "no popup appeared"

    await page.evaluate("() => window.__go(1)")
    await page.wait_for_timeout(4000)
    count = len(popup.frames(page))
    await page.close()

    assert count <= 1, \
        f"an in-app route change left {count} popups on the page"
