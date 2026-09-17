import asyncio
import time

from bring import config, netspy, popup, storage
from bring.walk import Result
from tests.steps import HOUR, MINUTE, unresolved

NEUTRAL = "https://example.com"
ADDRESS_A = config.WALLET          # the platform's own wallets, from one place
ADDRESS_B = config.OTHER_WALLET


async def fresh(walk, tab, mode=netspy.PASS):
    """A neutral page with the recorder armed and its counts cleared.

    Cleared after the navigation, not before: the recorder survives from the
    step before, and a waiter for "a check happened" returned at once on the
    previous step's call — measured as a disconnect racing the check it was
    supposed to follow.
    """
    await netspy.install(walk.context, mode)
    await tab.goto(NEUTRAL, wait_until="domcontentloaded")
    await netspy.reset(walk.context)


def backoff_of(window):
    """The span of a stored notificationCheck range, or a failure message."""
    if not (isinstance(window, list) and len(window) == 2):
        return None, f"stored {window!r} instead of a [start, end] range"
    start, end = window
    if not isinstance(end, (int, float)) or end != end:
        return None, f"the backoff end is not a number ({end!r}) — the [now, NaN] bug"
    span = end - start
    if abs(span - HOUR) > 5 * MINUTE:
        return None, f"backed off {span / MINUTE:.0f} minutes, not an hour"
    return span, ""


async def broadcast_until_checked(walk, tab, address, timeout=60) -> bool:
    """Broadcast *address* until the check it triggers has recorded it.

    The content script is injected after the page has loaded and only then
    registers its wallet listener; on a slow machine the first broadcast can
    go out before that, and the event is simply lost. Repeating it is
    harmless: once one has landed, the SDK's own dedup skips the rest
    ("address unchanged, skipping"). Eight seconds between tries, so a check
    that is merely slow is not counted twice.
    """
    deadline = time.time() + timeout
    while True:
        await netspy.broadcast_wallet(tab, address)
        marker = await storage.await_value(walk.context, storage.LAST_CHECKED_WALLET,
                                           address, timeout=8)
        if marker == address:
            return True
        if time.time() > deadline:
            return False


# ── 3.1 the request storm ───────────────────────────────────────────

def make_same_address_once(walk):
    async def check(site, tab):
        await fresh(walk, tab)
        if not await broadcast_until_checked(walk, tab, ADDRESS_A):
            return Result.failed("the first broadcast of a new address made no check")
        # Two checks can be owed here — the navigation's and the broadcast's.
        # Let the second land before counting repeats, or it is one.
        await tab.wait_for_timeout(2500)
        await netspy.reset(walk.context)
        for _ in range(5):
            await netspy.broadcast_wallet(tab, ADDRESS_A)
            await tab.wait_for_timeout(400)
        await tab.reload(wait_until="domcontentloaded")
        await tab.wait_for_timeout(1500)
        repeats = await netspy.count(walk.context, netspy.NOTIFICATION_CHECK)
        if repeats:
            return Result.failed(f"{repeats} checks fired for an unchanged address")
        return Result.passed("one check, then none for five re-broadcasts and a reload")
    return check


def make_real_change_once(walk):
    async def check(site, tab):
        await fresh(walk, tab)
        if not await broadcast_until_checked(walk, tab, ADDRESS_A):
            return Result.failed("the first check never recorded the address")
        await netspy.reset(walk.context)
        if not await broadcast_until_checked(walk, tab, ADDRESS_B):
            return Result.failed("switching accounts made no check: the marker never "
                                 "became the new address")
        try:
            fired = await netspy.count(walk.context, netspy.NOTIFICATION_CHECK)
        except netspy.SpyGone:
            fired = 1        # the marker moved, so a check ran; the recorder just missed it
        if fired != 1:
            return Result.failed(f"switching accounts fired {fired} checks; one is right")
        return Result.passed("one check, marker updated")
    return check


