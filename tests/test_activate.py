"""Activation: the confirmation, the wildcard silence, and the phase that sticks.

QA_TEST_PLAN section 1.3.
"""
import time

import pytest

from bring import popup, storage

pytestmark = pytest.mark.popup

HOUR = 60 * 60 * 1000


async def test_activate_shows_the_confirmation(on_retailer, retailer):
    """1.3 — activating closes the offer and shows 'you're now earning cashback'."""
    page, frame = on_retailer
    assert frame, f"no popup appeared on {retailer}"

    assert await popup.click(frame, popup.OFFER["activate"], settle=4), \
        "the popup has no activate button to click"

    confirmed = await popup.wait_for_popup(page, timeout=30, route="activated")
    assert confirmed, "activating did not bring up the activated confirmation"

    body = await popup.body_text(confirmed)
    assert body.strip(), "the activated confirmation rendered empty"
    # The rate is injected per retailer; this rendered as 'undefinedNaN' before.
    assert "undefined" not in body and "NaN" not in body, \
        f"the confirmation shows an unresolved value: {body[:200]!r}"


async def test_activate_writes_a_wildcard_entry(on_retailer, context, retailer):
    """1.3 — the silence covers the apex, its subdomains and other paths.

    Written as `*.<domain>`, so one entry answers for all of them. A bare host
    entry would leave `m.retailer.com` and `/checkout` popping again straight
    after the user activated, which is the bug the wildcard fixed.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["activate"], settle=5)

    entry = await storage.quiet_entry(context, retailer)
    assert entry, f"activating wrote no quietDomains entry for {retailer}"
    assert str(entry.get("domain", "")).startswith("*."), \
        f"expected a wildcard entry, got domain={entry.get('domain')!r}"
    assert entry.get("phase") == "activated", \
        f"expected phase 'activated' after activating, got {entry.get('phase')!r}"


async def test_activated_confirmation_returns_on_revisit(on_retailer, context, retailer):
    """1.3 — until the user dismisses it, revisiting shows the confirmation again.

    And it comes from storage: the phase is read before the retailer list, so it
    still shows for a retailer that has since left the list.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["activate"], settle=5)
    assert await popup.wait_for_popup(page, timeout=30, route="activated"), \
        "no confirmation to revisit"

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(again, timeout=25)
    route = popup.route_of(shown) if shown else None
    await again.close()

    assert shown, "revisiting an activated retailer showed nothing at all"
    assert route == "activated", \
        f"revisiting showed the {route!r} surface; the confirmation was expected"


async def test_dismissing_the_confirmation_switches_to_quiet(on_retailer, context,
                                                             retailer):
    """1.3 — the confirmation's X restarts the timer and turns the entry quiet."""
    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["activate"], settle=5)

    confirmed = await popup.wait_for_popup(page, timeout=30, route="activated")
    assert confirmed, "no confirmation appeared"

    before = await storage.quiet_entry(context, retailer)
    assert before and before.get("phase") == "activated"

    assert await popup.click(confirmed, popup.ACTIVATED["close_x"], settle=2), \
        "the confirmation has no X to dismiss it with"

    after = await storage.quiet_entry(context, retailer)
    assert after, "dismissing the confirmation removed the entry entirely"
    assert after.get("phase") == "quiet", \
        f"after dismissing, phase should be 'quiet', got {after.get('phase')!r}"

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(again, timeout=12)
    await again.close()
    assert shown is None, \
        "the retailer popped again after the confirmation was dismissed"


async def test_activation_closes_the_popup_on_other_tabs(context, retailer):
    """1.3 — activating in one tab closes the offer everywhere it is open."""
    first = await context.new_page()
    await first.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(first, timeout=30)
    assert frame, f"no popup appeared on {retailer}"

    second = await context.new_page()
    await second.goto(retailer, wait_until="domcontentloaded")
    other = await popup.wait_for_popup(second, timeout=25)
    if other is None:
        pytest.skip("the second tab never got its own popup, so there is "
                    "nothing to prove is closed")

    assert await popup.click(frame, popup.OFFER["activate"], settle=5)

    closed = await popup.wait_for_gone(second, timeout=10, route="offer")
    await first.close()
    await second.close()
    assert closed, "activating in one tab left the offer open in another"


async def test_a_stand_down_does_not_downgrade_an_activation(context, retailer):
    """1.8 — an affiliate arrival must not overwrite our own activation.

    Seeded rather than driven through a real activation: the assertion is about
    what the stand-down does to an existing `activated` row, and building that
    row by hand is both faster and the only way to be certain it is the row
    under test.
    """
    host = storage.normalise(retailer)
    now = int(time.time() * 1000)
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": f"*.{host}", "type": "kds", "phase": "activated",
         "time": [now, now + 2 * HOUR]},
    ])

    page = await context.new_page()
    await page.goto(f"{retailer}?utm_source=affiliate&clickid=e2e-probe",
                    wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=10)
    await page.close()

    entry = await storage.quiet_entry(context, retailer)
    assert entry, "the activated entry disappeared"
    assert entry.get("phase") == "activated", \
        (f"an affiliate arrival downgraded our activation to "
         f"{entry.get('phase')!r}")
