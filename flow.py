#!/usr/bin/env python
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

    async def one(self, title, work, site=None):
        """Run *work* against a single shop and record it like any other step.

        For checks whose subject is the extension's own bookkeeping rather than
        a shop — how it walks the quiet list, say. Asking four shops the same
        question adds nothing, and asking them at once makes the answer wrong:
        a popup check can come back with `quietDomainsChanged`, and the SDK
        then replaces the whole list, wiping the rows the other three tabs are
        relying on. Measured — the shop that failed changed run to run.
        """
        site = site or self.sites[0]
        started = time.time()
        result = await self._guarded(work, site)
        self.rows.append((title, {site: result}))
        self.failures += 0 if result.ok else 1
        self._print(title, {site: result}, time.time() - started)
        return result

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
    await netspy.install(walk.context, netspy.PASS)
    await netspy.reset(walk.context)

    joiner = "&" if "?" in site else "?"
    await tab.goto(f"{site}{joiner}irclickid=e2e-flow-probe",
                   wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(tab, timeout=15)

    if "irclickid" not in tab.url:
        return Result.passed("marker stripped by the shop; nothing to judge")

    if shown:
        return Result.failed(
            f"the popup appeared over an affiliate link "
            f"(surface {popup.route_of(shown)!r})")

    entry = await storage.await_quiet_entry(walk.context, site)
    if not entry:
        # No popup and no row: the stand-down did not happen and something else
        # kept the offer away. Said plainly rather than counted as a pass, since
        # a silent screen for the wrong reason is what this step exists to catch.
        return Result.failed(
            "no popup, but nothing was written either — the stand-down did not "
            "happen and something else hid the offer")
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


# ── the offer, in detail ────────────────────────────────────────────
# What the suite used to check one shop at a time, asked of all four at once.
# The saving is not the assertion — it is the arrival: one navigation answers
# several questions instead of one each.

async def check_deal_terms(site, tab):
    """7.2 — the terms open over the offer and Back returns to it."""
    frames = popup.frames(tab)
    if not frames:
        return Result.failed("the popup is gone")
    frame = frames[0]

    if not await popup.click(frame, popup.OFFER["terms_link"], settle=1.5):
        return Result.failed("could not click 'Deal Terms'")
    if not await popup.visible(frame, popup.OFFER["terms_box"]):
        return Result.failed("the terms view did not open")

    body = await popup.text(frame, popup.OFFER["terms_box"])
    if not body.strip():
        return Result.failed("the terms view opened empty")
    if "undefined" in body or "NaN" in body:
        return Result.failed(f"unresolved rate in the terms: {body[:60]!r}")

    if not await popup.click(frame, popup.OFFER["terms_back"], settle=1.5):
        return Result.failed("the terms view has no Back button")
    if not await popup.visible(frame, popup.OFFER["activate"]):
        return Result.failed("Back did not return to the offer")
    return Result.passed("terms open and Back returns")


async def check_one_popup_only(site, tab):
    """1.12 — one offer per page, however many frames the shop carries."""
    frames = [f for f in popup.frames(tab) if await popup.rendered(f)]
    if not frames:
        return Result.failed("no popup at all")
    if len(frames) > 1:
        routes = [popup.route_of(f) for f in frames]
        return Result.failed(f"{len(frames)} popups on one page: {routes}")
    return Result.passed("exactly one")


async def check_silence_is_scoped(site, tab, walk):
    rows = await storage.quiet_domains(walk.context)
    mine = storage.entry_for(rows, site)
    if not mine:
        return Result.failed("closing wrote no row for this shop")

    missing = [s for s in walk.sites
               if not storage.entry_for(rows, s)]
    if missing:
        names = ", ".join(retailers.label(s) for s in missing)
        return Result.failed(f"but {names} lost their row — the silence spread")
    return Result.passed(f"{mine.get('domain')!r}, and the others untouched")


async def check_silence_expires(site, tab, walk):
    if not await storage.expire_quiet(walk.context, site):
        return Result.failed("no quiet row to expire")

    await tab.goto(site, wait_until="domcontentloaded")
    frame = await popup.wait_for_offer(tab, timeout=35)
    if not frame:
        return Result.failed("the window expired but the offer did not return")
    return Result.passed("the offer returns once the window ends")


async def check_expired_row_hides_nothing(site, tab, walk):
    # `*.host`, which is the shape the SDK itself writes. A bare host does not
    # match `www.<host>` the way the reverse-string matching works, so a row
    # written without the wildcard silences nothing and the test measures its
    # own mistake.
    host = f"*.{storage.normalise(site)}"
    now = int(time.time() * 1000)
    await storage.set(walk.context, storage.QUIET_DOMAINS, [
        {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
         "time": [now - 4 * HOUR, now - 2 * HOUR]},          # long dead
        {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
         "time": [now, now + 30 * MINUTE]},                  # live
    ])

    await tab.goto(site, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(tab, timeout=12)
    if frame:
        return Result.failed("the expired row hid the live one and the offer showed")
    return Result.passed("the live row still silences")


async def expire_every_row(walk):
    """Move every shop's quiet row into the past, in one write.

    The list belongs to the browser, not to a tab, so anything that rewrites it
    has to happen once — outside `walk.step`, which runs all four tabs at the
    same time.
    """
    rows = await storage.quiet_domains(walk.context)
    for entry in rows:
        if isinstance(entry, dict):
            entry["time"] = storage.past()
    await storage.set(walk.context, storage.QUIET_DOMAINS, rows)
    print(f"\n-- moving every silence into the past ({len(rows)} row(s))")


async def seed_dead_and_live_rows(walk, only=None):
    """One dead row and one live row per shop, written together.

    `*.host` is the shape the SDK itself writes; a bare host does not match
    `www.<host>` under the reverse-string matching, and a row that matches
    nothing tests nothing.
    """
    now = int(time.time() * 1000)
    rows = []
    for site in ([only] if only else walk.sites):
        host = f"*.{storage.normalise(site)}"
        rows.append({"domain": host, "type": "kds", "phase": "quiet",
                     "isRegex": False,
                     "time": [now - 4 * HOUR, now - 2 * HOUR]})   # long dead
        rows.append({"domain": host, "type": "kds", "phase": "quiet",
                     "isRegex": False,
                     "time": [now, now + 30 * MINUTE]})           # live
    await storage.set(walk.context, storage.QUIET_DOMAINS, rows)
    print(f"\n-- a dead row and a live one for each shop ({len(rows)} rows)")


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


async def act_offer_detail(walk):
    """Everything the suite asked about the offer and its silence."""
    print("\n" + "=" * 64)
    print("ACT 1b - the offer in detail, on every shop at once")
    print("=" * 64)

    await walk.step("the offer appears", visit_and_open)
    await walk.step("only one popup per page", check_one_popup_only)
    await walk.step("Deal Terms opens and Back returns", check_deal_terms)

    await walk.step("close it", close_popup)
    await walk.step("the silence names this shop and no other",
                    lambda s, t: check_silence_is_scoped(s, t, walk))

    await expire_every_row(walk)
    await walk.step("and the offer returns when the window ends",
                    lambda s, t: check_silence_expires(s, t, walk))
    await walk.clear()

    # One shop: this is about how the extension reads its own list, and four
    # tabs checking at once let one server answer replace the rows the others
    # need.
    await seed_dead_and_live_rows(walk, only=walk.sites[0])
    await walk.one("an expired row does not hide a live one",
                   lambda s, t: check_expired_row_hides_nothing(s, t, walk))
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
            # The badge's X is the offer's close: Home.tsx hands the widget the
            # same `close`, and the background turns it into a quiet row with no
            # widget branch of any kind. Confirmed as intended.
            entry = await storage.quiet_entry(context, site)
            say(entry is not None,
                f"dismissing the badge silences {name} ({entry.get('phase')!r})"
                if entry else
                f"dismissing the badge silenced nothing — the close did not land")
    finally:
        await tab.close()
        await storage.delete(context, storage.QUIET_DOMAINS)

    return failures


async def act_offerbar(context, keyword):
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
        await storage.delete(context, storage.QUIET_DOMAINS)
        await netspy.install(context, netspy.PASS)
        await netspy.reset(context)
        tab = await context.new_page()
        reason = await search.search(tab, keyword, engine="google")
        if reason:
            return tab, None, None, reason
        frame = await popup.wait_for_bar(tab, timeout=25)
        if frame:
            return tab, frame, await popup.controls_for(frame), None
        decided = await netspy.server_said_offerbar(context)
        if decided:
            return tab, None, None, "the server asked for a bar and none appeared"
        return tab, None, None, (f"the server did not ask for a bar on {keyword!r} "
                                 f"(the term is not registered here)")

    async def bar_returns(tab):
        reason = await search.search(tab, keyword, engine="google")
        if reason:
            return None
        return await popup.wait_for_bar(tab, timeout=12) is not None

    def shop_row(rows):
        for row in rows:
            if isinstance(row, dict) and "google" not in str(row.get("domain", "")):
                return row
        return None

    # 1 — it appears, with an offer in it
    tab, frame, ctl, why = await fresh_bar()
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

        layout = "top bar" if ctl is popup.TOPBAR else "offer bar"
        say(True, f"the bar is over the results (as the {layout})")
        say(await popup.visible(frame, ctl["activate"]),
            "it has an activate button")
        say(not await popup.visible(frame, popup.OFFER["connect_wallet"]),
            "and no wallet-connect, which belongs to the popup")
        say(len(popup.bars(tab)) == 1, "exactly one bar")

        reserved = await tab.evaluate(popup.BODY_RESERVATION)
        if ctl is popup.TOPBAR:
            say(bool(reserved["transform"]),
                "it pushes the results down rather than covering them")
        elif await popup.visible(frame, ctl["spacer"]):
            say(True, "it reserves space rather than covering the results")
        say(await popup.visible(frame, ctl["optout"]),
            "opt-out is reachable from the bar")
    finally:
        await tab.close()

    # 2 — closing it silences the shop, not Google, and gives the space back
    tab, frame, ctl, why = await fresh_bar()
    try:
        if frame:
            closed = (await popup.click(frame, ctl["close_top"], settle=2)
                      or await popup.click(frame, ctl["close_bottom"], settle=2))
            say(closed, "the bar closes")
            say(await popup.bars_gone(tab, timeout=10),
                "and leaves the page")
            back = await tab.evaluate(popup.BODY_RESERVATION)
            say(not back["transform"], "and gives the reserved space back"
                if not back["transform"] else
                f"but left the page pushed down ({back['transform']})")

            returned = await bar_returns(tab)
            if returned is None:
                print("   --  could not search again to check the silence held")
            else:
                say(not returned, "and stays away on the next search")

            # Closing a bar quiets the search engine, not the shop: the bar
            # belongs to the search. Both layouts hard-code 'google.com' here
            # rather than using the searchEngineDomain they were given, so this
            # only lines up while the engine is Google.
            rows = await storage.quiet_domains(context)
            engine = storage.entry_for(rows, "https://www.google.com")
            say(bool(engine),
                f"the engine it was shown on is now quiet: {engine.get('domain')!r}"
                if engine else
                "nothing was silenced, so the bar returns on the next search")
            if engine:
                window = storage.window_ms(engine)
                say(window is not None
                    and abs(window - CLOSE_QUIET_MS) <= TOLERANCE_MS,
                    f"for {window / MINUTE:.0f} minutes" if window
                    else "with a malformed window")
    finally:
        await tab.close()

    # 3 — activating from the bar
    tab, frame, ctl, why = await fresh_bar()
    try:
        if frame:
            say(await popup.click(frame, ctl["activate"], settle=6),
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
    tab, frame, ctl, why = await fresh_bar()
    try:
        if frame:
            opened = await popup.click(frame, ctl["optout"], settle=2)
            say(opened and await popup.visible(frame, ctl["optout_panel"]),
                "opt-out opens from the bar")
            say(all([await popup.visible(frame, ctl["optout_24h"]),
                     await popup.visible(frame, ctl["optout_30d"]),
                     await popup.visible(frame, ctl["optout_forever"])]),
                "offering 24 hours, 30 days and forever")

            # Press one, then prove it took: no bar on the next search, and a
            # row covering the engine for exactly the period chosen.
            if await popup.click(frame, ctl["optout_24h"], settle=4):
                returned = await bar_returns(tab)
                if returned is None:
                    print("   --  could not search again after opting out")
                else:
                    say(not returned, "and 24 hours silences the next search")
                engine = storage.entry_for(await storage.quiet_domains(context),
                                           "https://www.google.com")
                window = storage.window_ms(engine) if engine else None
                say(window is not None
                    and abs(window - 24 * 60 * MINUTE) <= 5 * MINUTE,
                    f"for {window / MINUTE / 60:.0f} hours" if window
                    else "but wrote no window for the engine")
    finally:
        await tab.close()

    # 5 — the analytics call it a keyword search
    analytics = netspy.PageCalls()
    await analytics.watch(context, "**/analytics")
    tab, frame, ctl, why = await fresh_bar()
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

        if only in (None, "popup", "detail"):
            walk = Walk(context, sites)
            await walk.open_tabs()
            try:
                if only != "detail":
                    await act_popup(walk)
                if only != "popup":
                    await act_offer_detail(walk)
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
    parser.add_argument("--only", choices=["popup", "detail", "widget", "offerbar",
                                           "notification"], default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.only, args.headless))


if __name__ == "__main__":
    sys.exit(main())