def make_always_answered(walk):
    async def check(site, tab):
        await fresh(walk, tab)
        errors = []
        tab.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await netspy.broadcast_wallet(tab, ADDRESS_A)
        await tab.wait_for_timeout(2000)
        await netspy.broadcast_wallet(tab, ADDRESS_A)      # the skipped one
        await tab.wait_for_timeout(2000)
        hanging = [e for e in errors if "lastError" in e or "message port closed" in e]
        if hanging:
            return Result.failed(f"the skipped broadcast left the sender hanging: "
                                 f"{hanging[0][:80]}")
        return Result.passed("both broadcasts answered")
    return check


def make_failed_check_backs_off(walk):
    async def check(site, tab):
        await storage.delete(walk.context, storage.NOTIFICATION_CHECK)
        await storage.delete(walk.context, storage.LAST_CHECKED_WALLET)
        await fresh(walk, tab, netspy.FAIL)
        try:
            await broadcast_until_checked(walk, tab, ADDRESS_A)
            window = await storage.await_key(walk.context, storage.NOTIFICATION_CHECK)
            span, bad = backoff_of(window)
            if bad:
                return Result.failed(bad)
            await netspy.reset(walk.context)
            for _ in range(3):
                await tab.reload(wait_until="domcontentloaded")
                await tab.wait_for_timeout(1200)
            retries = await netspy.count(walk.context, netspy.NOTIFICATION_CHECK)
            if retries:
                return Result.failed(f"{retries} checks fired during the backoff")
            return Result.passed(f"backed off {span / MINUTE:.0f} minutes, no retries")
        finally:
            await netspy.set_mode(walk.context, netspy.PASS)
    return check


def make_bad_reply_backs_off(walk, mode, label):
    async def check(site, tab):
        await storage.delete(walk.context, storage.NOTIFICATION_CHECK)
        await storage.delete(walk.context, storage.LAST_CHECKED_WALLET)
        await fresh(walk, tab, mode)
        try:
            await broadcast_until_checked(walk, tab, ADDRESS_A)
            window = await storage.await_key(walk.context, storage.NOTIFICATION_CHECK)
            shown = await popup.wait_for_popup(tab, timeout=3, route="notification")
            span, bad = backoff_of(window)
            if bad:
                return Result.failed(f"{label}: {bad}")
            if shown:
                return Result.failed(f"{label} produced a notification")
            return Result.passed(f"{label}: an hour's backoff, no notification")
        finally:
            await netspy.set_mode(walk.context, netspy.PASS)
    return check


def make_resumes_after_backoff(walk):
    async def check(site, tab):
        await storage.delete(walk.context, storage.NOTIFICATION_CHECK)
        await storage.delete(walk.context, storage.LAST_CHECKED_WALLET)
        await fresh(walk, tab, netspy.FAIL)
        try:
            await broadcast_until_checked(walk, tab, ADDRESS_A)
            if not await storage.await_key(walk.context, storage.NOTIFICATION_CHECK):
                return Result.failed("no backoff was written, nothing to recover from")
        finally:
            await netspy.set_mode(walk.context, netspy.PASS)
        if not await storage.expire_key(walk.context, storage.NOTIFICATION_CHECK):
            return Result.failed("could not move the backoff into the past")
        await tab.goto(NEUTRAL + "/?again", wait_until="domcontentloaded")
        if not await broadcast_until_checked(walk, tab, ADDRESS_B):
            return Result.failed("the backoff expired but no check followed")
        return Result.passed("checked again once the hour was up")
    return check


