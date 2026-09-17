import asyncio
import os
import re
import time
from urllib.parse import quote_plus, unquote

from bring import netspy, pages, popup, storage
from bring.walk import Result
from tests.steps import CLOSE_QUIET_MS, MINUTE, TOLERANCE_MS

KEYWORD = os.getenv("BRING_OFFERBAR_KEYWORDS", "c ondor").split(",")[0].strip()
#: A shop the environment carries as an inline-search retailer (type 's').
LINK = os.getenv("BRING_OFFERBAR_LINK", "https://www.condor.com/")
GOOGLE = "https://www.google.com"
RESULTS = f"{GOOGLE}/search?q={quote_plus(KEYWORD)}"

HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{KEYWORD} - Google Search</title></head>
<body><div id="main"><h1>Results for {KEYWORD}</h1>
<div id="search"><a href="{LINK}">{KEYWORD}</a></div></div></body></html>"""

_visits = 0


class NoBar(Exception):
    def __init__(self, result):
        self.result = result


async def results(tab):
    """Open a fresh copy of the results page."""
    global _visits
    _visits += 1
    await tab.goto(f"{RESULTS}&e2e={_visits}", wait_until="domcontentloaded")


async def open_bar(walk, tab):
    """The bar and its control map, or raise NoBar with the reason."""
    await netspy.install(walk.context, netspy.PASS)
    await netspy.reset(walk.context)
    await results(tab)
    frame = await popup.wait_for_bar(tab, timeout=60)
    if frame:
        return frame, await popup.controls_for(frame)
    try:
        decided = await netspy.server_said_offerbar(walk.context)
    except netspy.SpyGone:
        raise NoBar(Result.blocked(
            "no bar, and the request recorder was lost to a worker restart, so "
            "whether the server was asked is unknown"))
    if decided is None:
        raise NoBar(Result.failed(
            f"the results page linking to {LINK} never produced a popup check — "
            f"the inline search did not match the link"))
    if decided is False:
        raise NoBar(Result.failed(
            f"the server was asked about {LINK} from a search page and did not "
            f"ask for a bar"))
    raise NoBar(Result.failed("the server asked for a bar and none appeared"))


def guarded(work):
    """Turn a NoBar into the step's result."""
    async def run(site, tab):
        try:
            return await work(site, tab)
        except NoBar as e:
            return e.result
    return run


async def bar_returns(tab):
    """Whether a fresh results page brings the bar back."""
    await results(tab)
    return await popup.wait_for_bar(tab, timeout=12) is not None


def retailer_row(rows):
    for row in rows:
        if isinstance(row, dict) and "google" not in str(row.get("domain", "")):
            return row
    return None


async def await_retailer_row(context, timeout=20):
    deadline = time.time() + timeout
    while True:
        shop = retailer_row(await storage.quiet_domains(context))
        if shop or time.time() >= deadline:
            return shop
        await asyncio.sleep(0.25)


def make_appears(walk):
    async def check(site, tab):
        frame, ctl = await open_bar(walk, tab)
        layout = "top bar" if ctl is popup.TOPBAR else "offer bar"
        if not await popup.visible(frame, ctl["activate"]):
            return Result.failed(f"the {layout} has no activate button")
        if await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed("the bar offers wallet-connect, which belongs to the popup")
        if len(popup.bars(tab)) != 1:
            return Result.failed(f"{len(popup.bars(tab))} bars on one page")
        reserved = await tab.evaluate(popup.BODY_RESERVATION)
        if ctl is popup.TOPBAR and not reserved["transform"]:
            return Result.failed("the top bar covers the results instead of pushing them")
        if ctl is popup.OFFERBAR and not await popup.visible(frame, ctl["spacer"]):
            return Result.failed("the offer bar reserves no space for itself")
        if not await popup.visible(frame, ctl["optout"]):
            return Result.failed("opt-out is not reachable from the bar")
        body = await popup.body_text(frame)
        if "undefined" in body or "NaN" in body:
            return Result.failed(f"unresolved value in the bar: {body[:60]!r}")
        return Result.passed(f"{layout}: activate, opt-out, one bar, room made")
    return guarded(check)


