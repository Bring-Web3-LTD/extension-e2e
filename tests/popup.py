import time
from urllib.parse import urlsplit

from bring import netspy, popup, retailers, storage
from bring.walk import Result
from tests.steps import (ACTIVATE_QUIET_MS, CLOSE_QUIET_MS, HOUR, MINUTE,
                         TOLERANCE_MS, activate, close_popup, offer_frame,
                         revisit_expecting, unresolved, visit_and_open,
                         window_check)

NEUTRAL = "https://example.com/"


# ── the offer as rendered ───────────────────────────────────────────

async def check_text(site, tab):
    """7.7 — the server's text, with every value filled in; 7.5 — a name."""
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    name = (await popup.text(frame, "#retailer-logo-text")).strip()
    if not name:
        return Result.failed("no retailer name on the offer")
    body = await popup.body_text(frame)
    bad = unresolved(body)
    if bad:
        return Result.failed(f"{bad!r} left in the offer text: {body[:60]!r}")
    return Result.passed(f"named {name!r}, text complete")


async def check_agree_line(site, tab):
    """7.2 — Deal Terms and Terms of Use, and the Privacy link stays gone."""
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    if not await popup.visible(frame, popup.OFFER["terms_link"]):
        return Result.failed("no 'Deal Terms' link")
    if not await popup.visible(frame, popup.OFFER["tou_link"]):
        return Result.failed("no 'Terms of Use' link")
    line = await popup.text(frame, popup.OFFER["agree_text"])
    if "privacy" in line.lower():
        return Result.failed(f"the Privacy link is back: {line[:60]!r}")
    return Result.passed("Deal Terms + Terms of Use, no Privacy")


async def check_token_in_fragment(site, tab):
    """1.10 — the token rides in `#token=`, never in the query string."""
    src = await popup.iframe_src(tab)
    if not src:
        return Result.failed("no iframe src to inspect")
    parts = urlsplit(src)
    if "token=" in parts.query:
        return Result.failed("the token is in the query string")
    if "token=" not in parts.fragment:
        return Result.failed(f"no token in the fragment: #{parts.fragment[:40]!r}")
    return Result.passed("token in the fragment only")


async def check_one_popup_only(site, tab):
    """1.12 — one offer per page, however many frames the shop carries."""
    frames = [f for f in popup.frames(tab) if await popup.rendered(f)]
    if not frames:
        return Result.failed("no popup at all")
    if len(frames) > 1:
        routes = [popup.route_of(f) for f in frames]
        return Result.failed(f"{len(frames)} popups on one page: {routes}")
    return Result.passed("exactly one")


async def check_deal_terms(site, tab):
    """7.2 — the terms open over the offer, with the rate, and Back returns."""
    frame = offer_frame(tab)
    if not frame:
        return Result.failed("the popup is gone")
    if not await popup.click(frame, popup.OFFER["terms_link"], settle=1.5):
        return Result.failed("could not click 'Deal Terms'")
    if not await popup.await_visible(frame, popup.OFFER["terms_box"]):
        return Result.failed("the terms view did not open")
    # The markdown is fetched after the view opens; read it once it has text.
    body = await popup.await_nonempty_text(frame, popup.OFFER["terms_box"])
    if not body.strip():
        return Result.failed("the terms view opened empty")
    bad = unresolved(body)
    if bad:
        return Result.failed(f"{bad!r} in the terms: {body[:60]!r}")
    if not await popup.click(frame, popup.OFFER["terms_back"], settle=1.5):
        return Result.failed("the terms view has no Back button")
    if not await popup.await_visible(frame, popup.OFFER["activate"]):
        return Result.failed("Back did not return to the offer")
    return Result.passed("terms open and Back returns")


# ── activation ──────────────────────────────────────────────────────

def make_check_activated_row(walk):
    async def check(site, tab):
        entry = await storage.await_quiet_entry(walk.context, site)
        if not entry:
            return Result.failed("nothing written to quietDomains")
        if not str(entry.get("domain", "")).startswith("*."):
            return Result.failed(f"not a wildcard: {entry.get('domain')!r}")
        if entry.get("phase") != "activated":
            return Result.failed(f"phase is {entry.get('phase')!r}")
        if not str(entry.get("type", "")).startswith("kds"):
            return Result.failed(f"type is {entry.get('type')!r}, expected kds")
        bad = window_check(entry, ACTIVATE_QUIET_MS, "activate")
        if bad:
            return bad
        if not (entry.get("payload") or {}).get("iframeUrl"):
            return Result.failed("the row carries no payload to re-show from")
        return Result.passed(f"*.{storage.normalise(site)} activated, "
                             f"{storage.window_ms(entry) / HOUR:.2f}h, with payload")
    return check