def make_disconnect_reconnect(walk):
    async def check(site, tab):
        await fresh(walk, tab)
        if not await broadcast_until_checked(walk, tab, ADDRESS_A):
            return Result.failed("the first check never recorded the address")
        # The page load owes a check of its own, made with no wallet; if it
        # lands after the disconnect it legitimately writes the marker as ''.
        # Let it land first. An absence, so a fixed wait.
        await tab.wait_for_timeout(2500)

        await netspy.broadcast_wallet(tab, "")
        deadline = asyncio.get_event_loop().time() + 20
        while await storage.get(walk.context, storage.WALLET_ADDRESS):
            if asyncio.get_event_loop().time() > deadline:
                return Result.failed("disconnecting did not remove the stored address")
            await asyncio.sleep(0.2)
        if await storage.get(walk.context, storage.LAST_CHECKED_WALLET) != ADDRESS_A:
            return Result.failed("disconnecting cleared lastCheckedWalletAddress; "
                                 "it must survive")

        await netspy.reset(walk.context)
        await netspy.broadcast_wallet(tab, ADDRESS_A)
        await tab.wait_for_timeout(4000)        # an absence: a fixed wait
        same = await netspy.count(walk.context, netspy.NOTIFICATION_CHECK)
        if same:
            return Result.failed(f"reconnecting the same wallet forced {same} checks")

        # A different wallet is proved by the mark its check leaves, not by
        # the recorder: MV3 recycles the worker, and the wrapper with it.
        if not await broadcast_until_checked(walk, tab, ADDRESS_B):
            return Result.failed("reconnecting a different wallet made no check")
        return Result.passed("address removed, marker kept, same wallet quiet, "
                             "different wallet checked")
    return check


def make_active_window_skips(walk):
    async def check(site, tab):
        now = int(time.time() * 1000)
        await storage.set(walk.context, storage.NOTIFICATION_CHECK, [now, now + HOUR])
        await storage.set(walk.context, storage.LAST_CHECKED_WALLET, ADDRESS_A)
        await fresh(walk, tab)
        await netspy.reset(walk.context)
        await tab.reload(wait_until="domcontentloaded")
        await tab.wait_for_timeout(5000)        # an absence: a fixed wait
        fired = await netspy.count(walk.context, netspy.NOTIFICATION_CHECK)
        if fired:
            return Result.failed(f"{fired} checks fired inside an active window")
        return Result.passed("no call while the window is active")
    return check


# ── 3 the notification surface ──────────────────────────────────────

# What each seeded variant must put on screen, from the product's own rules
# (iframe `Notification.tsx`, server `db-operations.ts` / `main.ts`):
#
# - a connected wallet gets the simple layout: "New cashback reward" and a
#   Details button, no amounts;
# - no wallet: "Just earned: <new>", plus "Total" while nothing is expiring or
#   "Claimable" once a READY reward is 122 days old (that is also when the
#   deadline line appears); the button is Claim when something is claimable
#   and Connect otherwise; Stop Reminding comes only on the reminder path.
EXPECTED = {
    "reward_approval": {"wallet": True,  "text": ("New cashback reward",),
                        "cta": "Details", "deadline": False, "stop": False},
    "walletless_1":    {"wallet": False, "text": ("Just earned", "0.42"),
                        "cta": "Connect", "deadline": False, "stop": False},
    "walletless_2":    {"wallet": False, "text": ("Just earned", "0.42", "Total", "0.73"),
                        "cta": "Connect", "deadline": False, "stop": False},
    "walletless_3":    {"wallet": False, "text": ("Just earned", "0.42", "Claimable", "0.55"),
                        "cta": "Claim", "deadline": True, "stop": False},
    "walletless_4":    {"wallet": False, "text": ("Claimable", "0.55"),
                        "cta": "Claim", "deadline": True, "stop": True},
}


class Seeder:
    """The database, or the reason there is none."""

    def __init__(self):
        from bring import db, seed
        self.db, self.seed = db, seed
        self.database = None
        self.reason = ""
        if not db.configured():
            self.reason = "no database configured, so no variant can be produced (.env)"
            return
        try:
            self.database = db.database_for()
        except db.NoDatabase as e:
            self.reason = str(e).splitlines()[0]

    def write(self, variant, user_id, wallet):
        return self.seed.seed(variant, database=self.database, user_id=user_id,
                              wallet_address=wallet)

    def clean(self, written):
        try:
            self.seed.clean(written)
        except Exception:
            pass


