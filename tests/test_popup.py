"""The offer popup: it appears, it closes, and the silence it writes is right.

QA_TEST_PLAN sections 1.1, 1.2, 1.6, 1.7.
"""
import time

import pytest

from bring import popup, storage

pytestmark = pytest.mark.popup

MINUTE = 60_000
# The server sets the window; 30 minutes is what it has been sending. Asserted
# as a range rather than a number, so a server that tunes it does not turn into
# a failing suite — but a window of seconds or of days still fails.
CLOSE_QUIET_MIN = 5 * MINUTE
CLOSE_QUIET_MAX = 24 * 60 * MINUTE


async def test_popup_appears(on_retailer, retailer):
    """1.1 — the popup shows up on a supported retailer, with content in it."""
    page, frame = on_retailer
    assert frame, f"no popup appeared on {retailer} within 30s"

    body = await popup.body_text(frame)
    assert body.strip(), "the popup rendered an empty frame"
    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "the popup has no activate button"


async def test_popup_has_agree_line_with_both_links(on_retailer):
    """7.2 — 'By activating you agree to Deal Terms, Terms of Use'.

    Both links, on every platform, and the Privacy link gone. This used to be
    Solflare-only, so a platform still showing the old single-Terms line is the
    regression being watched for.
    """
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

    entry = await storage.quiet_entry(context, retailer)
    assert entry, f"closing wrote no quietDomains entry for {retailer}"
    assert entry.get("phase") == "quiet", \
        f"expected phase 'quiet' after a close, got {entry.get('phase')!r}"

    window = storage.window_ms(entry)
    assert window and CLOSE_QUIET_MIN <= window <= CLOSE_QUIET_MAX, \
        f"the close silenced {retailer} for {window}ms, which is not a sane window"

    # Back on the retailer: still silent.
    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_popup(again, timeout=12) is None, \
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
    """1.7 — once the window is over, the popup comes back.

    The window is half an hour, so it is moved rather than waited out: the rows
    are pushed into the past, which is the state the extension would be in after
    the wait. The rows are left in place rather than deleted, because the SDK
    prunes them only on the next write — an expired row still sitting there is
    the real state, and deleting it would test a state the product never has.
    """
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
    """1.9 — a stale row before a live one must not shadow it.

    quietDomains is read first-match, so an expired entry sitting ahead of a
    valid one used to hide it and let the popup through on a retailer that was
    still supposed to be silent. Rows are skipped if expired on read, and pruned
    only on write.
    """
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
    shown = await popup.wait_for_popup(page, timeout=12)
    await page.close()

    assert shown is None, \
        "an expired quietDomains row hid a still-active one and the popup showed"


@pytest.mark.parametrize("mode", ["reload", "back", "forward"])
async def test_navigation_does_not_resurrect_a_closed_popup(on_retailer, context,
                                                            retailer, control, mode):
    """1.6 — reload and history navigation re-check the page, they do not bypass.

    After a close the retailer is silenced, so every one of these must stay
    quiet. The check that they re-check at all is `test_popup_appears` arriving
    fresh; this one is about not undoing a silence.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["close_x"])
    assert await popup.wait_for_gone(page, timeout=10)

    if mode == "reload":
        await page.reload(wait_until="domcontentloaded")
    else:
        await page.goto(control, wait_until="domcontentloaded")
        await page.go_back(wait_until="domcontentloaded")
        if mode == "forward":
            await page.go_forward(wait_until="domcontentloaded")
            await page.go_back(wait_until="domcontentloaded")

    assert await popup.wait_for_popup(page, timeout=12) is None, \
        f"the popup came back after {mode} on a retailer that was silenced"


async def test_only_one_popup_at_a_time(on_retailer):
    """1.12 — exactly one Bring frame, never two stacked."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    found = popup.frames(page)
    assert len(found) == 1, \
        f"{len(found)} Bring frames on the page — the popup was injected more than once"
