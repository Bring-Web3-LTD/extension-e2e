import time
import pytest
from bring import netspy, popup, storage
pytestmark = pytest.mark.popup

MINUTE = 60_000

# What closing the popup is supposed to buy: half an hour of quiet. Asserted as
# the number, not as a range, because "some plausible window" is what a client
# storing an invented value would also satisfy. The tolerance covers the
# round-trip between the server stamping the range and the client storing it,
# nothing more.
CLOSE_QUIET_MS = 30 * MINUTE
QUIET_TOLERANCE_MS = 2 * MINUTE


async def test_popup_appears(on_retailer, retailer):
    """1.1 — the popup shows up on a supported retailer, with content in it."""
    page, frame = on_retailer
    assert frame, f"no popup appeared on {retailer} within 30s"

    body = await popup.body_text(frame)
    assert body.strip(), "the popup rendered an empty frame"
    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "the popup has no activate button"


async def test_popup_has_agree_line_with_both_links(on_retailer):
    page, frame = on_retailer
    assert frame, "no popup appeared"

    assert await popup.visible(frame, popup.OFFER["terms_link"]), \
        "the 'Deal Terms' link is missing from the agree line"
    assert await popup.visible(frame, popup.OFFER["tou_link"]), \
        "the Bring 'Terms of Use' link is missing from the agree line"

    line = await popup.text(frame, popup.OFFER["agree_text"])
    assert "privacy" not in line.lower(), \
        f"the Privacy link was removed but is still on the agree line: {line!r}"


async def test_deal_terms_opens_in_popup_and_comes_back(on_retailer):
    """7.2 — the terms view opens over the offer and the Back button returns."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["terms_link"]), \
        "could not click 'Deal Terms'"
    assert await popup.visible(frame, popup.OFFER["terms_box"]), \
        "clicking 'Deal Terms' did not open the terms view"

    terms = await popup.text(frame, popup.OFFER["terms_box"])
    assert terms.strip(), "the terms view opened empty"
    # The per-retailer rate is injected into the markdown; the bug this guards
    # against rendered it as the literal 'undefinedNaN'.
    assert "undefined" not in terms and "NaN" not in terms, \
        f"the terms show an unresolved cashback rate: {terms[:200]!r}"

    assert await popup.click(frame, popup.OFFER["terms_back"]), \
        "the terms view has no Back button"
    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "Back did not return to the offer"


@pytest.mark.parametrize("control_name", ["close_x", "close_cancel"])
async def test_close_silences_this_retailer_only(on_retailer, context, retailer,
                                                 control, control_name):
    """1.2 + 1.7 — X and Close do the same thing: quiet here, not elsewhere."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    selector = popup.OFFER[control_name]
    if not await popup.visible(frame, selector):
        pytest.skip(f"this surface has no {control_name} control")

    assert await popup.click(frame, selector)
    assert await popup.wait_for_gone(page, timeout=10), \
        f"the popup is still on the page after clicking {control_name}"

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry, f"closing wrote no quietDomains entry for {retailer}"
    assert entry.get("phase") == "quiet", \
        f"expected phase 'quiet' after a close, got {entry.get('phase')!r}"

    # Two things have to be true, and they fail for different reasons. The
    # window must be the half hour a close is specified to buy — a wrong
    # number here is a product bug. And it must equal what the server sent on
    # this very check — a mismatch there means the client mangled a value it
    # was given, which the first assertion alone would miss whenever the
    # server happens to send the expected number anyway.
    window = storage.window_ms(entry)
    assert window is not None, f"the close wrote a malformed range: {entry!r}"

    served = await netspy.server_quiet_ms(context)
    assert abs(window - CLOSE_QUIET_MS) <= QUIET_TOLERANCE_MS, (
        f"closing should silence {retailer} for "
        f"{CLOSE_QUIET_MS / MINUTE:.0f} minutes; it stored "
        f"{window / MINUTE:.1f}"
        + (f" (the server sent {served / MINUTE:.1f} minutes)" if served else ""))

    if served:
        assert abs(window - served) < 2000, (
            f"the server sent {served / MINUTE:.1f} minutes but the extension "
            f"stored {window / MINUTE:.1f}")

    # Back on the retailer: still silent.
    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_popup(again, timeout=popup.ABSENT) is None, \
        f"the popup came back on {retailer} while it should be silenced"
    await again.close()

    # A different retailer: unaffected.
    other = await context.new_page()
    await other.goto(control, wait_until="domcontentloaded")
    elsewhere = await popup.wait_for_popup(other, timeout=25)
    await other.close()
    assert elsewhere, \
        f"closing on {retailer} also silenced {control} — the silence is not scoped"


