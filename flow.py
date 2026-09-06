#!/usr/bin/env python
"""Every retailer, in step, in one browser.

    python flow.py                 the whole walk
    python flow.py --headless      without the windows
    python flow.py --only popup    one act

The suite next door runs one test at a time, which is thorough and nothing like
a person. This walks the product the way a user meets it: four shops open at
once in one browser, sharing one extension and one `quietDomains` list, and
every tab takes the same step at the same moment.

Lockstep is what makes that safe. Concurrency inside a browser normally means
tests destroying each other — one tab opting out of all websites silences the
others mid-assertion. Here nobody is mid-assertion: every tab activates
together, every tab is checked together, the list is cleared for everyone
together, and only then does the next step begin. Interference has nowhere to
happen, and the shared list is exercised the way it actually lives.

What that buys, beyond realism: a step that fails, fails visibly for some shops
and not others, which is the difference between a product bug and a shop having
a bad day. A serial run cannot show you that.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bring import config as cfg, netspy, popup, retailers, search, storage  # noqa: E402
from bring.browser import extension_browser  # noqa: E402
from bring.env import find_manifest_dir  # noqa: E402

MINUTE = 60_000
HOUR = 60 * MINUTE

# What each control is specified to buy. Asserted against these rather than
# against "something plausible", which is what a client storing an invented
# value would also satisfy.
CLOSE_QUIET_MS = 30 * MINUTE
ACTIVATE_QUIET_MS = 2 * HOUR
TOLERANCE_MS = 2 * MINUTE

# Each shop picks a different duration, so one run covers all three and a bug
# that hands everyone 24 hours cannot hide behind a single choice.
OPTOUT_CHOICES = ["for_24h", "for_30d", "forever"]
OPTOUT_WINDOWS = {"for_24h": 24 * 60 * MINUTE, "for_30d": 30 * 24 * 60 * MINUTE}


class Result:
    """What one shop did in one step."""

    def __init__(self, ok: bool, detail: str = ""):
        self.ok = ok
        self.detail = detail

    @staticmethod
    def passed(detail=""):
        return Result(True, detail)

    @staticmethod
    def failed(detail):
        return Result(False, detail)


class Walk:
    """The browser, the tabs, and the tally."""

    def __init__(self, context, sites):
        self.context = context
        self.sites = sites
        self.tabs = {}
        self.rows = []          # (step, {site: Result})
        self.failures = 0

    async def open_tabs(self):
        for site in self.sites:
            self.tabs[site] = await self.context.new_page()

    async def close_tabs(self):
        for tab in self.tabs.values():
            try:
                await tab.close()
            except Exception:
                pass
        self.tabs.clear()

    async def step(self, title, work):
        """Run *work(site, tab)* for every shop at once, and record the row."""
        started = time.time()
        outcomes = await asyncio.gather(
            *(self._guarded(work, site) for site in self.sites))
        results = dict(zip(self.sites, outcomes))
        self.rows.append((title, results))
        self.failures += sum(0 if r.ok else 1 for r in results.values())
        self._print(title, results, time.time() - started)
        return results

    async def _guarded(self, work, site):
        try:
            return await work(site, self.tabs[site])
        except Exception as e:
            return Result.failed(f"{type(e).__name__}: {e}"[:110])

    def _print(self, title, results, seconds):
        mark = "ok " if all(r.ok for r in results.values()) else "FAIL"
        print(f"\n[{mark}] {title}   ({seconds:.0f}s)")
        for site, result in results.items():
            name = retailers.label(site)
            flag = "  ok " if result.ok else "  X  "
            print(f"   {flag}{name:<18} {result.detail}")

    async def clear(self, note="clearing the quiet list for everyone"):
        """Wipe the shared list between acts.

        Safe here in a way it is not inside the suite: every tab has finished
        the step, so there is no half-written state to lose.
        """
        await storage.delete(self.context, storage.QUIET_DOMAINS)
        await storage.delete(self.context, storage.OPT_OUT)
        print(f"\n-- {note}")


# ── the steps ───────────────────────────────────────────────────────

async def visit_and_open(site, tab):
    await tab.goto(site, wait_until="domcontentloaded")
    frame = await popup.wait_for_offer(tab, timeout=40)
    if not frame:
        return Result.failed("no popup appeared")
    if not await popup.visible(frame, popup.OFFER["activate"]):
        return Result.failed("popup has no activate button")
    return Result.passed("offer shown")


async def check_agree_line(site, tab):
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("the popup is gone")
    frame = frames[0]
    if not await popup.visible(frame, popup.OFFER["terms_link"]):
        return Result.failed("no 'Deal Terms' link")
    if not await popup.visible(frame, popup.OFFER["tou_link"]):
        return Result.failed("no 'Terms of Use' link")
    line = await popup.text(frame, popup.OFFER["agree_text"])
    if "privacy" in line.lower():
        return Result.failed(f"the Privacy link is back: {line[:60]!r}")
    return Result.passed("Deal Terms + Terms of Use, no Privacy")


async def activate(site, tab):
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("the popup is gone")
    if not await popup.click(frames[0], popup.OFFER["activate"], settle=5):
        return Result.failed("could not click activate")
    confirmed = await popup.wait_for_confirmation(tab)
    if not confirmed:
        return Result.failed("no confirmation screen")
    body = await popup.body_text(confirmed)
    if "undefined" in body or "NaN" in body:
        return Result.failed(f"unresolved value on the confirmation: {body[:60]!r}")
    return Result.passed("confirmation shown")


def window_check(entry, expected, label):
    window = storage.window_ms(entry)
    if window is None:
        return Result.failed(f"malformed range: {entry.get('time')!r}")
    if abs(window - expected) > TOLERANCE_MS:
        return Result.failed(
            f"{label}: expected {expected / HOUR:.2f}h, stored {window / HOUR:.2f}h")
    return None


async def check_activated_row(site, tab, walk):
    entry = await storage.quiet_entry(walk.context, site)
    if not entry:
        return Result.failed("nothing written to quietDomains")
    if not str(entry.get("domain", "")).startswith("*."):
        return Result.failed(f"not a wildcard: {entry.get('domain')!r}")
    if entry.get("phase") != "activated":
        return Result.failed(f"phase is {entry.get('phase')!r}")
    bad = window_check(entry, ACTIVATE_QUIET_MS, "activate")
    return bad or Result.passed(
        f"*.{storage.normalise(site)} activated, "
        f"{storage.window_ms(entry) / HOUR:.2f}h")


async def revisit_expecting(site, tab, walk, want_route, what):
    tab2 = await walk.context.new_page()
    try:
        await tab2.goto(site, wait_until="domcontentloaded")
        shown = await popup.wait_for_popup(tab2, timeout=25)
        route = popup.route_of(shown) if shown else None
        if want_route is None:
            if shown:
                return Result.failed(f"expected silence, got the {route} surface")
            return Result.passed("silent")
        if route != want_route:
            return Result.failed(f"expected {what}, got {route!r}")
        return Result.passed(what)
    finally:
        await tab2.close()


async def close_popup(site, tab):
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("no popup to close")
    if not await popup.click(frames[0], popup.OFFER["close_x"], settle=3):
        return Result.failed("could not click the X")
    if not await popup.wait_for_gone(tab, timeout=10):
        return Result.failed("the popup is still on the page")
    return Result.passed("closed")


async def check_closed_row(site, tab, walk):
    entry = await storage.quiet_entry(walk.context, site)
    if not entry:
        return Result.failed("nothing written to quietDomains")
    if entry.get("phase") != "quiet":
        return Result.failed(f"phase is {entry.get('phase')!r}")
    bad = window_check(entry, CLOSE_QUIET_MS, "close")
    return bad or Result.passed(
        f"quiet for {storage.window_ms(entry) / MINUTE:.0f} minutes")


async def open_optout(site, tab):
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("the popup is gone")
    if not await popup.click(frames[0], popup.OFFER["optout"], settle=2):
        return Result.failed("no opt-out control")
    if not await popup.visible(frames[0], popup.OPTOUT["card"]):
        return Result.failed("the opt-out screen did not open")
    return Result.passed("opt-out screen open")


def choice_for(site, sites):
    return OPTOUT_CHOICES[sites.index(site) % len(OPTOUT_CHOICES)]


async def apply_optout(site, tab, walk):
    choice = choice_for(site, walk.sites)
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("the popup is gone")
    frame = frames[0]
    await popup.click(frame, popup.OPTOUT["this_site"], settle=0.5)
    await popup.click(frame, popup.OPTOUT[choice], settle=0.5)
    if not await popup.click(frame, popup.OPTOUT["apply"], settle=3):
        return Result.failed("could not click Apply")
    if not await popup.visible(frame, popup.OPTOUT["confirmation"]):
        return Result.failed("no confirmation after Apply")

    entry = await storage.quiet_entry(walk.context, site)
    if not entry:
        return Result.failed(f"{choice}: nothing written")
    window = storage.window_ms(entry)
    if window is None:
        return Result.failed(f"{choice}: malformed range")
    if choice == "forever":
        if window <= 60 * 24 * 60 * MINUTE:
            return Result.failed(
                f"forever was capped at {window / (24 * 60 * MINUTE):.0f} days")
        return Result.passed("forever, past the old 60-day cap")
    expected = OPTOUT_WINDOWS[choice]
    if abs(window - expected) > TOLERANCE_MS:
        return Result.failed(
            f"{choice}: expected {expected / (24 * 60 * MINUTE):.0f}d, "
            f"stored {window / (24 * 60 * MINUTE):.2f}d")
    return Result.passed(f"{choice} honoured exactly")


async def arrive_with_affiliate_marker(site, tab, walk):
    """Arrive carrying somebody else's attribution, and stay out of the way.

    Judged on the server's verdict as well as the screen. The QA plan puts it
    exactly that way — the popup check comes back `isValid = false` — and a
    frame that happens to be absent is much weaker evidence: a slow page, a
    leftover from the previous step, or a shop that redirected can all produce
    "no popup" without the stand-down having done anything.
    """
    await netspy.install(walk.context, netspy.PASS)
    await netspy.reset(walk.context)

    joiner = "&" if "?" in site else "?"
    await tab.goto(f"{site}{joiner}irclickid=e2e-flow-probe",
                   wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(tab, timeout=15)

    if "irclickid" not in tab.url:
        return Result.passed("marker stripped by the shop; nothing to judge")

    answer = await netspy.last_body(walk.context, netspy.POPUP_CHECK)
    verdict = None if answer is None else answer.get("isValid")

    if shown:
        return Result.failed(
            f"the popup appeared over an affiliate link "
            f"(server said isValid={verdict!r}, surface {popup.route_of(shown)!r})")
    if verdict is True:
        return Result.failed(
            "no popup, but the server still called the arrival valid — the "
            "stand-down did not happen; something else hid the popup")

    entry = await storage.quiet_entry(walk.context, site)
    if not entry:
        return Result.failed("stood down but wrote no row")
    if not str(entry.get("type", "")).startswith("kdi"):
        return Result.failed(f"type is {entry.get('type')!r}, expected kdi")
    offset = await storage.get(walk.context, "standDownOffset")
    window = storage.window_ms(entry)
    if isinstance(offset, (int, float)) and offset > 0 and window:
        if abs(window - offset) > TOLERANCE_MS:
            return Result.failed(
                f"standDownOffset is {offset / HOUR:.2f}h, stored {window / HOUR:.2f}h")
    return Result.passed(f"stood down, kdi, {window / HOUR:.2f}h")


async def navigate_and_expect_popup(site, tab, walk, mode):
    """Back, forward or refresh, on a shop that was never silenced.

    The popup has to come back: these are re-checks, not cache hits. Without
    this the sibling step below passes on an extension that stopped reacting to
    navigation altogether.
    """
    control = next((s for s in walk.sites if s != site), site)
    if mode == "reload":
        await tab.reload(wait_until="domcontentloaded")
    else:
        await tab.goto(control, wait_until="domcontentloaded")
        await tab.go_back(wait_until="domcontentloaded")
        if mode == "forward":
            await tab.go_forward(wait_until="domcontentloaded")
            await tab.go_back(wait_until="domcontentloaded")

    frame = await popup.wait_for_popup(tab, timeout=35)
    if not frame:
        return Result.failed(f"after {mode} the offer did not come back")
    return Result.passed(f"{mode} re-checked the page")


async def navigate_and_expect_silence(site, tab, walk, mode):
    """The same three, on a shop that was just closed. None may resurrect it."""
    control = next((s for s in walk.sites if s != site), site)
    if mode == "reload":
        await tab.reload(wait_until="domcontentloaded")
    else:
        await tab.goto(control, wait_until="domcontentloaded")
        await tab.go_back(wait_until="domcontentloaded")
        if mode == "forward":
            await tab.go_forward(wait_until="domcontentloaded")
            await tab.go_back(wait_until="domcontentloaded")

    frame = await popup.wait_for_popup(tab, timeout=12)
    if frame:
        return Result.failed(f"after {mode} the offer came back on a silenced shop")
    return Result.passed(f"{mode} kept it silent")


# ── the acts ────────────────────────────────────────────────────────

async def act_popup(walk):
    print("\n" + "=" * 64)
    print("ACT 1 - the offer, on every shop at once")
    print("=" * 64)

    await walk.step("the offer appears", visit_and_open)
    await walk.step("the agree line carries both links", check_agree_line)

    await walk.step("activate", activate)
    await walk.step("quietDomains says activated, for two hours",
                    lambda s, t: check_activated_row(s, t, walk))
    await walk.step("revisiting shows the confirmation again",
                    lambda s, t: revisit_expecting(s, t, walk, "activated",
                                                   "confirmation"))
    await walk.clear()

    await walk.step("the offer is back once the row is gone", visit_and_open)
    await walk.step("close it", close_popup)
    await walk.step("quietDomains says quiet, for thirty minutes",
                    lambda s, t: check_closed_row(s, t, walk))
    await walk.step("revisiting stays silent",
                    lambda s, t: revisit_expecting(s, t, walk, None, "silent"))
    for mode in ("reload", "back", "forward"):
        await walk.step(f"{mode} does not bring a silenced offer back",
                        lambda s, t, m=mode: navigate_and_expect_silence(s, t, walk, m))
    await walk.clear()

    await walk.step("the offer is back after clearing", visit_and_open)
    for mode in ("reload", "back", "forward"):
        await walk.step(f"{mode} re-checks and the offer returns",
                        lambda s, t, m=mode: navigate_and_expect_popup(s, t, walk, m))
    await walk.clear()

    await walk.step("the offer is back again", visit_and_open)
    await walk.step("open opt-out", open_optout)
    await walk.step("apply — a different duration on each shop",
                    lambda s, t: apply_optout(s, t, walk))
    await walk.clear()

    await walk.step("arriving on an affiliate link stands down",
                    lambda s, t: arrive_with_affiliate_marker(s, t, walk))
    await walk.clear()


async def act_widget(context, headless):
    """The badge, and the two closes that look alike and are not."""
    print("\n" + "=" * 64)
    print("ACT 2 - the widget, on the one shop that has it")
    print("=" * 64)

    site = retailers.WIDGET_SITE
    name = retailers.label(site)
    failures = 0

    def say(ok, text):
        nonlocal failures
        print(f"   {'ok ' if ok else 'X  '} {text}")
        if not ok:
            failures += 1

    async def fresh():
        await storage.delete(context, storage.QUIET_DOMAINS)
        tab = await context.new_page()
        await tab.goto(site, wait_until="domcontentloaded")
        frame = await popup.wait_for_popup(tab, timeout=40)
        return tab, frame

    tab, frame = await fresh()
    try:
        if not frame:
            say(False, f"nothing appeared on {name}")
            return failures
        if not await popup.is_widget(frame):
            print(f"   --  {name} sent the full popup, not a badge; "
                  f"the widget is off here")
            return failures

        say(True, "the badge is showing, collapsed")
        say(not await popup.visible(frame, popup.OFFER["activate"]),
            "the offer is still closed behind it")

        element = await tab.query_selector(f'iframe[id^="{popup.IFRAME_ID_PREFIX}-"]')
        box = await element.bounding_box() if element else None
        say(bool(box) and box["width"] < 300,
            f"the frame is badge-sized ({box and int(box['width'])}px wide)")

        say(await popup.expand_widget(frame), "clicking the badge opens the offer")
        say(await popup.visible(frame, popup.OFFER["activate"]),
            "the expanded offer has its activate button")
        # The badge zooms out while the card scales up in its place, so reading
        # this the instant the offer appears catches the hand-over rather than
        # the result.
        await tab.wait_for_timeout(1500)
        say(not await popup.visible(frame, popup.WIDGET["collapsed"]),
            "the badge is gone once expanded")

        element = await tab.query_selector(f'iframe[id^="{popup.IFRAME_ID_PREFIX}-"]')
        box = await element.bounding_box() if element else None
        say(bool(box) and box["width"] > 200,
            f"the frame grew to fit it ({box and int(box['width'])}px wide)")

        say(await popup.visible(frame, popup.OFFER["terms_link"]),
            "Deal Terms is reachable from the widget")
    finally:
        await tab.close()

    # The expanded offer's X is a real close: it silences the shop.
    tab, frame = await fresh()
    try:
        if frame and await popup.is_widget(frame) and await popup.expand_widget(frame):
            closed = (await popup.click(frame, popup.OFFER["close_x"], settle=3)
                      or await popup.click(frame, popup.OFFER["close_cancel"], settle=3))
            say(closed, "the expanded offer closes")
            entry = await storage.quiet_entry(context, site)
            window = storage.window_ms(entry) if entry else None
            say(bool(entry) and entry.get("phase") == "quiet"
                and window is not None
                and abs(window - CLOSE_QUIET_MS) <= TOLERANCE_MS,
                f"closing it silences {name} for "
                f"{window / MINUTE:.0f} minutes" if window else
                f"closing it wrote no quiet row")
    finally:
        await tab.close()

    # The badge's own X is a dismiss, and must not.
    tab, frame = await fresh()
    try:
        if frame and await popup.is_widget(frame):
            say(await popup.click(frame, popup.WIDGET["close"], settle=3),
                "the badge has its own X")
            entry = await storage.quiet_entry(context, site)
            say(entry is None,
                "dismissing the badge silences nothing — it is not a close"
                if entry is None else
                f"dismissing the badge silenced {name} ({entry.get('phase')!r})")
    finally:
        await tab.close()
        await storage.delete(context, storage.QUIET_DOMAINS)

    return failures


async def act_offerbar(context, keyword):
    """The bar over search results, given the same treatment as the popup.

    Whether it arrives as an offer bar or a top bar is the server's choice and
    the same surface either way — the checks below are the bar's, not the
    layout's. Reached only by searching: nobody visits a shop to see it.
    """
    print("\n" + "=" * 64)
    print(f"ACT 3 - the bar, searching {keyword!r} on Google")
    print("=" * 64)

    failures = 0

    def say(ok, text):
        nonlocal failures
        print(f"   {'ok ' if ok else 'X  '} {text}")
        if not ok:
            failures += 1

    async def fresh_bar():
        """A results page with the bar on it, or (tab, None) and why."""
        await storage.delete(context, storage.QUIET_DOMAINS)
        await netspy.install(context, netspy.PASS)
        await netspy.reset(context)
        tab = await context.new_page()
        reason = await search.search(tab, keyword, engine="google")
        if reason:
            return tab, None, reason
        frame = await popup.wait_for_popup(tab, timeout=20, route="offerbar")
        if frame:
            return tab, frame, None
        decided = await netspy.server_said_offerbar(context)
        if decided:
            return tab, None, "the server asked for a bar and none appeared"
        return tab, None, (f"the server did not ask for a bar on {keyword!r} "
                           f"(the term is not registered here)")

    def shop_row(rows):
        for row in rows:
            if isinstance(row, dict) and "google" not in str(row.get("domain", "")):
                return row
        return None

    # 1 — it appears, with an offer in it
    tab, frame, why = await fresh_bar()
    try:
        if not frame:
            # A term the backend does not carry is not a finding; a term it
            # does carry with no bar behind it is. `fresh_bar` has already told
            # them apart, so the wording decides whether this counts.
            if "did not ask" in (why or ""):
                print(f"   --  {why}")
                return failures
            say(False, why)
            return failures

        say(True, "the bar is over the results")
        say(await popup.visible(frame, popup.OFFERBAR["activate"]),
            "it has an activate button")
        say(not await popup.visible(frame, popup.OFFER["connect_wallet"]),
            "and no wallet-connect, which belongs to the popup")
        say(len(popup.frames(tab, "offerbar")) == 1, "exactly one bar")

        before = await tab.evaluate("() => document.documentElement.scrollHeight")
        if await popup.visible(frame, popup.OFFERBAR["spacer"]):
            say(True, "it reserves space rather than covering the results")
        say(await popup.visible(frame, popup.OFFERBAR["optout"]),
            "opt-out is reachable from the bar")
    finally:
        await tab.close()

    # 2 — closing it silences the shop, not Google, and gives the space back
    tab, frame, why = await fresh_bar()
    try:
        if frame:
            closed = (await popup.click(frame, popup.OFFERBAR["close_top"], settle=2)
                      or await popup.click(frame, popup.OFFERBAR["close_bottom"], settle=2))
            say(closed, "the bar closes")
            say(await popup.wait_for_gone(tab, timeout=10, route="offerbar"),
                "and leaves the page")

            rows = await storage.quiet_domains(context)
            engine = storage.entry_for(rows, "https://www.google.com")
            say(not engine,
                "the silence landed on the shop, not on Google"
                if not engine else
                f"it silenced Google itself ({engine.get('domain')!r})")
            shop = shop_row(rows)
            say(bool(shop),
                f"the shop it offered is now quiet: {shop.get('domain')!r}"
                if shop else "nothing that looks like a shop was silenced")
            if shop:
                window = storage.window_ms(shop)
                say(window is not None
                    and abs(window - CLOSE_QUIET_MS) <= TOLERANCE_MS,
                    f"for {window / MINUTE:.0f} minutes" if window
                    else "with a malformed window")
    finally:
        await tab.close()

    # 3 — activating from the bar
    tab, frame, why = await fresh_bar()
    try:
        if frame:
            say(await popup.click(frame, popup.OFFERBAR["activate"], settle=6),
                "activate can be clicked from the bar")
            rows = await storage.quiet_domains(context)
            shop = shop_row(rows)
            say(bool(shop) and shop.get("phase") == "activated",
                f"the shop is now activated: {shop.get('domain')!r}" if shop
                else "activating from the bar wrote no shop row")
            engine = storage.entry_for(rows, "https://www.google.com")
            say(not engine, "and Google was left alone"
                if not engine else f"it activated against Google itself")
    finally:
        await tab.close()

    # 4 — opt-out from the bar
    tab, frame, why = await fresh_bar()
    try:
        if frame:
            opened = await popup.click(frame, popup.OFFERBAR["optout"], settle=2)
            say(opened and await popup.visible(frame, popup.OPTOUT["card"]),
                "opt-out opens from the bar")
    finally:
        await tab.close()

    # 5 — the analytics call it a keyword search
    analytics = netspy.PageCalls()
    await analytics.watch(context, "**/analytics")
    tab, frame, why = await fresh_bar()
    try:
        if frame:
            await tab.wait_for_timeout(2500)
            events = analytics.events()
            triggers = {e.get("triggerType") for e in events
                        if isinstance(e, dict) and e.get("triggerType")}
            if not triggers:
                print("   --  no analytics events carried a triggerType")
            else:
                say(triggers == {"keyword"},
                    f"reported as triggerType {triggers}")
    finally:
        await tab.close()
        await storage.delete(context, storage.QUIET_DOMAINS)

    return failures


async def act_notifications(context):
    print("\n" + "=" * 64)
    print("ACT 4 - the reward check, and what a failure costs")
    print("=" * 64)

    address = ("addr1qydfh2z0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5psyq"
               "79gflnhwlttgw8gk7aj5j6lj95vg7my67vpsdcvu4l")
    tab = await context.new_page()
    failures = 0
    try:
        await netspy.install(context, netspy.PASS)
        await tab.goto("https://example.com", wait_until="domcontentloaded")

        # Reset after the navigation has had its own check. Loading a page
        # triggers one on its own, so counting from before it measures two
        # events and calls the result "one check" when it is not.
        await tab.wait_for_timeout(3000)
        await netspy.reset(context)

        await netspy.broadcast_wallet(tab, address)
        await tab.wait_for_timeout(4000)
        first = await netspy.count(context, netspy.NOTIFICATION_CHECK)
        print(f"   {'ok ' if first == 1 else 'X  '} a new wallet address "
              f"triggers exactly one check ({first})")
        failures += 0 if first == 1 else 1

        await netspy.reset(context)
        for _ in range(4):
            await netspy.broadcast_wallet(tab, address)
            await tab.wait_for_timeout(400)
        repeats = await netspy.count(context, netspy.NOTIFICATION_CHECK)
        print(f"   {'ok ' if repeats == 0 else 'X  '} re-broadcasting the same "
              f"address costs nothing ({repeats} calls)")
        failures += 0 if repeats == 0 else 1

        await netspy.set_mode(context, netspy.FAIL)
        await storage.delete(context, storage.NOTIFICATION_CHECK)
        await storage.delete(context, storage.LAST_CHECKED_WALLET)
        await netspy.broadcast_wallet(tab, address + "x")
        await tab.wait_for_timeout(4000)

        window = await storage.get(context, storage.NOTIFICATION_CHECK)
        if isinstance(window, list) and len(window) == 2:
            span = window[1] - window[0]
            ok = abs(span - HOUR) < 5 * MINUTE
            print(f"   {'ok ' if ok else 'X  '} a failed check backs off "
                  f"{span / MINUTE:.0f} minutes")
            failures += 0 if ok else 1
        else:
            print(f"   X   a failed check stored {window!r}, not a range")
            failures += 1
    finally:
        await netspy.set_mode(context, netspy.PASS)
        await tab.close()
    return failures


async def act_notification_variants(context):
    """The five screens, each produced by putting rows where the server looks.

    Not a client-side switch: the server computes which variant to send from
    `purchases`, signs it into a token, and the iframe only reads it. So the
    only honest way to see all five is to write the rows and let it decide —
    which also means this act needs the environment's database, and says so
    plainly when it cannot reach one.
    """
    print("\n" + "=" * 64)
    print("ACT 5 - the five notification variants")
    print("=" * 64)

    from bring import db, seed as seeder

    if not db.configured():
        print("   --  no database configured, so no variant can be produced")
        return 0
    try:
        database = db.database_for()
    except db.NoDatabase as e:
        print(f"   --  {str(e).splitlines()[0]}")
        return 0

    address = ("addr1qydfh2z0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5psyq"
               "79gflnhwlttgw8gk7aj5j6lj95vg7my67vpsdcvu4l")
    expected = {
        "reward_approval": ("Details", False),
        "walletless_1": ("Connect", False),
        "walletless_2": ("Connect", False),
        "walletless_3": ("Claim", False),
        "walletless_4": ("Claim", True),
    }

    failures = 0
    user_id = await storage.get(context, "id")
    if not user_id:
        print("   X   the extension has no user id to seed against")
        return 1

    for variant, (want_cta, want_stop) in expected.items():
        written = None
        tab = await context.new_page()
        try:
            seeder.clean_all(database)
            written = seeder.seed(variant, database=database, user_id=user_id,
                                  wallet_address=address)

            # The wallet has to be in its final state *before* the page loads.
            # Navigation is what triggers the reward check, and an empty
            # broadcast triggers nothing at all — it only removes the address
            # (handleContentMessages, WALLET_ADDRESS_UPDATE). Broadcasting
            # after navigating therefore measured the previous variant's wallet:
            # walletless_1 came back with "Details", which is the
            # wallet-connected screen.
            for key in (storage.NOTIFICATION, storage.NOTIFICATION_CHECK,
                        storage.LAST_CHECKED_WALLET, storage.WALLET_ADDRESS):
                await storage.delete(context, key)

            await netspy.install(context, netspy.PASS)
            wants_wallet = variant == "reward_approval"
            # Both wallet keys, before anything navigates. The host extension's
            # own `walletAddress` is what the content script reports, and a
            # stale one there is enough to make the server answer as though a
            # wallet were connected.
            await netspy.set_host_wallet(context, address if wants_wallet else "")

            await tab.goto("https://example.com", wait_until="domcontentloaded")
            await netspy.broadcast_wallet(tab, address if wants_wallet else "")
            await tab.wait_for_timeout(1500)
            # Now the state is right; this navigation is the one that counts.
            await tab.goto("https://example.com/?check", wait_until="domcontentloaded")
            await tab.wait_for_timeout(6000)

            frame = await popup.wait_for_popup(tab, timeout=20, route="notification")
            if not frame:
                answered = await netspy.last_body(context, netspy.NOTIFICATION_CHECK)
                shown = (answered or {}).get("showNotification")
                print(f"   X   {variant:<16} nothing appeared "
                      f"(server said showNotification={shown!r})")
                failures += 1
                continue

            cta = await popup.text(frame, popup.NOTIFICATION["cta"])
            stop = await popup.visible(frame, popup.NOTIFICATION["stop_reminders"])
            body = await popup.body_text(frame)

            problems = []
            if cta != want_cta:
                problems.append(f"button says {cta!r}, expected {want_cta!r}")
            if stop != want_stop:
                problems.append(
                    "Stop reminding is showing" if stop else "no Stop reminding")
            if "undefined" in body or "NaN" in body:
                problems.append(f"unresolved value: {body[:50]!r}")

            if problems:
                print(f"   X   {variant:<16} {'; '.join(problems)}")
                failures += 1
            else:
                print(f"   ok  {variant:<16} {cta}"
                      + (" + Stop reminding" if stop else ""))
        except Exception as e:
            print(f"   X   {variant:<16} {type(e).__name__}: {e}"[:110])
            failures += 1
        finally:
            await tab.close()
            if written:
                try:
                    seeder.clean(written)
                except Exception:
                    pass

    return failures


# ── the walk ────────────────────────────────────────────────────────

async def run(only, headless):
    extension = find_manifest_dir(ROOT / "extension" / cfg.ENV_NAME)
    profile = ROOT / "profiles" / f"flow-{os.getpid()}"
    sites = retailers.sites()

    print(f"Extension: {extension}")
    print(f"API:       {cfg.env_api_url()}")
    print(f"Shops:     {', '.join(retailers.label(s) for s in sites)}   "
          f"(in step, one browser)")

    failures = 0
    async with extension_browser(extension, profile, headless=headless) as context:
        await storage.settled(context)

        if only in (None, "popup"):
            walk = Walk(context, sites)
            await walk.open_tabs()
            try:
                await act_popup(walk)
            finally:
                await walk.close_tabs()
            failures += walk.failures

        if only in (None, "widget"):
            failures += await act_widget(context, headless)
        if only in (None, "offerbar"):
            failures += await act_offerbar(
                context, os.getenv("BRING_OFFERBAR_KEYWORDS", "c ondor").split(",")[0])
        if only in (None, "notification"):
            failures += await act_notifications(context)
            failures += await act_notification_variants(context)

    print("\n" + "=" * 64)
    if failures:
        print(f"FAIL - {failures} step(s) did not do what they should")
    else:
        print("PASS - every shop did the same right thing at the same time")
    print("=" * 64)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--only", choices=["popup", "widget", "offerbar",
                                           "notification"], default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.only, args.headless))


if __name__ == "__main__":
    sys.exit(main())
