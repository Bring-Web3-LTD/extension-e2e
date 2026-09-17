import time

from bring import popup, storage
from bring.walk import Result
from tests.steps import (DAY, TOLERANCE_MS, close_popup, offer_frame,
                         open_optout, revisit_expecting, visit_and_open)

# Each shop picks a different duration, so one pass covers all three and a
# bug that hands everyone 24 hours cannot hide behind a single choice.
CHOICES = ("for_24h", "for_30d", "forever")
WINDOWS = {"for_24h": DAY, "for_30d": 30 * DAY}


def choice_for(site, sites):
    return CHOICES[sites.index(site) % len(CHOICES)]


def window_ok(window, choice):
    """A failure message when *window* is not what *choice* buys, else ''."""
    if window is None:
        return "malformed range"
    if choice == "forever":
        # Not a number to match: 'forever' is whatever largest value the
        # iframe sends. What has to hold is that the 60-day cap is gone.
        if window <= 60 * DAY:
            return f"forever was capped at {window / DAY:.0f} days"
        return ""
    expected = WINDOWS[choice]
    if abs(window - expected) > TOLERANCE_MS:
        return (f"{choice}: expected {expected / DAY:.0f}d, "
                f"stored {window / DAY:.2f}d")
    return ""


async def check_choices(site, tab):
    """1.4 — both scopes, all three durations, Apply and Back on screen."""
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    for name in ("this_site", "all_sites", "for_24h", "for_30d", "forever",
                 "apply", "back"):
        if not await popup.visible(frame, popup.OPTOUT[name]):
            return Result.failed(f"the opt-out screen is missing {name!r}")
    return Result.passed("this site / all sites, 24h / 30d / forever, Apply, Back")


def make_nothing_until_apply(walk):
    async def check(site, tab):
        frame = offer_frame(tab)
        if not frame:
            return Result.failed("the popup is gone")
        await popup.click(frame, popup.OPTOUT["all_sites"], settle=0.5)
        await popup.click(frame, popup.OPTOUT["forever"], settle=0.5)
        if await storage.get(walk.context, storage.OPT_OUT):
            return Result.failed("the global opt-out was written before Apply")
        # An absence, so this one spends its whole budget.
        if await storage.await_quiet_entry(walk.context, site, timeout=1.5):
            return Result.failed("a quiet row was written before Apply")
        return Result.passed("nothing written")
    return check


async def back_to_offer(site, tab):
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    if not await popup.click(frame, popup.OPTOUT["back"], settle=1.5):
        return Result.failed("no 'Back to activation'")
    if not await popup.await_visible(frame, popup.OFFER["activate"]):
        return Result.failed("Back did not return to the offer")
    return Result.passed("back on the offer")


def make_apply_this_site(walk):
    async def check(site, tab):
        choice = choice_for(site, walk.sites)
        frame = offer_frame(tab)
        if not frame:
            return Result.failed("the popup is gone")
        await popup.click(frame, popup.OPTOUT["this_site"], settle=0.5)
        await popup.click(frame, popup.OPTOUT[choice], settle=0.5)
        if not await popup.click(frame, popup.OPTOUT["apply"], settle=3):
            return Result.failed("could not click Apply")
        if not await popup.await_visible(frame, popup.OPTOUT["confirmation"]):
            return Result.failed("no confirmation after Apply")
        entry = await storage.await_quiet_entry(walk.context, site)
        if not entry:
            return Result.failed(f"{choice}: nothing written")
        if await storage.get(walk.context, storage.OPT_OUT):
            return Result.failed("a single-site opt-out set the global opt-out too")
        bad = window_ok(storage.window_ms(entry), choice)
        if bad:
            return Result.failed(bad)
        return Result.passed(f"{choice} honoured exactly")
    return check


