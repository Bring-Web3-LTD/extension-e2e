"""Reward notifications: the request storm that was fixed, and the surface.

QA_TEST_PLAN section 3.1 in full, and as much of section 3 as can be reached
without a seeded reward.

Section 3's five variants are decided entirely by five fields the server signs
into the notification token — `promptPairing`, `new`, `eligible`, `total`,
`expiredAt` (backend notification/db-operations.ts) — and those come from rows
in the environment's `purchases` table. The token is signed, so it cannot be
fabricated here. Those tests therefore skip, loudly, with what they need, and
what is covered instead is everything the client decides: when a check is made,
what a failed one costs, and that the surface renders when there is one.
"""
import time

import pytest

from bring import netspy, popup, storage

pytestmark = pytest.mark.notification

HOUR_MS = 60 * 60 * 1000
ADDRESS_A = "addr1qydfh2z0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5psyq79gflnhwlttgw8gk7aj5j6lj95vg7my67vpsdcvu4l"
ADDRESS_B = "addr1q9zzzzzz0m4j2297rzwsu7dfu4ld3a6nhgytrn2wzxgvdlwd6y4l5psyq79gflnhwlttgw8gk7aj5j6lj95vg7my67vpsqqqqqq"

NEUTRAL = "https://example.com"


async def settle(page, ms: int = 2500):
    await page.wait_for_timeout(ms)


# ── 3.1 the request storm ───────────────────────────────────────────

async def test_repeated_broadcasts_of_the_same_address_make_one_check(context):
    """3.1 — wallets re-broadcast constantly; only a real change earns a check.

    Before the fix this fired a reward check per broadcast per frame per
    navigation, which on a page with a few iframes is a burst of identical
    requests on every page the user opens.
    """
    await netspy.install(context, netspy.PASS)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)
    first = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    assert first >= 1, \
        "the first broadcast of a new address made no reward check at all"

    await netspy.reset(context)
    for _ in range(5):
        await netspy.broadcast_wallet(page, ADDRESS_A)
        await page.wait_for_timeout(400)
    await page.reload(wait_until="domcontentloaded")
    await settle(page, 4000)

    repeats = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    await page.close()
    assert repeats == 0, (
        f"{repeats} reward checks fired for an unchanged wallet address — the "
        f"broadcast dedup is not holding")


async def test_a_real_address_change_fires_exactly_one_check(context):
    """3.1 — switching accounts bypasses the throttle, once."""
    await netspy.install(context, netspy.PASS)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)
    assert await storage.get(context, storage.LAST_CHECKED_WALLET) == ADDRESS_A, \
        "lastCheckedWalletAddress was not updated after the first check"

    await netspy.reset(context)
    await netspy.broadcast_wallet(page, ADDRESS_B)
    await settle(page, 4000)

    fired = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    marker = await storage.get(context, storage.LAST_CHECKED_WALLET)
    await page.close()

    assert fired == 1, \
        f"switching wallet account fired {fired} reward checks; exactly one is right"
    assert marker == ADDRESS_B, \
        f"lastCheckedWalletAddress is {marker!r}, not the address just checked"


async def test_the_broadcast_is_always_answered(context):
    """3.1 — the wallet page must not hang, even when the check is skipped.

    The skipped path used to return without calling sendResponse, which leaves
    the caller waiting and Chrome logging an unchecked runtime.lastError.
    """
    await netspy.install(context, netspy.PASS)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 3000)
    await netspy.broadcast_wallet(page, ADDRESS_A)      # the skipped one
    await settle(page, 3000)
    await page.close()

    unchecked = [e for e in errors if "lastError" in e or "message port closed" in e]
    assert not unchecked, \
        f"the skipped broadcast left the sender hanging: {unchecked[:3]}"


async def test_a_failed_check_backs_off_for_an_hour(context):
    """3.1 — one failure, then silence for an hour, not a retry per navigation.

    The old bug stored `[now, NaN]`, which reads as already expired, so every
    subsequent navigation retried a server that was not answering.
    """
    await netspy.install(context, netspy.FAIL)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)

    window = await storage.get(context, storage.NOTIFICATION_CHECK)
    assert isinstance(window, list) and len(window) == 2, \
        f"a failed check stored {window!r} instead of a [start, end] range"
    start, end = window
    assert isinstance(end, (int, float)) and end == end, \
        f"the backoff end is not a number ({end!r}) — this is the [now, NaN] bug"
    span = end - start
    assert abs(span - HOUR_MS) < 5 * 60_000, \
        f"expected roughly an hour of backoff, got {span / 60_000:.1f} minutes"

    await netspy.reset(context)
    for _ in range(3):
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
    retries = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    await page.close()

    assert retries == 0, \
        f"{retries} reward checks fired during the hour-long backoff"


@pytest.mark.parametrize("mode,label", [
    (netspy.NON_JSON, "a block page instead of JSON"),
    (netspy.ERROR_BODY, "an error body with no nextCall"),
])
async def test_a_bad_reply_backs_off_the_same_way(context, mode, label):
    """3.1 — a WAF page or an error body must not become an immediate retry."""
    await netspy.install(context, mode)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)

    window = await storage.get(context, storage.NOTIFICATION_CHECK)
    shown = await popup.wait_for_popup(page, timeout=3, route="notification")
    await page.close()

    assert isinstance(window, list) and len(window) == 2, \
        f"{label} stored {window!r} instead of a backoff range"
    span = window[1] - window[0]
    assert abs(span - HOUR_MS) < 5 * 60_000, \
        f"{label} produced a {span / 60_000:.1f}-minute backoff instead of an hour"
    assert shown is None, f"{label} produced a notification"