async def show_variant(walk, tab, seeder, variant, wallet):
    """Seed, clear the state that would stop a check, and wait for the
    notification. Seeded twice if the first check comes back empty: a
    purchase counts as new exactly once, and a check still in flight from
    the step before can be the one that consumes it."""
    user_id = await storage.get(walk.context, "id")
    if not user_id:
        return None, None, Result.failed("the extension has no user id to seed against")
    own_wallet = "k:" + (user_id.replace("-", "") * 2)[:64]
    address = own_wallet if wallet else ""
    written = []
    answered = None
    for attempt in (1, 2):
        written.append(seeder.write(variant, user_id, own_wallet))
        for key in (storage.NOTIFICATION, storage.NOTIFICATION_CHECK,
                    storage.LAST_CHECKED_WALLET, storage.WALLET_ADDRESS):
            await storage.delete(walk.context, key)
        # The host extension's own key is what the content script reports on
        # every navigation; left set, the first load checks with the previous
        # variant's wallet and stores that answer.
        await netspy.set_host_wallet(walk.context, address)
        await netspy.install(walk.context, netspy.PASS)
        await netspy.reset(walk.context)
        await tab.goto(NEUTRAL, wait_until="domcontentloaded")
        await netspy.broadcast_wallet(tab, address)
        await netspy.await_call(walk.context, netspy.NOTIFICATION_CHECK, timeout=30)
        frame = await popup.wait_for_popup(tab, timeout=20, route="notification")
        if frame is None and await storage.get(walk.context, storage.NOTIFICATION):
            # Stored, not yet shown: the SDK shows a stored notification on
            # the next navigation. Ask for one.
            await tab.goto(NEUTRAL + "/?again", wait_until="domcontentloaded")
            frame = await popup.wait_for_popup(tab, timeout=20, route="notification")
        if frame:
            return frame, written, None
        try:
            answered = await netspy.last_body(walk.context, netspy.NOTIFICATION_CHECK)
        except netspy.SpyGone:
            answered = None
    shown = (answered or {}).get("showNotification") if isinstance(answered, dict) else None
    return None, written, Result.failed(
        f"seeded twice, nothing appeared either time (the server said "
        f"showNotification={shown!r} on the second, against rows nothing else "
        f"could have consumed)")

def seeded_step(walk, seeder, variant, wallet, judge):
    """Seed *variant*, wait for its notification, hand the frame to *judge*,
    and remove the rows whatever happened."""
    async def check(site, tab):
        if seeder.reason:
            return Result.blocked(seeder.reason)
        frame, written, bad = await show_variant(walk, tab, seeder, variant, wallet)
        try:
            return bad or await judge(frame, tab)
        finally:
            for row in written or []:
                seeder.clean(row)
    return check


def make_variant(walk, seeder, variant):
    want = EXPECTED[variant]

    async def judge(frame, tab):
        body = await popup.body_text(frame)
        cta = (await popup.text(frame, popup.NOTIFICATION["cta"])).strip()
        stop = await popup.visible(frame, popup.NOTIFICATION["stop_reminders"])
        deadline = (await popup.visible(frame, popup.NOTIFICATION["expiration"])
                    or "deadline" in body.lower())
        problems = []
        unres = unresolved(body)
        if unres:
            problems.append(f"{unres!r} in the text")
        for piece in want["text"]:
            if piece not in body:
                problems.append(f"{piece!r} not shown")
        if cta.lower() != want["cta"].lower():
            problems.append(f"button says {cta!r}, expected {want['cta']!r}")
        if want["deadline"] and not deadline:
            problems.append("no deadline")
        if not want["deadline"] and deadline:
            problems.append("a deadline where none is owed")
        if stop != want["stop"]:
            problems.append("Stop Reminding is showing" if stop else "no Stop Reminding")
        if problems:
            return Result.failed("; ".join(problems) + f" — {body[:70]!r}")
        return Result.passed(f"{cta}" + (" + Stop Reminding" if stop else "")
                             + ", " + " · ".join(want["text"])
                             + (", deadline" if deadline else ""))
    return seeded_step(walk, seeder, variant, want["wallet"], judge)


