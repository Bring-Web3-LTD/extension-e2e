import time
import pytest
from bring import netspy, popup, storage, search
pytestmark = pytest.mark.popup

async def confirmed_on(page, what: str = "activating"):
    confirmed = await popup.wait_for_confirmation(page)
    if confirmed:
        return confirmed

    marker = search.blocked_url(page.url)
    if marker:
        pytest.skip(
            f"{what} sent the tab through the affiliate network and the shop "
            f"answered with a bot check ({marker!r}); the confirmation cannot "
            f"be shown on that page")
    return None


HOUR = 60 * 60 * 1000
MINUTE = 60 * 1000

# What activating is specified to buy: two hours of quiet on that retailer.
ACTIVATE_QUIET_MS = 2 * HOUR
QUIET_TOLERANCE_MS = 2 * MINUTE


async def test_activate_shows_the_confirmation(on_retailer, retailer):
    """1.3 — activating closes the offer and shows 'you're now earning cashback'."""
    page, frame = on_retailer
    assert frame, f"no popup appeared on {retailer}"

    assert await popup.click(frame, popup.OFFER["activate"], settle=4), \
        "the popup has no activate button to click"

    confirmed = await confirmed_on(page)
    assert confirmed, "activating did not bring up the activated confirmation"

    body = await popup.body_text(confirmed)
    assert body.strip(), "the activated confirmation rendered empty"
    # The rate is injected per retailer; this rendered as 'undefinedNaN' before.
    assert "undefined" not in body and "NaN" not in body, \
        f"the confirmation shows an unresolved value: {body[:200]!r}"


async def test_activate_writes_a_wildcard_entry(on_retailer, context, retailer):
    page, frame = on_retailer
    assert frame, "no popup appeared"
    assert await popup.click(frame, popup.OFFER["activate"], settle=5)

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry, f"activating wrote no quietDomains entry for {retailer}"
    assert str(entry.get("domain", "")).startswith("*."), \
        f"expected a wildcard entry, got domain={entry.get('domain')!r}"
    assert entry.get("phase") == "activated", \
        f"expected phase 'activated' after activating, got {entry.get('phase')!r}"
    assert str(entry.get("type", "")).startswith("kds"), \
        f"an activation is a user action, type 'kds'; got {entry.get('type')!r}"

    # Checked the way it is checked by hand — (end - start) / 1000 / 60 / 60 —
    # against the two hours an activation is specified to buy, and then against
    # what the server actually sent on this check. The first catches a wrong
    # window; the second catches the client mangling a correct one, which the
    # first would miss whenever the server happens to send two hours anyway.
    window = storage.window_ms(entry)
    assert window is not None, f"the entry has a malformed time range: {entry!r}"

    served = await netspy.server_quiet_ms(context)
    assert abs(window - ACTIVATE_QUIET_MS) <= QUIET_TOLERANCE_MS, (
        f"activating should silence {retailer} for "
        f"{ACTIVATE_QUIET_MS / HOUR:.0f}h; it stored {window / HOUR:.2f}h"
        + (f" (the server sent {served / HOUR:.2f}h)" if served else ""))

    if served:
        assert abs(window - served) < 2000, (
            f"the server sent {served / HOUR:.2f}h but the extension stored "
            f"{window / HOUR:.2f}h")


async def test_activated_confirmation_returns_on_revisit(on_retailer, context, retailer):
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

    confirmed = await confirmed_on(page)
    assert confirmed, "no confirmation appeared"

    before = await storage.await_quiet_entry(context, retailer)
    assert before and before.get("phase") == "activated"

    assert await popup.click(confirmed, popup.ACTIVATED["close_x"], settle=2), \
        "the confirmation has no X to dismiss it with"

    after = await storage.await_quiet_entry(context, retailer)
    assert after, "dismissing the confirmation removed the entry entirely"
    assert after.get("phase") == "quiet", \
        f"after dismissing, phase should be 'quiet', got {after.get('phase')!r}"

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(again, timeout=popup.ABSENT)
    await again.close()
    assert shown is None, \
        "the retailer popped again after the confirmation was dismissed"


async def test_activation_leaves_other_tabs_alone(context, retailer):
    first = await context.new_page()
    await first.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_offer(first, timeout=30)
    assert frame, f"no popup appeared on {retailer}"

    second = await context.new_page()
    await second.goto(retailer, wait_until="domcontentloaded")
    other = await popup.wait_for_popup(second, timeout=25)
    if other is None:
        await first.close()
        await second.close()
        pytest.skip("the second tab never got its own popup, so there is "
                    "nothing to say about what activation does to it")

    assert await popup.click(frame, popup.OFFER["activate"], settle=6)

    still_there = bool(popup.frames(second))
    await first.close()
    await second.close()
    assert still_there, (
        "activating in one tab closed the popup in another. That is what the "
        "QA plan asks for, so this may be the product catching up with it — "
        "but it is a behaviour change and the test should be revisited, not "
        "just flipped")


async def test_a_stand_down_does_not_downgrade_an_activation(context, retailer):
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

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry, "the activated entry disappeared"
    assert entry.get("phase") == "activated", \
        (f"an affiliate arrival downgraded our activation to "
         f"{entry.get('phase')!r}")


async def test_the_silence_is_what_keeps_it_quiet(on_retailer, context, retailer):
    page, frame = on_retailer
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["activate"], settle=5)
    confirmed = await confirmed_on(page)
    assert confirmed, "activating did not bring up the confirmation"

    assert await popup.click(confirmed, popup.ACTIVATED["close_x"], settle=2), \
        "the confirmation has no X"

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry and entry.get("phase") == "quiet", \
        f"dismissing should leave phase 'quiet', got {entry and entry.get('phase')!r}"

    silent = await context.new_page()
    await silent.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(silent, timeout=popup.ABSENT)
    await silent.close()
    assert shown is None, "the retailer popped again while it was still silenced"

    assert await storage.forget(context, retailer), \
        "there was no row to remove, so the silence came from somewhere else"

    back = await context.new_page()
    await back.goto(retailer, wait_until="domcontentloaded")
    returned = await popup.wait_for_offer(back, timeout=30)
    await back.close()
    assert returned, (
        f"the silence was removed but {retailer} still shows nothing — the quiet "
        f"row was not what was keeping it silent")


async def test_the_confirmation_comes_back_without_asking_the_server(
        on_retailer, context, retailer):
    page, frame = on_retailer
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["activate"], settle=5)
    assert await confirmed_on(page), "no confirmation to re-show"

    entry = await storage.await_quiet_entry(context, retailer)
    assert entry and entry.get("phase") == "activated", \
        "the activation did not register, so there is nothing to re-show"
    assert (entry.get("payload") or {}).get("iframeUrl"), (
        f"the activated row carries no payload to rebuild the confirmation "
        f"from: {entry!r}")

    await netspy.install(context, netspy.PASS)
    await netspy.reset(context)

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(again, timeout=25)
    route = popup.route_of(shown) if shown else None
    checks = await netspy.count(context, netspy.POPUP_CHECK)
    await again.close()

    assert shown and route == "activated", \
        f"revisiting showed {route!r} instead of the confirmation"
    assert checks == 0, (
        f"revisiting an activated retailer made {checks} popup check(s); the "
        f"confirmation is supposed to come from the stored payload")