async def test_the_check_resumes_once_the_backoff_expires(context):
    """3.1 — recovery. After the hour, the next navigation checks again."""
    await netspy.install(context, netspy.FAIL)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")
    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)
    assert await storage.get(context, storage.NOTIFICATION_CHECK), \
        "no backoff was written, so there is nothing to recover from"

    await netspy.set_mode(context, netspy.PASS)
    assert await storage.expire_key(context, storage.NOTIFICATION_CHECK), \
        "the backoff range could not be moved into the past"

    await netspy.reset(context)
    await page.goto(NEUTRAL + "/?again", wait_until="domcontentloaded")
    await netspy.broadcast_wallet(page, ADDRESS_B)
    await settle(page, 4000)
    resumed = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    await page.close()

    assert resumed >= 1, \
        "the backoff expired but no reward check was made on the next visit"


async def test_disconnecting_removes_the_address_and_reconnecting_is_quiet(context):
    """3.1 — disconnect clears walletAddress; the same wallet back is not a change.

    lastCheckedWalletAddress deliberately survives a disconnect, so reconnecting
    the same wallet does not force a check — and no notification is lost.
    """
    await netspy.install(context, netspy.PASS)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")

    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 4000)

    await netspy.broadcast_wallet(page, "")
    await settle(page, 2500)
    assert not await storage.get(context, storage.WALLET_ADDRESS), \
        "disconnecting did not remove the stored wallet address"
    assert await storage.get(context, storage.LAST_CHECKED_WALLET) == ADDRESS_A, \
        "disconnecting cleared lastCheckedWalletAddress; it must survive"

    await netspy.reset(context)
    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 3000)
    same = await netspy.count(context, netspy.NOTIFICATION_CHECK)

    await netspy.reset(context)
    await netspy.broadcast_wallet(page, ADDRESS_B)
    await settle(page, 4000)
    different = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    await page.close()

    assert same == 0, \
        f"reconnecting the same wallet forced {same} reward checks"
    assert different == 1, \
        f"reconnecting a different wallet fired {different} checks; one is right"


async def test_an_active_check_window_skips_the_server(context):
    """3.1 + 4 — while notificationCheck is in the future, no call is made."""
    now = int(time.time() * 1000)
    await storage.set(context, storage.NOTIFICATION_CHECK, [now, now + HOUR_MS])
    await storage.set(context, storage.LAST_CHECKED_WALLET, ADDRESS_A)
    await netspy.install(context, netspy.PASS)

    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")
    await netspy.reset(context)
    await page.reload(wait_until="domcontentloaded")
    await settle(page, 3000)
    fired = await netspy.count(context, netspy.NOTIFICATION_CHECK)
    await page.close()

    assert fired == 0, \
        f"{fired} reward checks fired inside an active notificationCheck window"


# ── 3 the notification surface ──────────────────────────────────────

async def test_a_notification_renders_correctly_when_there_is_one(context):
    """3 — if the environment has a reward for us, the surface must be sound.

    Skips rather than fails when there is nothing to show: on a fresh
    environment there are no purchases, and "no notification" is the correct
    answer, not a bug.
    """
    await netspy.install(context, netspy.PASS)
    page = await context.new_page()
    await page.goto(NEUTRAL, wait_until="domcontentloaded")
    await netspy.broadcast_wallet(page, ADDRESS_A)
    await settle(page, 5000)

    frame = await popup.wait_for_popup(page, timeout=10, route="notification")
    if frame is None:
        saved = await storage.get(context, storage.NOTIFICATION)
        await page.close()
        pytest.skip(
            "no notification to inspect — this environment has no reward for the "
            "test wallet. Seed a row in `purchases` for it to exercise section 3. "
            f"(stored notification: {bool(saved)})")

    body = await popup.body_text(frame)
    cta = await popup.text(frame, popup.NOTIFICATION["cta"])
    await page.close()

    assert body.strip(), "the notification rendered empty"
    assert "undefined" not in body and "NaN" not in body, \
        f"the notification shows an unresolved value: {body[:200]!r}"
    assert cta in ("Details", "Claim", "Connect"), \
        f"unexpected notification call to action: {cta!r}"


@pytest.mark.needs_db
@pytest.mark.parametrize("variant", [
    "reward_approval", "walletless_1", "walletless_2", "walletless_3", "walletless_4",
])
def test_notification_variant(variant):
    """3 — each variant's text and buttons.

    Not reachable from the client. The variant is chosen by fields the server
    signs into the token, and those come from `purchases` rows:

      reward_approval  wallet paired, a new approved purchase   -> Details
      walletless_1     no wallet, new purchase, no deadline     -> Connect
      walletless_2     no wallet, new purchase, in reminding    -> Connect
      walletless_3     no wallet, new + claimable, in reminding -> Claim
      walletless_4     no wallet, claimable only, in reminding  -> Claim, Stop Reminding

    Seeding those rows is the missing half. Point this suite at a database with
    them and drop the marker.
    """
    pytest.skip(f"{variant} needs a seeded purchases row in the environment's database")