def make_revisit_from_storage(walk):
    """1.3 — the confirmation is re-shown from the row, with no server call."""
    async def check(site, tab):
        await netspy.install(walk.context, netspy.PASS)
        await netspy.reset(walk.context)
        result = await revisit_expecting(walk.context, site, "activated",
                                         "confirmation")
        if not result.ok:
            return result
        try:
            checks = await netspy.count(walk.context, netspy.POPUP_CHECK)
        except netspy.SpyGone:
            return Result.passed("confirmation -- (recorder lost to a worker "
                                 "restart, so the call count is unknown)")
        if checks:
            return Result.failed(f"confirmation shown, but {checks} popup "
                                 f"check(s) went out — it should come from storage")
        return Result.passed("confirmation, no server call")
    return check


def make_dismiss_confirmation(walk):
    """1.3 — X on the confirmation: phase turns quiet, the timer restarts."""
    async def check(site, tab):
        frames = popup.frames(tab, route="activated")
        if not frames:
            return Result.failed("no confirmation on this tab to dismiss")
        if not await popup.click(frames[0], popup.ACTIVATED["close_x"], settle=2):
            return Result.failed("the confirmation has no X")
        after = await storage.await_quiet_phase(walk.context, site, "quiet")
        if not after:
            return Result.failed("dismissing did not turn the row quiet")
        start = (after.get("time") or [0])[0]
        drift = abs(time.time() * 1000 - start)
        if drift > TOLERANCE_MS:
            return Result.failed(f"the timer did not restart: starts "
                                 f"{drift / MINUTE:.0f} min from now")
        return Result.passed("quiet, timer restarted")
    return check


# ── closing ─────────────────────────────────────────────────────────

def make_check_closed_rows(walk):
    """1.2 — thirty minutes of quiet, one row per shop, none lost."""
    async def check(site, tab):
        # Three closes in the same instant, three rows landing one after
        # another: wait for all of them before judging any.
        await storage.await_rows(walk.context, walk.sites)
        rows = await storage.quiet_domains(walk.context)
        entry = storage.entry_for(rows, site)
        if not entry:
            return Result.failed("closing wrote no row for this shop")
        if entry.get("phase") != "quiet":
            return Result.failed(f"phase is {entry.get('phase')!r}")
        bad = window_check(entry, CLOSE_QUIET_MS, "close")
        if bad:
            return bad
        missing = [retailers.label(s) for s in walk.sites
                   if not storage.entry_for(rows, s)]
        if missing:
            return Result.failed(f"but {', '.join(missing)} lost their row")
        return Result.passed(f"quiet for {storage.window_ms(entry) / MINUTE:.0f} "
                             f"minutes, the others kept theirs")
    return check


async def _navigate(tab, mode):
    """Reload, or leave for a neutral page and come back (and forward)."""
    if mode == "reload":
        await tab.reload(wait_until="domcontentloaded")
        return
    # A neutral page, not another shop: leaving for a retailer has *its*
    # popup check in flight while we come back, and the previous shop's
    # popup then lands on this page. Measured, and not a product bug.
    await tab.goto(NEUTRAL, wait_until="domcontentloaded")
    await tab.go_back(wait_until="domcontentloaded")
    if mode == "forward":
        await tab.go_forward(wait_until="domcontentloaded")
        await tab.go_back(wait_until="domcontentloaded")


def make_navigate_silent(mode):
    async def check(site, tab):
        await _navigate(tab, mode)
        if await popup.wait_for_popup(tab, timeout=12):
            return Result.failed(f"after {mode} the offer came back on a silenced shop")
        return Result.passed(f"{mode} kept it silent")
    return check


def make_navigate_pops(mode):
    async def check(site, tab):
        await _navigate(tab, mode)
        if not await popup.wait_for_popup(tab, timeout=35):
            return Result.failed(f"after {mode} the offer did not come back")
        return Result.passed(f"{mode} re-checked the page")
    return check


# ── the silence and its end ─────────────────────────────────────────

async def expire_every_row(walk):
    rows = await storage.quiet_domains(walk.context)
    for entry in rows:
        if isinstance(entry, dict):
            entry["time"] = storage.past()
    await storage.set(walk.context, storage.QUIET_DOMAINS, rows)
    walk.note(f"moving every silence into the past ({len(rows)} row(s))")


