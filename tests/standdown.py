import time

from bring import pages, popup, retailers, search, storage
from bring.walk import Result
from tests.steps import HOUR, MINUTE, TOLERANCE_MS, visit_and_open

# Copied from the backend's AFFILIATE_URL_IDENTIFIERS: matched as substrings
# against every URL in the chain. One per shop on the landed URL; the fourth
# rides on a redirect hop, which is the case the chain-watching exists for.
URL_MARKERS = ("irclickid", "cjevent", "ranMID")
HOP_MARKER = "affiliate_id"
VALUE = "e2e-standdown-probe"


def marker_for(site, sites):
    return URL_MARKERS[sites.index(site) % len(URL_MARKERS)]


def with_param(url, name):
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{name}={VALUE}"


async def stood_down(walk, site, tab, how):
    """No popup, and a kdi row for the domain sized by standDownOffset."""
    shown = await popup.wait_for_popup(tab, timeout=15)
    if shown:
        return Result.failed(f"the popup appeared after arriving {how} "
                             f"(surface {popup.route_of(shown)!r})")
    blocked = search.blocked_url(tab.url)
    if blocked:
        return Result.blocked(f"bot check ({blocked!r}) on the arrival {how}")

    row = await storage.await_quiet_entry(walk.context, site)
    if not row:
        return Result.failed(f"no popup, but nothing was written either — "
                             f"arriving {how} did not stand the extension down")
    if not str(row.get("domain", "")).startswith("*."):
        return Result.failed(f"quieted {row.get('domain')!r}, not the whole domain")
    if not str(row.get("type", "")).startswith("kdi"):
        return Result.failed(f"type is {row.get('type')!r}, expected kdi")

    window = storage.window_ms(row)
    if window is None:
        return Result.failed(f"malformed range: {row.get('time')!r}")
    offset = await storage.get(walk.context, "standDownOffset")
    if isinstance(offset, (int, float)) and offset > 0:
        if abs(window - offset) > TOLERANCE_MS:
            return Result.failed(f"standDownOffset is {offset / HOUR:.2f}h, "
                                 f"stored {window / HOUR:.2f}h")
    elif not 0.5 <= window / HOUR <= 24:
        return Result.failed(f"no standDownOffset stored and the window is "
                             f"{window / HOUR:.2f}h, not the 2h default")
    return Result.passed(f"stood down {how}: kdi, {window / HOUR:.2f}h")


def make_marker_on_url(walk):
    async def check(site, tab):
        marker = marker_for(site, walk.sites)
        await tab.goto(with_param(site, marker), wait_until="domcontentloaded")
        return await stood_down(walk, site, tab, f"with {marker} on the URL")
    return check


def make_marker_on_hop(walk):
    """The marker sits on a 3xx hop through an affiliate host; the landed
    URL is clean. Only the chain gives it away.

    The entry page — the coupon site — is on a neutral origin, as it is in
    life. Served on the shop's own origin it was itself a retailer page: its
    popup check was still in flight while the redirect landed on the shop,
    and the answer injected the offer over a domain the stand-down had just
    quieted. The kdi row was there; the popup came from the page before.
    """
    async def check(site, tab):
        origin = retailers.origin(site)
        entry = f"https://example.com/e2e-coupon-entry/{storage.normalise(site)}"
        hop = with_param(pages.coupon_hop(origin, index=walk.sites.index(site)),
                         HOP_MARKER)
        await pages.redirect_through(walk.context, entry, hop, f"{origin}/")
        await tab.goto(entry, wait_until="domcontentloaded")
        if HOP_MARKER in tab.url:
            return Result.failed(f"the marker reached the landed URL ({tab.url[:80]})")
        host = hop.split("//")[1].split("/")[0]
        return await stood_down(walk, site, tab, f"via {host} + {HOP_MARKER} on the hop")
    return check


