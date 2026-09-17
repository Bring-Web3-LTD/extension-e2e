import asyncio
import json
import time

from bring.browser import wake_worker

PREFIX = "bring_"

QUIET_DOMAINS = "quietDomains"
OPT_OUT = "optOut"
NOTIFICATION = "notification"
NOTIFICATION_CHECK = "notificationCheck"
LAST_CHECKED_WALLET = "lastCheckedWalletAddress"
WALLET_ADDRESS = "walletAddress"


async def _call(context, expression: str, timeout: float = 10):
    worker = await wake_worker(context)
    return await worker.evaluate(expression)


async def get(context, key: str):
    """One saved value, as the extension itself would read it."""
    return await _call(context, f"bringCache.get({json.dumps(key)})")


async def set(context, key: str, value) -> None:
    await _call(context, f"bringCache.set({json.dumps(key)}, {json.dumps(value)})")


async def delete(context, key: str) -> None:
    await _call(context, f"bringCache.delete({json.dumps(key)})")


async def dump(context) -> dict:
    raw = await _call(context, "chrome.storage.local.get(null)") or {}
    return {k[len(PREFIX):]: v for k, v in raw.items() if k.startswith(PREFIX)}


#: Not a retailer, so visiting it cannot silence anything or affect a test.
WARMUP_URL = "https://example.com/"


async def settled(context, timeout: float = 45) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    warmed = False

    while asyncio.get_event_loop().time() < deadline:
        try:
            if await get(context, "migrationVersion"):
                entries = await get(context, "relevantDomains")
                if entries:
                    return True
                if not warmed:
                    warmed = True
                    page = await context.new_page()
                    try:
                        await page.goto(WARMUP_URL, wait_until="domcontentloaded",
                                        timeout=20_000)
                    except Exception:
                        pass        # even a failed navigation runs the content script
                    finally:
                        await page.close()
        except Exception:
            pass
        await asyncio.sleep(0.5)

    return False


# ── quiet domains ───────────────────────────────────────────────────

def normalise(host_or_url: str) -> str:
    """The bare host, however it was written."""
    host = host_or_url.split("//")[-1].split("/")[0].lower()
    return host.removeprefix("*.").removeprefix("www.")


def is_live(entry, now: int = None) -> bool:
    span = (entry or {}).get("time")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return False
    start, end = span
    if not all(isinstance(v, (int, float)) for v in (start, end)):
        return False
    now = now if now is not None else int(time.time() * 1000)
    return start <= now <= end


def entries_for(entries, url: str, live_only: bool = False) -> list:
    host = normalise(url)
    if not host:
        return []
    now = int(time.time() * 1000)
    found = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        domain = normalise(str(entry.get("domain", "")))
        if not domain:
            continue
        if domain == host or host.endswith("." + domain):
            if live_only and not is_live(entry, now):
                continue
            found.append(entry)
    return found


def entry_for(entries, url: str, type_prefix: str = None):
    """The row the extension would obey for *url*, or None.

    Expired rows are skipped, as `getQuietDomain` skips them. Counting a dead
    row as a silence is how "the popup came back on a shop which is still
    silenced" was reported against an extension that had correctly noticed the
    window had ended.
    """
    found = entries_for(entries, url, live_only=True)
    if type_prefix:
        found = [e for e in found if str(e.get("type", "")).startswith(type_prefix)]
    return found[0] if found else None


def window_ms(entry) -> int:
    """How long a row silences for, or None when the range is malformed."""
    span = (entry or {}).get("time")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    start, end = span
    if not all(isinstance(v, (int, float)) for v in (start, end)) or end < start:
        return None
    return int(end - start)


async def quiet_domains(context) -> list:
    return await get(context, QUIET_DOMAINS) or []


async def quiet_entry(context, url: str, type_prefix: str = None):
    return entry_for(await quiet_domains(context), url, type_prefix)


async def await_key(context, key: str, timeout: float = 30):
    """A saved value, once the extension has actually written it.

    The same race as `await_quiet_entry`, one level up. A wallet broadcast does
    not write `notificationCheck`: it starts a check, which fails or answers,
    and only then is the window stored. Sleeping a flat two or four seconds and
    reading is a guess about how long that takes — too short and the test reads
    None and calls it a defect, too long and every run pays for the worst case.

    Returns as soon as the key has a value, so a quick write costs nothing.
    """
    deadline = time.time() + timeout
    while True:
        value = await get(context, key)
        if value not in (None, "", [], {}) or time.time() >= deadline:
            return value
        await asyncio.sleep(0.2)


async def await_value(context, key: str, expected, timeout: float = 30):
    """A saved value once it *equals* `expected`, or whatever it is at the end.

    `await_key` waits for the key to hold anything, which is the wrong question
    when it already holds the previous value and the point is that it changes
    — an address switch, say. That returned the old address on the first poll
    and reported the switch as not having happened.
    """
    deadline = time.time() + timeout
    while True:
        value = await get(context, key)
        if value == expected or time.time() >= deadline:
            return value
        await asyncio.sleep(0.2)


async def await_quiet_entry(context, url: str, timeout: float = 20,
                            type_prefix: str = None):
    """The shop's quiet row, once the extension has actually written it.

    Closing a popup does not write the row: it sends CLOSE to the background,
    which writes it a moment later. Reading the list straight after the click
    is a race the test loses often enough to look like a defect — measured, it
    failed five checks a run with the row appearing milliseconds afterwards.

    Returns as soon as the row exists, so the wait costs nothing when the write
    was quick. Returns None after *timeout* for callers asserting an absence,
    which is the one case that spends the whole budget.
    """
    deadline = time.time() + timeout
    while True:
        entry = entry_for(await quiet_domains(context), url, type_prefix)
        if entry or time.time() >= deadline:
            return entry
        await asyncio.sleep(0.2)


async def await_quiet_phase(context, url: str, phase: str,
                            timeout: float = 20):
    """The shop's row once it reads *phase*, or the row as it stands at the end.

    `await_quiet_entry` waits for a row to exist, which is the wrong condition
    whenever one is already there and the point is that it *changes* — an
    activation dismissed back to `quiet`, say. It returns the stale row on the
    first poll, and the test reports the value it was about to stop being.
    """
    deadline = time.time() + timeout
    while True:
        entry = entry_for(await quiet_domains(context), url)
        if (entry or {}).get("phase") == phase or time.time() >= deadline:
            return entry
        await asyncio.sleep(0.2)


async def await_rows(context, urls, timeout: float = 30) -> list:
    """Wait until every url in *urls* has a row, then return what is missing.

    Several tabs closing at once each write the whole list back, and the writes
    land one after another rather than together. A flat sleep either passes
    before the last one has landed — reporting a lost row that was merely late
    — or spends its whole budget on every healthy run.
    """
    deadline = time.time() + timeout
    while True:
        rows = await quiet_domains(context)
        missing = [u for u in urls if not entries_for(rows, u)]
        if not missing or time.time() >= deadline:
            return missing
        await asyncio.sleep(0.25)


# ── moving the clock ────────────────────────────────────────────────

def past(span=None):
    """A timestamp range that ended a minute ago."""
    now = int(time.time() * 1000)
    return [now - 7200_000, now - 60_000]


async def expire_key(context, key: str) -> bool:
    """Push a bare `[start, end]` value — optOut, notificationCheck — into the past."""
    value = await get(context, key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    await set(context, key, past())
    return True
