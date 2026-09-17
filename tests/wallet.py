from bring import netspy, popup, search, storage
from bring.walk import Result
from tests.steps import offer_frame, visit_and_open

ACTIVATE_ENDPOINT = "**/v1/extension/activate**"
FAST = "fastActivation"               # the popup already carried the payload
STANDALONE = "standAloneActivation"   # worked out at click time


class Modes:
    """How each activation request described itself."""

    def __init__(self):
        self.seen = []

    async def watch(self, context):
        async def handler(route):
            try:
                body = route.request.post_data_json or {}
                self.seen.append(body.get("activationMode"))
            except Exception:
                self.seen.append(None)
            await route.continue_()
        await context.route(ACTIVATE_ENDPOINT, handler)


async def disconnect(context):
    await netspy.set_host_wallet(context, "")
    await storage.delete(context, storage.WALLET_ADDRESS)
    await storage.delete(context, storage.LAST_CHECKED_WALLET)


async def activate_and_read_mode(tab, modes):
    frame = offer_frame(tab)
    before = len(modes.seen)
    if not await popup.click(frame, popup.OFFER["activate"], settle=6):
        return None, Result.failed("could not click activate")
    if not await popup.wait_for_confirmation(tab):
        marker = search.blocked_url(tab.url)
        if marker:
            return None, Result.blocked(f"bot check ({marker!r}) on the affiliate hop")
        return None, Result.failed("activating did not confirm")
    if len(modes.seen) <= before:
        return None, Result.failed("no request reached the activate endpoint")
    return modes.seen[-1], None


def make_no_wallet(walk):
    async def check(site, tab):
        await disconnect(walk.context)
        result = await visit_and_open(site, tab)
        if not result.ok:
            return result
        frame = offer_frame(tab)
        if not await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed("no wallet, but the offer does not offer to connect one")
        if not await storage.get(walk.context, "id"):
            return Result.failed("no bring_id stored for the wallet-less user")
        return Result.passed("offer shown with Connect, bring_id present")
    return check


def make_fast_without_wallet(walk, modes):
    async def check(site, tab):
        await disconnect(walk.context)
        result = await visit_and_open(site, tab)
        if not result.ok:
            return result
        mode, bad = await activate_and_read_mode(tab, modes)
        if bad:
            return bad
        if mode != FAST:
            return Result.failed(f"reported {mode!r}; the popup already carried "
                                 f"the payload, so this should be {FAST!r}")
        return Result.passed(FAST)
    return check


def make_fresh_after_connecting(walk, modes):
    async def check(site, tab):
        await disconnect(walk.context)
        result = await visit_and_open(site, tab)
        if not result.ok:
            return result
        frame = offer_frame(tab)
        if not await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed("the popup already had a wallet; nothing to change")
        if not await popup.click(frame, popup.OFFER["connect_wallet"], settle=4):
            return Result.failed("could not click Connect")
        shown = (await popup.text(frame, popup.OFFER["wallet_address"])).strip()
        if not shown:
            return Result.failed("connecting put no address in the popup")
        mode, bad = await activate_and_read_mode(tab, modes)
        if bad:
            return bad
        if mode != STANDALONE:
            return Result.failed(f"connecting after the popup still reported "
                                 f"{mode!r} — the wallet-less payload was reused")
        return Result.passed(f"connected as {shown}, then {STANDALONE}")
    return check


def make_fast_with_wallet(walk, modes):
    """Connected before the popup: the address shows, and activation is fast."""
    async def check(site, tab):
        result = await visit_and_open(site, tab)
        if not result.ok:
            return result
        frame = offer_frame(tab)
        if await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed("asked to connect again after connecting a moment ago")
        shown = (await popup.text(frame, popup.OFFER["wallet_address"])).strip()
        if not shown:
            return Result.failed("connected, but the popup shows no address")
        mode, bad = await activate_and_read_mode(tab, modes)
        if bad:
            return bad
        if mode != FAST:
            return Result.failed(f"reported {mode!r} with the wallet connected "
                                 f"before the popup; expected {FAST!r}")
        return Result.passed(f"shows {shown}, {FAST}")
    return check


def make_stays_connected(walk, connected_on):
    async def check(site, tab):
        result = await visit_and_open(site, tab)
        if not result.ok:
            return result
        frame = offer_frame(tab)
        if await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed(f"asked to connect again, after connecting on "
                                 f"{connected_on}")
        shown = (await popup.text(frame, popup.OFFER["wallet_address"])).strip()
        if not shown:
            return Result.failed("no address on the offer")
        return Result.passed(f"still {shown}")
    return check


def make_disconnect(walk):
    async def check(site, tab):
        await tab.goto("https://example.com/", wait_until="domcontentloaded")
        await netspy.set_host_wallet(walk.context, "")
        await netspy.broadcast_wallet(tab, "")
        deadline = 20
        while await storage.get(walk.context, storage.WALLET_ADDRESS) and deadline > 0:
            await tab.wait_for_timeout(250)
            deadline -= 0.25
        if await storage.get(walk.context, storage.WALLET_ADDRESS):
            return Result.failed("disconnecting left the wallet address stored")
        result = await visit_and_open(site, tab)
        if not result.ok:
            return Result.failed(f"after disconnecting the offer no longer works: "
                                 f"{result.detail}")
        frame = offer_frame(tab)
        if not await popup.visible(frame, popup.OFFER["connect_wallet"]):
            return Result.failed("disconnected, but the offer does not offer to connect")
        return Result.passed("address removed, offer back to Connect")
    return check


async def act(walk):
    walk.begin("ACT 5", "the wallet")
    first, second = walk.sites[0], walk.sites[1 % len(walk.sites)]
    modes = Modes()
    await modes.watch(walk.context)
    try:
        await walk.one("with no wallet the offer still shows", make_no_wallet(walk))
        await walk.clear()
        await walk.one("activating with no wallet takes the fast path",
                       make_fast_without_wallet(walk, modes))
        await walk.clear()
        await walk.one("connecting after the popup forces a fresh activation",
                       make_fresh_after_connecting(walk, modes))
        await walk.clear()
        await walk.one("connected before the popup: the address shows, the path is fast",
                       make_fast_with_wallet(walk, modes))
        await walk.clear()
        await walk.one("the wallet stays connected on the next shop",
                       make_stays_connected(walk, first), site=second)
        await walk.clear()
        await walk.one("disconnecting removes the address and the offer still works",
                       make_disconnect(walk))
        await walk.clear()
    finally:
        try:
            await walk.context.unroute(ACTIVATE_ENDPOINT)
        except Exception:
            pass
        await disconnect(walk.context)