def make_whole_domain_quiet(walk):
    async def check(site, tab):
        origin = retailers.origin(site)
        bare = origin.replace("://www.", "://")
        for where, url in (("another path", f"{origin}/e2e-other-path"),
                           ("the bare apex", f"{bare}/")):
            await tab.goto(url, wait_until="domcontentloaded")
            shown = await popup.wait_for_popup(tab, timeout=popup.ABSENT)
            if shown:
                return Result.failed(f"{where} still popped after the stand-down")
        return Result.passed("another path and the bare apex stay silent")
    return check


def make_extends_never_shortens(walk):
    async def check(site, tab):
        host = f"*.{storage.normalise(site)}"
        now = int(time.time() * 1000)

        # A longer silence is kept.
        long_end = now + 30 * 24 * HOUR
        await storage.set(walk.context, storage.QUIET_DOMAINS, [
            {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
             "time": [now, long_end]}])
        await tab.goto(with_param(site, "irclickid"), wait_until="domcontentloaded")
        await popup.wait_for_popup(tab, timeout=popup.ABSENT)
        rows = storage.entries_for(await storage.quiet_domains(walk.context), site)
        if not rows:
            return Result.failed("the existing 30-day silence disappeared")
        furthest = max(r["time"][1] for r in rows if isinstance(r.get("time"), list))
        if furthest < long_end - MINUTE:
            return Result.failed(f"a 30-day silence was shortened to "
                                 f"{(furthest - now) / HOUR:.1f}h")

        # A shorter one is extended.
        short_end = now + 5 * MINUTE
        await storage.set(walk.context, storage.QUIET_DOMAINS, [
            {"domain": host, "type": "kds", "phase": "quiet", "isRegex": False,
             "time": [now, short_end]}])
        await tab.goto(with_param(site, "cjevent"), wait_until="domcontentloaded")
        await popup.wait_for_popup(tab, timeout=popup.ABSENT)
        rows = storage.entries_for(await storage.quiet_domains(walk.context), site)
        furthest = max((r["time"][1] for r in rows if isinstance(r.get("time"), list)),
                       default=0)
        if furthest < now + 30 * MINUTE:
            return Result.failed(f"a 5-minute silence was not extended "
                                 f"(ends in {(furthest - now) / MINUTE:.0f} min)")
        return Result.passed(f"30 days kept; 5 minutes extended to "
                             f"{(furthest - now) / HOUR:.1f}h")
    return check


def make_never_downgrades_activation(walk):
    async def check(site, tab):
        host = f"*.{storage.normalise(site)}"
        now = int(time.time() * 1000)
        await storage.set(walk.context, storage.QUIET_DOMAINS, [
            {"domain": host, "type": "kds", "phase": "activated", "isRegex": False,
             "time": [now, now + 2 * HOUR]}])
        await tab.goto(with_param(site, "irclickid"), wait_until="domcontentloaded")
        await popup.wait_for_popup(tab, timeout=popup.ABSENT)
        entry = await storage.await_quiet_entry(walk.context, site)
        if not entry:
            return Result.failed("the activated row disappeared")
        if entry.get("phase") != "activated":
            return Result.failed(f"an affiliate arrival downgraded the activation "
                                 f"to {entry.get('phase')!r}")
        return Result.passed("still activated")
    return check


async def act(walk):
    walk.begin("ACT 3", "stand-down, on every shop")

    await walk.each("arriving with an affiliate marker on the URL stands down",
                    make_marker_on_url(walk))
    await walk.step("another path and the bare apex are quiet too",
                    make_whole_domain_quiet(walk))
    await walk.clear()

    await walk.each("a marker only on a redirect hop stands down",
                    make_marker_on_hop(walk))
    await walk.clear()

    await walk.one("a stand-down extends a silence and never shortens it",
                   make_extends_never_shortens(walk))
    await walk.clear()
    await walk.one("a stand-down never downgrades an activation",
                   make_never_downgrades_activation(walk))
    await walk.clear()

    await walk.step("a clean arrival still pops", visit_and_open)
    await walk.clear()
    try:
        await walk.context.unroute_all(behavior="ignoreErrors")
    except Exception:
        pass
