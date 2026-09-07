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

    if await storage.get(context, storage.OPT_OUT):
        return "the user is opted out of every website"
    entry = await storage.await_quiet_entry(context, url)
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
    reason = await already_silent(context, retailer)
    if reason:
        pytest.skip(f"cannot tell silence from a stand-down — {reason}")

    origin = retailers.origin(retailer)
    entry = f"{origin}/e2e-entry"
    hop = f"{origin}/e2e-hop?irclickid={MARKER_VALUE}"
    # The shop's own page, not one of ours: a redirect's target cannot be
    # intercepted, and landing on the real retailer is the case anyway.
    final = f"{origin}/"

    await pages.redirect_through(context, entry, hop, final)

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

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry, "a stand-down wrote no quietDomains entry"
    assert str(entry.get("domain", "")).startswith("*."), \
        f"a stand-down should quiet `*.<domain>`, got {entry.get('domain')!r}"
    assert str(entry.get("type", "")).startswith("kdi"), \
        f"a stand-down entry should be type 'kdi', got {entry.get('type')!r}"

    # The window is `now + standDownOffset`, and the offset is downloaded with
    # the retailer list — so it can be read back and compared exactly, with no
    # number written here to go stale when the server raises it again. It was
    # raised from one hour to two once already.
    window = storage.window_ms(entry)
    assert window is not None, f"the stand-down wrote a malformed range: {entry!r}"
    offset = await storage.get(context, "standDownOffset")
    if isinstance(offset, (int, float)) and offset > 0:
        assert abs(window - offset) < 2000, (
            f"standDownOffset is {offset / HOUR:.2f}h but the stand-down "
            f"silenced {retailer} for {window / HOUR:.2f}h")
    else:
        # No offset downloaded: the SDK falls back to its own default of 2h.
        assert 0.5 <= window / HOUR <= 24, (
            f"no standDownOffset was stored and the stand-down window is "
            f"{window / HOUR:.2f}h, which is not the 2h default either")

    # A different path on the same retailer is covered by the same entry.
    other = await context.new_page()
    await other.goto(f"{retailers.origin(retailer)}/", wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(other, timeout=popup.ABSENT)
    await other.close()
    assert shown is None, \
        "another path on the stood-down retailer still popped"


async def test_stand_down_extends_but_never_shortens(context, retailer):
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
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=30)
    await page.close()
    assert shown, \
        f"no popup on a clean arrival at {retailer} — the other stand-down " \
        f"assertions in this file prove nothing"