def make_check_silence_expires(walk):
    async def check(site, tab):
        rows = await storage.quiet_domains(walk.context)
        if not storage.entries_for(rows, site):
            return Result.failed("no quiet row for this shop, expired or otherwise")
        result = await visit_and_open(site, tab)
        if not result.ok:
            return Result.failed("the window expired but the offer did not return")
        return Result.passed("the offer returns once the window ends")
    return check


def make_scoped_silence(walk, closed_site):
    """1.2/1.7 — the one shop closed stays silent; the other two still pop."""
    async def check(site, tab):
        if site == closed_site:
            return await revisit_expecting(walk.context, site, None, "silent")
        result = await visit_and_open(site, tab)
        if not result.ok:
            return Result.failed(f"silenced along with "
                                 f"{retailers.label(closed_site)}: {result.detail}")
        return Result.passed("still pops")
    return check


def make_expired_row_hides_nothing(walk):
    """1.9 — a dead row before a live one does not un-silence the shop."""
    async def check(site, tab):
        host = f"*.{storage.normalise(site)}"
        now = int(time.time() * 1000)
        await storage.set(walk.context, storage.QUIET_DOMAINS, [
            {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
             "time": [now - 4 * HOUR, now - 2 * HOUR]},          # long dead
            {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
             "time": [now, now + 30 * MINUTE]},                  # live
        ])
        await tab.goto(site, wait_until="domcontentloaded")
        if await popup.wait_for_popup(tab, timeout=12):
            return Result.failed("the expired row hid the live one and the offer showed")
        return Result.passed("the live row still silences")
    return check


# ── the act ─────────────────────────────────────────────────────────

async def act(walk):
    walk.begin("ACT 1", "the popup, on every shop")

    # 1.1 / 7.2 / 7.7 / 1.10 / 1.12 — what appears
    await walk.step("the offer appears", visit_and_open)
    await walk.step("the offer text is complete", check_text)
    await walk.step("the agree line carries both links", check_agree_line)
    await walk.step("the token rides in the fragment, not the query",
                    check_token_in_fragment)
    await walk.step("only one popup per page", check_one_popup_only)
    await walk.step("Deal Terms opens and Back returns", check_deal_terms)

    # 1.3 — activate
    await walk.each("activate", activate)
    await walk.step("quietDomains says activated, for two hours",
                    make_check_activated_row(walk))
    await walk.each("revisiting shows the confirmation again, without asking the server",
                    make_revisit_from_storage(walk))
    await walk.step("X on the confirmation turns it quiet and restarts the timer",
                    make_dismiss_confirmation(walk))
    await walk.step("revisiting stays silent",
                    lambda s, t: revisit_expecting(walk.context, s, None, "silent"))
    await walk.clear()

    # 1.2 / 1.6 / 1.7 — close, and nothing brings it back until the window ends
    await walk.step("the offer is back once the row is gone", visit_and_open)
    await walk.step("close it, in every tab at once", close_popup)
    await walk.step("quietDomains says quiet, for thirty minutes, one row each",
                    make_check_closed_rows(walk))
    # Silence checks run on all three at once: a quiet domain is never put to
    # the server, so there is no answer that could replace the list.
    await walk.step("revisiting stays silent",
                    lambda s, t: revisit_expecting(walk.context, s, None, "silent"))
    for mode in ("reload", "back", "forward"):
        await walk.step(f"{mode} does not bring a silenced offer back",
                        make_navigate_silent(mode))
    await expire_every_row(walk)
    await walk.each("the offer returns once the window ends",
                    make_check_silence_expires(walk))
    await walk.clear()

    # 1.6 — navigation re-checks an eligible shop
    await walk.step("the offer is back after clearing", visit_and_open)
    # The list is empty here, so three checks at once have nothing to lose.
    for mode in ("reload", "back", "forward"):
        await walk.step(f"{mode} re-checks and the offer returns",
                        make_navigate_pops(mode))
    await walk.clear()

    # 1.2 / 1.7 — the Close button, and a silence that names one shop
    first = walk.sites[0]
    await walk.step("the offer is back again", visit_and_open)
    await walk.one("the Close button silences like the X",
                   lambda s, t: close_popup(s, t, control="close_cancel"), site=first)
    await walk.each("the silence names this shop and no other",
                    make_scoped_silence(walk, first))
    await walk.clear()

    # 1.9 — how the list is read
    await walk.one("an expired row does not hide a live one",
                   make_expired_row_hides_nothing(walk))
    await walk.clear()