def make_close_silences_engine(walk):
    async def check(site, tab):
        frame, ctl = await open_bar(walk, tab)
        closed = (await popup.click(frame, ctl["close_top"], settle=2)
                  or await popup.click(frame, ctl["close_bottom"], settle=2))
        if not closed:
            return Result.failed("the bar has no close control")
        if not await popup.bars_gone(tab, timeout=20):
            return Result.failed("the bar is still on the page")
        back = await tab.evaluate(popup.BODY_RESERVATION)
        if back["transform"]:
            return Result.failed(f"the page stayed pushed down ({back['transform']})")
        # Closing a bar quiets the engine it was shown on: the bar belongs
        # to the search, not to the shop.
        engine = await storage.await_quiet_entry(walk.context, GOOGLE)
        if not engine:
            return Result.failed("nothing was silenced, so the bar returns next search")
        window = storage.window_ms(engine)
        if window is None or abs(window - CLOSE_QUIET_MS) > TOLERANCE_MS:
            return Result.failed(f"the engine is quiet for "
                                 f"{window and window / MINUTE:.0f} minutes, not thirty")
        if await bar_returns(tab):
            return Result.failed("the bar came back on the very next search")
        return Result.passed(f"closed, space given back, {engine.get('domain')!r} "
                             f"quiet for {window / MINUTE:.0f} minutes, no bar on the next search")
    return guarded(check)


def make_activate_silences_shop(walk):
    async def check(site, tab):
        frame, ctl = await open_bar(walk, tab)
        if not await popup.click(frame, ctl["activate"], settle=6):
            return Result.failed("the bar has no activate button")
        shop = await await_retailer_row(walk.context)
        if not shop:
            return Result.failed("activating from the bar wrote no shop row")
        if shop.get("phase") != "activated":
            return Result.failed(f"the shop row says {shop.get('phase')!r}")
        if not str(shop.get("domain", "")).startswith("*."):
            return Result.failed(f"quieted {shop.get('domain')!r}, not the whole domain")
        rows = await storage.quiet_domains(walk.context)
        if storage.entry_for(rows, GOOGLE):
            return Result.failed("Google itself was silenced by the activation")

        # The activated shop shows the confirmation until it is closed.
        host = str(shop["domain"]).lstrip("*.")
        visit = await walk.context.new_page()
        try:
            await visit.goto(f"https://{host}", wait_until="domcontentloaded")
            confirmation = await popup.wait_for_popup(visit, timeout=25, route="activated")
            if confirmation is None:
                if popup.frames(visit, "offer"):
                    return Result.failed(f"{host} showed a fresh offer, not the "
                                         f"activated confirmation")
                return Result.passed(f"*.{host} activated, Google untouched "
                                     f"(-- {host} showed nothing on the visit)")
            try:
                await popup.click(confirmation, popup.ACTIVATED["close_x"], settle=3)
            except Exception as e:
                if "detached" not in str(e).lower():
                    raise
        finally:
            await visit.close()
        quiet = await storage.await_quiet_phase(walk.context, f"https://{host}", "quiet")
        if not quiet:
            return Result.failed("closing the confirmation did not turn the row quiet")
        return Result.passed(f"*.{host} activated, Google untouched, "
                             f"confirmation shown then closed to quiet")
    return guarded(check)


def make_optout_from_bar(walk):
    async def check(site, tab):
        frame, ctl = await open_bar(walk, tab)
        if not await popup.click(frame, ctl["optout"], settle=2):
            return Result.failed("the bar has no opt-out")
        if not await popup.await_visible(frame, ctl["optout_panel"]):
            return Result.failed("opt-out did not open from the bar")
        for name in ("optout_24h", "optout_30d", "optout_forever"):
            if not await popup.visible(frame, ctl[name]):
                return Result.failed(f"the bar's opt-out is missing {name}")
        if not await popup.click(frame, ctl["optout_24h"], settle=4):
            return Result.failed("could not press 24 hours")
        engine = await storage.await_quiet_entry(walk.context, GOOGLE)
        window = storage.window_ms(engine) if engine else None
        if window is None or abs(window - 24 * 60 * MINUTE) > 5 * MINUTE:
            return Result.failed(f"24 hours wrote {window and window / MINUTE / 60:.1f}h "
                                 f"for the engine")
        if await bar_returns(tab):
            return Result.failed("the bar came back on the next search after opting out")
        return Result.passed("24 hours / 30 days / forever; 24h silences the next search")
    return guarded(check)


