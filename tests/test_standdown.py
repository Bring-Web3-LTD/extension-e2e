"""Hijack protection: stand down when another affiliate already owns the click.

QA_TEST_PLAN section 1.8. The SDK watches the whole navigation redirect chain
— every 3xx hop — as well as the URL the browser lands on, and if any of them
carries an affiliate attribution marker it stays silent and quiets the
retailer's registrable domain.

The nasty case is the middle one: the marker rides on a hop and is gone by the
time the browser stops, so a clean-looking final URL proves nothing. Playwright
answers with real 302s so `webRequest` sees genuine hops.
"""
import time

import pytest

from bring import pages, popup, retailers, storage

pytestmark = pytest.mark.popup

HOUR = 60 * 60 * 1000
# Copied from the backend's AFFILIATE_URL_IDENTIFIERS: matched as substrings
# against every URL in the chain. These are the ones that can simply be
# appended to a URL as name=value.
MARKERS = ("irclickid", "cjevent", "ranMID", "affiliate_id")
MARKER_VALUE = "e2e-standdown-probe"


async def already_silent(context, url) -> str:
    """Why this retailer would be silent anyway, or ''.

    Asked first, because a silenced retailer and a respected affiliate link
    look identical from outside — and reporting the first as the second is how
    a suite claims to test hijack protection while testing nothing.
    """
    if await storage.get(context, storage.OPT_OUT):
        return "the user is opted out of every website"
    entry = await storage.quiet_entry(context, url)
    if entry:
        return f"already in quietDomains (phase {entry.get('phase')!r})"
    return ""


@pytest.mark.parametrize("marker", MARKERS)
async def test_no_popup_on_an_affiliate_arrival(context, retailer, marker):
    """1.8 — a marker already on the URL means someone else owns the click."""
    reason = await already_silent(context, retailer)
    if reason:
        pytest.skip(f"cannot tell silence from a stand-down — {reason}")

    joiner = "&" if "?" in retailer else "?"
    page = await context.new_page()
    await page.goto(f"{retailer}{joiner}{marker}={MARKER_VALUE}",
                    wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=15)
    landed = page.url
    await page.close()

    if marker not in landed:
        pytest.skip(f"{marker} was stripped before the extension could see it "
                    f"(landed on {landed[:120]})")

    assert shown is None, \
        f"the popup appeared over an existing affiliate link ({marker})"


async def test_no_popup_when_the_marker_is_only_on_a_hop(context, retailer):
    """1.8 — the attribution is on an intermediate 3xx, not on the final URL.

    This is the case the redirect-chain watch exists for. Before it, the
    extension only looked at where the browser stopped, so an affiliate link
    that redirected to a clean URL was hijacked every time.
    """
    reason = await already_silent(context, retailer)
    if reason:
        pytest.skip(f"cannot tell silence from a stand-down — {reason}")

    origin = retailers.origin(retailer)
    entry = f"{origin}/e2e-entry"
    hop = f"{origin}/e2e-hop?irclickid={MARKER_VALUE}"
    final = f"{origin}/e2e-landed"

    await pages.redirect_through(context, entry, [hop], pages.plain("Landed"), final)

    page = await context.new_page()
    await page.goto(entry, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=15)
    landed = page.url
    await page.close()

    assert "irclickid" not in landed, \
        f"the test did not reproduce the case — the marker is on the final URL ({landed})"
    assert shown is None, \
        ("the popup appeared after arriving through an affiliate redirect hop; "
         "only the final URL was checked")


async def test_stand_down_quiets_the_whole_domain(context, retailer):
    """1.8 — the stand-down writes `*.<domain>`, so paths and subdomains follow."""
    reason = await already_silent(context, retailer)
    if reason:
        pytest.skip(f"cannot tell silence from a stand-down — {reason}")

    joiner = "&" if "?" in retailer else "?"
    page = await context.new_page()
    await page.goto(f"{retailer}{joiner}irclickid={MARKER_VALUE}",
                    wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=12)
    landed = page.url
    await page.close()

    if "irclickid" not in landed:
        pytest.skip("the marker was stripped before the extension saw it")

    entry = await storage.quiet_entry(context, retailer)
    assert entry, "a stand-down wrote no quietDomains entry"
    assert str(entry.get("domain", "")).startswith("*."), \
        f"a stand-down should quiet `*.<domain>`, got {entry.get('domain')!r}"
    assert str(entry.get("type", "")).startswith("kdi"), \
        f"a stand-down entry should be type 'kdi', got {entry.get('type')!r}"

    # A different path on the same retailer is covered by the same entry.
    other = await context.new_page()
    await other.goto(f"{retailers.origin(retailer)}/", wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(other, timeout=12)
    await other.close()
    assert shown is None, \
        "another path on the stood-down retailer still popped"


async def test_stand_down_extends_but_never_shortens(context, retailer):
    """1.8 — a longer existing silence survives a fresh stand-down.

    The stand-down window is `now + standDownOffset`, two hours by default. If
    the retailer is already quiet for longer, that must be kept: shortening it
    would let the popup back in early on a retailer somebody deliberately
    silenced for a month.
    """
    reason = await already_silent(context, retailer)
    if reason:
        pytest.skip(f"cannot tell silence from a stand-down — {reason}")

    host = storage.normalise(retailer)
    now = int(time.time() * 1000)
    long_end = now + 30 * 24 * HOUR
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": f"*.{host}", "type": "kds", "phase": "quiet",
         "time": [now, long_end]},
    ])

    joiner = "&" if "?" in retailer else "?"
    page = await context.new_page()
    await page.goto(f"{retailer}{joiner}irclickid={MARKER_VALUE}",
                    wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=12)
    await page.close()

    rows = storage.entries_for(await storage.quiet_domains(context), retailer)
    assert rows, "the existing silence disappeared after a stand-down"
    furthest = max(r["time"][1] for r in rows if isinstance(r.get("time"), list))
    assert furthest >= long_end - 60_000, (
        f"a stand-down shortened an existing 30-day silence to "
        f"{(furthest - now) // HOUR}h")


async def test_a_clean_arrival_still_pops(context, retailer):
    """1.8 — the guard must not swallow ordinary visits.

    Stated as its own test because every assertion above passes trivially on an
    extension that never pops at all.
    """
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=30)
    await page.close()
    assert shown, \
        f"no popup on a clean arrival at {retailer} — the other stand-down " \
        f"assertions in this file prove nothing"