def make_rounding(walk, seeder):
    async def judge(frame, tab):
        earned = await popup.text(frame, popup.NOTIFICATION["earned"]) or             await popup.body_text(frame)
        # The server keeps the first significant digit plus one and
        # truncates (`formatToFirstNonZeroPlus`): 0.0457 → 0.045. Never a
        # fourth decimal, never rounded up to a reward not yet earned.
        if "0.0457" in earned:
            return Result.failed(f"0.0457 shown with four decimals: {earned[:60]!r}")
        if "0.045" not in earned:
            return Result.failed(f"0.0457 did not become 0.045: {earned[:60]!r}")
        return Result.passed("0.0457 shows as 0.045 (three decimals, truncated)")
    return seeded_step(walk, seeder, "rounding", False, judge)


def make_close_removes(walk, seeder):
    async def judge(frame, tab):
        if not await popup.click(frame, popup.NOTIFICATION["close_x"], settle=3):
            return Result.failed("the notification has no X")
        if not await popup.wait_for_gone(tab, timeout=20, route="notification"):
            return Result.failed("still on the page after the X")
        if await storage.get(walk.context, storage.NOTIFICATION):
            return Result.failed("closing left the stored copy behind, so it "
                                 "will reappear on the next navigation")
        return Result.passed("closed and removed from storage")
    return seeded_step(walk, seeder, "walletless_1", False, judge)


def make_stop_reminding(walk, seeder):
    async def judge(frame, tab):
        if not await popup.visible(frame, popup.NOTIFICATION["stop_reminders"]):
            return Result.failed("walletless_4 offers no Stop Reminding")
        if not await popup.click(frame, popup.NOTIFICATION["stop_reminders"], settle=3):
            return Result.failed("could not click Stop Reminding")
        if not await storage.await_key(walk.context, "disableReminders"):
            return Result.failed("Stop Reminding did not set disableReminders")
        await storage.delete(walk.context, "disableReminders")
        return Result.passed("disableReminders set")
    return seeded_step(walk, seeder, "walletless_4", False, judge)


async def act(walk):
    walk.begin("ACT 8", "notifications — the reward check, then every variant")
    site = NEUTRAL
    checks = [
        ("a new address checks once; the same address again costs nothing",
         make_same_address_once(walk)),
        ("a real address change fires exactly one check", make_real_change_once(walk)),
        ("the broadcast is always answered", make_always_answered(walk)),
        ("a failed check backs off for an hour", make_failed_check_backs_off(walk)),
        ("a block page instead of JSON backs off the same way",
         make_bad_reply_backs_off(walk, netspy.NON_JSON, "a block page")),
        ("an error body with no nextCall backs off the same way",
         make_bad_reply_backs_off(walk, netspy.ERROR_BODY, "an error body")),
        ("the check resumes once the backoff expires", make_resumes_after_backoff(walk)),
        ("disconnecting removes the address; reconnecting is quiet",
         make_disconnect_reconnect(walk)),
        ("an active check window skips the server", make_active_window_skips(walk)),
    ]
    for title, work in checks:
        await walk.one(title, work, site=site)
        for key in (storage.NOTIFICATION, storage.NOTIFICATION_CHECK,
                    storage.LAST_CHECKED_WALLET, storage.WALLET_ADDRESS):
            await storage.delete(walk.context, key)
    await netspy.set_mode(walk.context, netspy.PASS)

    seeder = Seeder()
    if seeder.reason:
        walk.note(seeder.reason)
    if seeder.database:
        seeder.seed.clean_all(seeder.database)
    await storage.delete(walk.context, "disableReminders")
    variants = [(f"variant {v}", make_variant(walk, seeder, v)) for v in EXPECTED]
    variants += [
        ("amounts between 0.01 and 0.1 show three decimals, not four", make_rounding(walk, seeder)),
        ("closing the notification removes it", make_close_removes(walk, seeder)),
        ("Stop Reminding turns the reminders off", make_stop_reminding(walk, seeder)),
    ]
    try:
        for title, work in variants:
            await walk.one(title, work, site=site)
    finally:
        await netspy.set_host_wallet(walk.context, "")
        for key in (storage.NOTIFICATION, storage.NOTIFICATION_CHECK,
                    storage.LAST_CHECKED_WALLET, storage.WALLET_ADDRESS):
            await storage.delete(walk.context, key)
