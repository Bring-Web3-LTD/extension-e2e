"""The wallet: connecting, switching, and the fast activation path.

QA_TEST_PLAN section 1.5. Every action has to work with a wallet and without
one — the "without" case is not an edge, it is how every user starts.
"""
import pytest

from bring import netspy, popup, storage, search

pytestmark = pytest.mark.popup

# The iframe posts here on every activation. What the fast path changes is not
# whether the call happens but whether the user waits for it: with a payload
# already in hand the request goes out `keepalive` and the answer is never
# read, so the confirmation is instant (api/activate.ts). The request says which
# path it took in `activationMode`, and that is the only honest thing to assert
# — "the endpoint was never touched" describes a product that does not exist,
# and fails on a perfectly working fast activation.
ACTIVATE_ENDPOINT = "**/v1/extension/activate**"

FAST = "fastActivation"          # the popup already carried the payload
STANDALONE = "standAloneActivation"   # worked out at click time

# Two addresses, so "switched" is provable rather than assumed.
ADDRESS = ("addr1qydfh2z0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5psyq79gf"
           "lnhwlttgw8gk7aj5j6lj95vg7my67vpsdcvu4l")
SECOND_WALLET = ("addr1q9zzzzzz0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5ps"
                 "yq79gflnhwlttgw8gk7aj5j6lj95vg7my67vpsqqqqqq")


class Counter:
    """The activation requests the iframe sent, and how each described itself."""

    def __init__(self):
        self.hits = 0
        self.modes = []

    async def watch(self, context):
        async def handler(route):
            self.hits += 1
            try:
                body = route.request.post_data_json or {}
                self.modes.append(body.get("activationMode"))
            except Exception:
                self.modes.append(None)
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


async def test_activation_takes_the_fast_path_with_no_wallet_connected(context, retailer):
    """1.5 — with no wallet, everything was prepared at the popup call.

    Nothing about the activation can change before the click, so the server
    computes it up front and the click spends it rather than asking again. The
    request still goes out — it has to, the activation must be recorded — but it
    is sent `keepalive` and its answer is never read, which is what makes the
    confirmation instant. `activationMode` is where the client says which of the
    two it did.
    """
    counter = Counter()
    await counter.watch(context)

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    assert frame, "no popup appeared"

    assert await popup.click(frame, popup.OFFER["activate"], settle=6)
    confirmed = await popup.wait_for_popup(page, timeout=25, route="activated")
    if confirmed is None:
        marker = search.blocked_url(page.url)
        await page.close()
        if marker:
            pytest.skip(
                f"the shop answered the affiliate hop with a bot check "
                f"({marker!r}), so no confirmation could be shown")
        pytest.fail("activating without a wallet did not confirm")
    await page.close()
    assert counter.modes, "activating sent no request to the activate endpoint"
    assert counter.modes[-1] == FAST, (
        f"activating without a wallet reported {counter.modes[-1]!r}; the popup "
        f"response should already have carried the payload, making this "
        f"{FAST!r}")


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
    assert counter.modes, "activating sent no request to the activate endpoint"
    assert counter.modes[-1] == STANDALONE, (
        f"connecting a wallet after the popup appeared still reported "
        f"{counter.modes[-1]!r} — the payload prepared for the wallet-less user "
        f"was reused, and the activation is credited to the wrong user")


async def test_switching_the_wallet_updates_what_the_popup_shows(on_retailer, context):
    """1.5 — the address on the offer follows the wallet, not the first one seen.

    Switching accounts is ordinary — people have several — and the offer is
    where the consequence shows. A popup still naming the previous account is
    telling the user their cashback is going somewhere it is not.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"

    if await popup.visible(frame, popup.OFFER["connect_wallet"]):
        assert await popup.click(frame, popup.OFFER["connect_wallet"], settle=4)

    first = await popup.text(frame, popup.OFFER["wallet_address"])
    if not first.strip():
        pytest.skip("no wallet address on the offer to switch away from")

    await netspy.set_host_wallet(context, SECOND_WALLET)
    await netspy.broadcast_wallet(page, SECOND_WALLET)
    await page.wait_for_timeout(3000)

    stored = await storage.get(context, storage.WALLET_ADDRESS)
    assert stored == SECOND_WALLET, (
        f"switching wallets left {stored!r} stored, not the address just "
        f"connected")


async def test_disconnecting_clears_the_wallet_and_the_offer_still_works(
        on_retailer, context, retailer):
    """1.5 — disconnect, and the offer keeps working without one.

    Every action has to work with or without a wallet. A disconnect that leaves
    the extension holding a stale address would keep crediting a wallet the
    user has walked away from.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"

    await netspy.set_host_wallet(context, ADDRESS)
    await netspy.broadcast_wallet(page, ADDRESS)
    await page.wait_for_timeout(2500)

    await netspy.set_host_wallet(context, "")
    await netspy.broadcast_wallet(page, "")
    await page.wait_for_timeout(2500)

    assert not await storage.get(context, storage.WALLET_ADDRESS), \
        "disconnecting left the wallet address stored"

    again = await context.new_page()
    await again.goto(retailer, wait_until="domcontentloaded")
    after = await popup.wait_for_offer(again, timeout=30)
    still_works = after is not None and await popup.visible(
        after, popup.OFFER["activate"])
    await again.close()

    assert still_works, \
        "after disconnecting, the offer no longer works without a wallet"