def make_forever_outlives_sixty_days(walk):
    """1.4 — stand 61 days into a forever opt-out: still silent."""
    async def check(site, tab):
        rows = await storage.quiet_domains(walk.context)
        moved = 0
        for row in rows:
            if storage.entries_for([row], site) and isinstance(row.get("time"), list):
                start, end = row["time"]
                row["time"] = [start - 61 * DAY, end - 61 * DAY]
                moved += 1
        if not moved:
            return Result.failed("no forever row to age")
        await storage.set(walk.context, storage.QUIET_DOMAINS, rows)
        return await revisit_expecting(walk.context, site, None,
                                       "silent, 61 days in")
    return check


def make_apply_all_sites(walk, choice):
    async def check(site, tab):
        result = await open_optout(site, tab)
        if not result.ok:
            return result
        frame = offer_frame(tab)
        await popup.click(frame, popup.OPTOUT["all_sites"], settle=0.5)
        await popup.click(frame, popup.OPTOUT[choice], settle=0.5)
        if not await popup.click(frame, popup.OPTOUT["apply"], settle=3):
            return Result.failed("could not click Apply")
        if not await popup.await_visible(frame, popup.OPTOUT["confirmation"]):
            return Result.failed("no confirmation after Apply")
        optout = await storage.await_key(walk.context, storage.OPT_OUT)
        if not (isinstance(optout, list) and len(optout) == 2):
            return Result.failed(f"optOut is {optout!r}, not a [start, end] range")
        bad = window_ok(storage.window_ms({"time": optout}), choice)
        if bad:
            return Result.failed(bad)
        return Result.passed(f"global opt-out {choice}")
    return check


def make_expired_optout_clears_lazily(walk):
    """1.4 / 4 — an expired opt-out is ignored, and pruned on the next write."""
    async def check(site, tab):
        now = int(time.time() * 1000)
        await storage.set(walk.context, storage.OPT_OUT, [now - 7200_000, now - 3600_000])
        result = await visit_and_open(site, tab)
        if not result.ok:
            return Result.failed(f"an expired global opt-out still silences: "
                                 f"{result.detail}")
        # The next write into the list — a close — is when it is pruned.
        closed = await close_popup(site, tab)
        if not closed.ok:
            return closed
        await storage.await_quiet_entry(walk.context, site)
        optout = await storage.get(walk.context, storage.OPT_OUT)
        if optout and not storage.is_live({"time": optout}):
            return Result.passed("ignored while expired; still stored until "
                                 "the next write, as specified")
        return Result.passed("ignored while expired, and gone after the next write")
    return check


async def act(walk):
    walk.begin("ACT 2", "opt-out, on every shop")

    await walk.step("the offer appears", visit_and_open)
    await walk.step("open opt-out", open_optout)
    await walk.step("it offers both scopes and every duration", check_choices)
    await walk.step("nothing changes until Apply", make_nothing_until_apply(walk))
    await walk.step("Back to activation returns to the offer", back_to_offer)

    await walk.step("open opt-out again", open_optout)
    await walk.step("apply 'this site' — a different duration on each shop",
                    make_apply_this_site(walk))
    await walk.step("revisiting shows nothing",
                    lambda s, t: revisit_expecting(walk.context, s, None, "silent"))
    forever = next(s for s in walk.sites if choice_for(s, walk.sites) == "forever")
    await walk.one("a forever opt-out outlives sixty days",
                   make_forever_outlives_sixty_days(walk), site=forever)
    await walk.clear()

    first = walk.sites[0]
    for choice in CHOICES:
        await walk.step("the offer appears", visit_and_open)
        await walk.one(f"apply 'all sites' {choice} on one shop",
                       make_apply_all_sites(walk, choice), site=first)
        # The tab that applied still shows its confirmation; a new tab is
        # the visit that has to stay empty.
        await walk.step(f"every shop stays silent ({choice})",
                        lambda s, t: revisit_expecting(walk.context, s, None, "silent"))
        await walk.clear(f"clearing the {choice} opt-out")

    await walk.one("an expired opt-out is ignored and cleared lazily",
                   make_expired_optout_clears_lazily(walk))
    await walk.clear()