async def keyword_registered(walk) -> bool:
    """Whether the environment's list matches the search URL itself as a
    keyword search — the SDK's own test (`getRelevantDomain(url, 'kd')`:
    reversed host + path against the patterns typed 'k' / 'kd'), done here.
    """
    listed = await storage.get(walk.context, "relevantDomains") or []
    types = await storage.get(walk.context, "domainsTypes") or []
    # Raw storage holds {flags, regexes}; the SDK's cache hands back the
    # compiled list, serialised as [{"r": {"p": source, "f": flags}}, ...].
    patterns = (listed.get("regexes", []) if isinstance(listed, dict)
                else [e.get("r", {}).get("p", "") if isinstance(e, dict) else str(e)
                      for e in listed])
    host, _, path = RESULTS.split("://", 1)[1].replace("www.", "", 1).partition("/")
    probe = host[::-1] + "/" + unquote(path.replace("+", " ")) + "&e2e=1"
    for kind, pattern in zip(types, patterns):
        if pattern and set(kind) & set("kd") and re.match(pattern, probe, re.I):
            return True
    return False


def make_trigger_type_reported(walk, analytics):
    """7.3 / 2 — the analytics carry the server's triggerType.

    The server labels a bar `keyword` only when the search URL itself matched
    a registered search keyword (`obData.ts`, type 'k' / 'kd'); a bar reached
    through a retailer link on the results page — the inline search — is
    labelled `domain`. Which one applies is decided by the environment's
    retailer list, so the walk reads that list the way the SDK does. Measured
    both ways: one environment had no keyword patterns at all (`domain`), a
    fresh deploy of the same branch carried six (`keyword`).
    """
    async def check(site, tab):
        expected = "keyword" if await keyword_registered(walk) else "domain"
        analytics.seen.clear()
        await open_bar(walk, tab)
        await tab.wait_for_timeout(2500)
        triggers = {e.get("triggerType") for e in analytics.events()
                    if isinstance(e, dict) and e.get("triggerType")}
        if not triggers:
            return Result.failed("no analytics event carried a triggerType")
        if triggers != {expected}:
            return Result.failed(f"reported as triggerType {triggers}; the list "
                                 f"{'matches' if expected == 'keyword' else 'does not match'} "
                                 f"the search URL as a keyword, so {expected!r} was owed")
        why = ("the search URL matches a registered keyword" if expected == "keyword"
               else "no keyword pattern matches; the bar came through the retailer link")
        return Result.passed(f"triggerType {expected} — {why}")
    return guarded(check)


def make_stood_down_gets_no_bar(walk):
    async def check(site, tab):
        domain = f"*.{storage.normalise(LINK)}"
        now = int(time.time() * 1000)
        await storage.set(walk.context, storage.QUIET_DOMAINS, [
            {"domain": domain, "type": "kdi", "phase": "quiet", "isRegex": False,
             "time": [now, now + 2 * 60 * MINUTE]}])
        await results(tab)
        if await popup.wait_for_bar(tab, timeout=popup.ABSENT):
            return Result.failed(f"a bar appeared for {LINK} while {domain!r} is stood down")
        return Result.passed(f"no bar while {domain!r} is stood down")
    return check


async def act(walk):
    walk.begin("ACT 7", f"the offer bar, on a results page for {KEYWORD!r} linking to {LINK}")
    await pages.serve(walk.context, re.compile("^" + re.escape(RESULTS) + r"&e2e=\d+$"), HTML)
    analytics = netspy.PageCalls()
    await analytics.watch(walk.context, "**/analytics")
    steps = [
        ("the bar appears over the results", make_appears(walk)),
        ("closing the bar silences the engine and gives the space back",
         make_close_silences_engine(walk)),
        ("activating from the bar silences the shop, not Google",
         make_activate_silences_shop(walk)),
        ("opt-out from the bar", make_optout_from_bar(walk)),
        ("the analytics carry the server's trigger type", make_trigger_type_reported(walk, analytics)),
        ("a stood-down shop gets no bar", make_stood_down_gets_no_bar(walk)),
    ]
    try:
        for title, work in steps:
            await walk.one(title, work, site=GOOGLE)
            await walk.clear("clearing after the search")
    finally:
        try:
            await walk.context.unroute_all(behavior="ignoreErrors")
        except Exception:
            pass