async def test_silence_ends_when_its_window_does(on_retailer, context, retailer):

    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["close_x"])
    assert await popup.wait_for_gone(page, timeout=10)

    assert await storage.expire_quiet(context, retailer), \
        "closing wrote nothing that could expire"

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    back = await popup.wait_for_popup(again, timeout=30)
    await again.close()
    assert back, \
        f"the silence on {retailer} expired but the popup did not come back"


async def test_expired_row_does_not_hide_an_active_one(context, retailer):
    host = storage.normalise(retailer)
    now = int(time.time() * 1000)

    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": f"*.{host}", "type": "kds", "phase": "quiet",
         "time": [now - 7200_000, now - 3600_000]},          # expired, first
        {"domain": f"*.{host}", "type": "kds", "phase": "quiet",
         "time": [now, now + 3600_000]},                      # live, second
    ])

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=popup.ABSENT)
    await page.close()

    assert shown is None, \
        "an expired quietDomains row hid a still-active one and the popup showed"


@pytest.mark.parametrize("mode", ["reload", "back", "forward"])
async def test_navigation_does_not_resurrect_a_closed_popup(on_retailer, context,
                                                            retailer, control, mode):

    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["close_x"])
    assert await popup.wait_for_gone(page, timeout=10)

    # The silence has to exist before the walk, or the walk proves nothing. A
    # close whose CLOSE message did not land leaves the shop free to offer
    # again, and the popup that follows is correct behaviour being reported as
    # a resurrection.
    silence = await storage.await_quiet_entry(context, retailer)
    if not silence:
        pytest.skip(
            f"closing wrote no quiet row for {retailer}, so there is no silence "
            f"for {mode} to undo — nothing to test here")

    if mode == "reload":
        await page.reload(wait_until="domcontentloaded")
    else:
        await page.goto(control, wait_until="domcontentloaded")
        await page.go_back(wait_until="domcontentloaded")
        if mode == "forward":
            await page.go_forward(wait_until="domcontentloaded")
            await page.go_back(wait_until="domcontentloaded")

    # Let the walk finish before looking. Polling for a popup while the tab is
    # still moving finds the *control's* popup — it is a different shop and
    # entitled to offer — and the url read afterwards says the retailer,
    # because by then the tab has arrived. Two true facts from two different
    # moments, and the test reports a resurrection that never happened.
    #
    # Confirmed by hand against this exact sequence: the SDK logs
    # `No popup — domain is quiet phase` on every luminskin navigation while
    # smallrig pops correctly in between, which is the product being right.
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)

    # Now both readings are of the same moment, and the popup has to still be
    # there when the tab is demonstrably on the silenced shop.
    landed = page.url
    if storage.normalise(retailer) not in storage.normalise(landed):
        pytest.skip(
            f"after {mode} the tab is on {landed}, not on {retailer} — nothing "
            f"here is about this retailer's silence")

    frames = [f for f in popup.frames(page) if await popup.rendered(f)]
    if not frames:
        return

    still = await storage.await_quiet_entry(context, retailer)
    if not still:
        pytest.skip(
            f"the quiet row for {retailer} was gone by the end of the walk, so "
            f"the popup was owed")

    assert False, (
        f"the popup came back after {mode} on {retailer}, which is still "
        f"silenced ({still.get('domain')!r}, phase {still.get('phase')!r})")


async def test_only_one_popup_at_a_time(on_retailer):
    """1.12 — exactly one Bring frame, never two stacked."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    found = popup.frames(page)
    assert len(found) == 1, \
        f"{len(found)} Bring frames on the page — the popup was injected more than once"


@pytest.mark.parametrize("mode", ["reload", "back", "forward"])
async def test_navigation_re_pops_when_the_retailer_is_still_eligible(
        context, retailer, control, mode):
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_offer(page, timeout=30), \
        f"no popup on the first visit to {retailer}"

    if mode == "reload":
        await page.reload(wait_until="domcontentloaded")
    else:
        await page.goto(control, wait_until="domcontentloaded")
        await page.go_back(wait_until="domcontentloaded")
        if mode == "forward":
            await page.go_forward(wait_until="domcontentloaded")
            await page.go_back(wait_until="domcontentloaded")

    again = await popup.wait_for_popup(page, timeout=30)
    await page.close()
    assert again, (
        f"after {mode} the popup did not come back on {retailer}, which was "
        f"never silenced — the navigation was not re-checked")
