"""The wallet: connecting, switching, and the fast activation path.

QA_TEST_PLAN section 1.5. Every action has to work with a wallet and without
one — the "without" case is not an edge, it is how every user starts.
"""
import pytest

from bring import popup

pytestmark = pytest.mark.popup

# The iframe calls this only when it has to work the activation out at click
# time. When the popup already arrived with a payload, activating is local and
# this endpoint is never touched — which is the whole point of the fast path.
ACTIVATE_ENDPOINT = "**/v1/extension/activate**"


class Counter:
    """How many times the iframe asked the server to activate."""

    def __init__(self):
        self.hits = 0

    async def watch(self, context):
        async def handler(route):
            self.hits += 1
            await route.continue_()

        await context.route(ACTIVATE_ENDPOINT, handler)


async def test_popup_works_with_no_wallet_connected(on_retailer):
    """1.5 — with no wallet, the offer still shows and offers to connect."""
    page, frame = on_retailer
    assert frame, "no popup appeared without a wallet connected"

    assert await popup.visible(frame, popup.OFFER["activate"]), \
        "the offer has no activate button when there is no wallet"
    assert await popup.visible(frame, popup.OFFER["connect_wallet"]), \
        "the offer does not offer to connect a wallet when none is connected"


async def test_connecting_a_wallet_shows_its_address(on_retailer):
    """1.5 — after connecting, the popup shows the address instead of the prompt."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    if not await popup.visible(frame, popup.OFFER["connect_wallet"]):
        pytest.skip("this run already started with a wallet connected")

    assert await popup.click(frame, popup.OFFER["connect_wallet"], settle=4)

    shown = await popup.text(frame, popup.OFFER["wallet_address"])
    assert shown.strip(), \
        "connecting a wallet did not put an address in the popup"


async def test_activation_is_local_when_no_wallet_is_connected(context, retailer):
    """1.5 — with no wallet, everything was prepared at the popup call.

    The server can compute the activation up front because nothing about it is
    going to change, so clicking activate must not go back to it.
    """
    counter = Counter()
    await counter.watch(context)

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["activate"], settle=6)
    confirmed = await popup.wait_for_popup(page, timeout=25, route="activated")
    await page.close()

    assert confirmed, "activating without a wallet did not confirm"
    assert counter.hits == 0, (
        f"activating without a wallet called the activate endpoint "
        f"{counter.hits} time(s); the payload should already have been in the "
        f"popup response")


async def test_connecting_after_the_popup_forces_a_fresh_activation(context, retailer):
    """1.5 — a wallet that arrives after the popup makes the payload stale.

    The payload was computed for whoever the user was when the popup was built.
    Connecting a wallet changes that, so the client has to ask again — using the
    stale one would credit the activation to the wrong user.
    """
    counter = Counter()
    await counter.watch(context)

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    assert frame, "no popup appeared"

    if not await popup.visible(frame, popup.OFFER["connect_wallet"]):
        await page.close()
        pytest.skip("the popup already had a wallet, so there is nothing to change")

    assert await popup.click(frame, popup.OFFER["connect_wallet"], settle=4)
    assert await popup.click(frame, popup.OFFER["activate"], settle=8)
    confirmed = await popup.wait_for_popup(page, timeout=25, route="activated")
    await page.close()

    assert confirmed, "activating after connecting a wallet did not confirm"
    assert counter.hits >= 1, (
        "connecting a wallet after the popup appeared did not force a fresh "
        "activation — the payload prepared for the wallet-less user was reused")
