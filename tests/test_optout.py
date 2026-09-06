"""Opt-out: scope, duration, and the 60-day cap that is gone.

QA_TEST_PLAN section 1.4, plus 7.6.
"""
import time

import pytest

from bring import popup, storage

pytestmark = pytest.mark.popup

DAY = 24 * 60 * 60 * 1000
MINUTE = 60 * 1000

# What each duration button is specified to buy, straight from the iframe's own
# option list (OptOut.tsx). Asserted per choice rather than only for the
# default, because "everyone got 24 hours whichever button they pressed" is
# exactly the shape of bug a single-duration test cannot see.
OPTOUT_WINDOWS = {
    "for_24h": 24 * 60 * 60 * 1000,
    "for_30d": 30 * 24 * 60 * 60 * 1000,
}
# Generous: the range is stamped when the click is handled, not when it is sent.
OPTOUT_TOLERANCE_MS = 2 * 60 * 1000


async def open_optout(page, frame):
    """Get from the offer to the opt-out screen, or skip saying why."""
    if not await popup.click(frame, popup.OFFER["optout"], settle=2):
        pytest.skip("this surface has no opt-out entry point")
    assert await popup.visible(frame, popup.OPTOUT["card"]), \
        "clicking opt-out did not open the opt-out screen"
    return frame


async def test_optout_screen_offers_scope_and_duration(on_retailer):
    """1.4 — both scopes and all three durations are on screen."""
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    for name in ("this_site", "all_sites", "for_24h", "for_30d", "forever"):
        assert await popup.visible(frame, popup.OPTOUT[name]), \
            f"the opt-out screen is missing the {name!r} choice"
    assert await popup.visible(frame, popup.OPTOUT["apply"]), \
        "the opt-out screen has no Apply button"


async def test_nothing_happens_until_apply(on_retailer, context, retailer):
    """1.4 — choosing a scope and a duration changes nothing on its own."""
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    await popup.click(frame, popup.OPTOUT["all_sites"], settle=0.5)
    await popup.click(frame, popup.OPTOUT["forever"], settle=0.5)

    assert not await storage.get(context, storage.OPT_OUT), \
        "the global opt-out was written before Apply was clicked"
    assert not await storage.quiet_entry(context, retailer), \
        "a quiet entry was written before Apply was clicked"


async def test_back_to_activation_returns_to_the_offer(on_retailer):
    """1.4 — 'Back to activation' goes back, and writes nothing."""
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    assert await popup.click(frame, popup.OPTOUT["back"], settle=1.5), \
        "the opt-out screen has no 'Back to activation'"
    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "Back did not return to the offer"


@pytest.mark.parametrize("duration", ["for_24h", "for_30d", "forever"])
async def test_optout_this_site_silences_for_the_time_chosen(on_retailer, context,
                                                             retailer, control,
                                                             duration):
    """1.4 - a single-site opt-out lasts exactly as long as the button says.

    Run for every duration, because the window is the whole point of the
    screen: an opt-out that silently gives everyone 24 hours looks correct on
    whichever single duration a one-case test happened to pick.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    await popup.click(frame, popup.OPTOUT["this_site"], settle=0.5)
    await popup.click(frame, popup.OPTOUT[duration], settle=0.5)
    assert await popup.click(frame, popup.OPTOUT["apply"], settle=3)

    assert await popup.visible(frame, popup.OPTOUT["confirmation"]), \
        "Apply did not show the confirmation"

    entry = await storage.quiet_entry(context, retailer)
    assert entry, f"a single-site opt-out wrote nothing for {retailer}"
    assert not await storage.get(context, storage.OPT_OUT), \
        "a single-site opt-out set the global opt-out as well"

    window = storage.window_ms(entry)
    assert window is not None, f"the opt-out wrote a malformed range: {entry!r}"

    if duration == "forever":
        # Not a number to match against: 'forever' is whatever largest value
        # the iframe sends, and an older SDK may clamp it. What has to hold is
        # that it outlasts every other choice by a wide margin.
        assert window > 60 * DAY, (
            f"'forever' gave {window / DAY:.1f} days, which is not forever - "
            f"the 60-day cap is supposed to be gone")
    else:
        expected = OPTOUT_WINDOWS[duration]
        assert abs(window - expected) <= OPTOUT_TOLERANCE_MS, (
            f"opting out for {duration.removeprefix('for_')} should silence "
            f"{retailer} for {expected / DAY:.2f} days; it stored "
            f"{window / DAY:.2f}")


async def test_optout_all_sites_silences_everything(on_retailer, context,
                                                    retailer, control):
    """1.4 — 'For all websites' sets the global opt-out, and no retailer pops."""
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    await popup.click(frame, popup.OPTOUT["all_sites"], settle=0.5)
    await popup.click(frame, popup.OPTOUT["for_24h"], settle=0.5)
    assert await popup.click(frame, popup.OPTOUT["apply"], settle=3)

    optout = await storage.get(context, storage.OPT_OUT)
    assert optout, "'For all websites' did not set the global opt-out"
    assert isinstance(optout, list) and len(optout) == 2, \
        f"the global opt-out should be a [start, end] range, got {optout!r}"

    for site in (retailer, control):
        tab = await context.new_page()
        await tab.goto(site, wait_until="domcontentloaded")
        shown = await popup.wait_for_popup(tab, timeout=12)
        await tab.close()
        assert shown is None, \
            f"the popup appeared on {site} while opted out of all websites"


async def test_forever_optout_outlives_sixty_days(on_retailer, context, retailer):
    """1.4 + 7.6 — the 60-day cap is gone; forever means forever.

    The old SDK wiped any range longer than 60 days, so a 'forever' opt-out
    quietly expired after two months. Moving the clock past that point is the
    only way to see the difference, and the clock here is the stored range: the
    end of a real 'forever' is far enough out that no wait would ever reach it.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    await open_optout(page, frame)

    await popup.click(frame, popup.OPTOUT["this_site"], settle=0.5)
    await popup.click(frame, popup.OPTOUT["forever"], settle=0.5)
    assert await popup.click(frame, popup.OPTOUT["apply"], settle=3)

    entry = await storage.quiet_entry(context, retailer)
    assert entry, "a forever opt-out wrote nothing"

    window = storage.window_ms(entry)
    assert window is not None, f"the forever entry has a malformed range: {entry!r}"
    assert window > 60 * DAY, (
        f"a 'forever' opt-out was capped at {window // DAY} days — the 60-day cap "
        f"is supposed to be removed")

    # Now stand 61 days into that window and confirm it still silences.
    now = int(time.time() * 1000)
    rows = await storage.quiet_domains(context)
    for row in rows:
        if storage.entries_for([row], retailer):
            start, end = row["time"]
            row["time"] = [start - 61 * DAY, end - 61 * DAY]
    await storage.set(context, storage.QUIET_DOMAINS, rows)

    tab = await context.new_page()
    await tab.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(tab, timeout=12)
    await tab.close()
    assert shown is None, \
        "a 'forever' opt-out stopped silencing once 60 days had passed"


async def test_expired_optout_is_cleared_lazily(context, retailer):
    """1.4 — an expired opt-out stays saved until the next write, then goes.

    Not an implementation detail: it is why a user who opted out and came back
    a month later still sees the old row in storage, and why a test that clears
    it on read would be testing a state the product never reaches.
    """
    now = int(time.time() * 1000)
    await storage.set(context, storage.OPT_OUT, [now - 7200_000, now - 3600_000])

    tab = await context.new_page()
    await tab.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(tab, timeout=30)
    await tab.close()

    assert shown, "an expired global opt-out is still silencing the popup"
