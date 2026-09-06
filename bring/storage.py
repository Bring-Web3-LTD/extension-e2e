"""Read and write the extension's saved state, from outside the extension.

The SDK exposes `bringCache` on its service worker (utils/storage/storage.ts,
`initializeDebugCache`), which is the same door its own debug tooling uses.
Writes go through it rather than straight to `chrome.storage.local`, because
the SDK reads most values from an in-memory cache first — a raw storage write
lands in the browser but not in the extension, and the test then asserts
against a value the product never saw.

The write side is what makes the time-gated half of the QA plan testable at
all. A close silences a retailer for 30 minutes, an opt-out can be forever, a
failed reward check backs off an hour, and a stand-down lasts two. No suite can
sit through those. The expiry is a timestamp, so moving the timestamp into the
past and reloading exercises exactly the code an hour of waiting would.
"""
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
DEBUG_MODE = "debugMode"

# Every key the SDK writes, so a test can assert on all of them rather than on
# the two that happen to be interesting today. Mirrors the table at the end of
# QA_TEST_PLAN.md section 4.
ALL_KEYS = (
    "id", "walletAddress", "popupEnabled", "disableReminders", "optOut",
    "envName", "relevantDomains", "relevantDomainsCheck", "domainsTypes",
    "postPurchaseUrls", "quietDomainsMaxLength", "standDownOffset",
    "redirectsWhitelist", "quietDomains", "portalRelevantDomains",
    "lastActivation", "notification", "notificationCheck", "migrationVersion",
    "extensionMemoryTest", "optOutDomains", "debugMode",
    "lastCheckedWalletAddress",
)

# Written by a removed feature. Its presence after an upgrade is the finding.
DEPRECATED_KEYS = ("postPurchaseUrls", "optOutDomains")


async def _call(context, expression: str, timeout: float = 10):
    worker = await wake_worker(context)
    return await worker.evaluate(expression)


async def get(context, key: str):
    """One saved value, as the extension itself would read it."""
    return await _call(context, f"bringCache.get({json.dumps(key)})")


async def set(context, key: str, value) -> None:
    """Write a value the extension will actually read back.

    Goes through `bringCache.set`, which updates the in-memory cache and
    `chrome.storage.local` together. Writing only the latter leaves the SDK
    reading a stale cached value for the rest of the session.
    """
    await _call(context, f"bringCache.set({json.dumps(key)}, {json.dumps(value)})")


async def delete(context, key: str) -> None:
    await _call(context, f"bringCache.delete({json.dumps(key)})")


async def dump(context) -> dict:
    """Every `bring_` value in this profile, unprefixed.

    Read straight from chrome.storage rather than through the cache, because
    this is the evidence attached to a failure: what is actually on disk, keys
    the cache never loaded included.
    """
    raw = await _call(context, "chrome.storage.local.get(null)") or {}
    return {k[len(PREFIX):]: v for k, v in raw.items() if k.startswith(PREFIX)}


async def settled(context, timeout: float = 15) -> bool:
    """Wait until the extension has finished writing its own first-run state.

    On a fresh profile the SDK runs its data migration on startup, and that
    migration *writes* `quietDomains`. A test that seeds storage the instant the
    worker wakes has its rows overwritten a moment later, and then fails on a
    state nobody put there — which is exactly what happened while building this,
    and reads as a random flake because the race is decided by milliseconds.

    `migrationVersion` reaching its current value is the marker: the last thing
    the migration does is write it.
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await get(context, "migrationVersion"):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False


async def reset(context) -> None:
    """Forget every silence this run has written.

    Close, activate and opt-out each silence the retailer, so a later step on
    the same retailer would find no popup — which is correct behaviour and
    still a useless test. Clearing the two keys that hold it is the same reset
    as throwing the profile away, without the browser restart.
    """
    for key in (QUIET_DOMAINS, OPT_OUT):
        await delete(context, key)


# ── quiet domains ───────────────────────────────────────────────────

def normalise(host_or_url: str) -> str:
    """The bare host, however it was written."""
    host = host_or_url.split("//")[-1].split("/")[0].lower()
    return host.removeprefix("*.").removeprefix("www.")


def entries_for(entries, url: str) -> list:
    """Every quietDomains row covering *url*.

    One retailer can hold several: the list is keyed by (domain, type), so a
    row the user wrote and a row the server wrote coexist. The backend stores
    subdomain-inclusive retailers as `*.domain`, so a row written for
    `*.aliexpress.com` has to match a visit to `www.aliexpress.com` — without
    stripping the wildcard, a row that exists reads as nothing written.
    """
    host = normalise(url)
    if not host:
        return []
    found = []
    for entry in entries or []:
        # The list is read straight out of the extension, and the whole point
        # of several of these tests is to put bad data in it. A null or a bare
        # string in there is a finding for whoever asserts on it, not a reason
        # for this helper to raise.
        if not isinstance(entry, dict):
            continue
        domain = normalise(str(entry.get("domain", "")))
        if not domain:
            continue
        if domain == host or host.endswith("." + domain):
            found.append(entry)
    return found


def entry_for(entries, url: str, type_prefix: str = None):
    """The row covering *url*, or None. `getQuietDomain` returns the first."""
    found = entries_for(entries, url)
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


# ── moving the clock ────────────────────────────────────────────────

def past(span=None):
    """A timestamp range that ended a minute ago."""
    now = int(time.time() * 1000)
    return [now - 7200_000, now - 60_000]


async def expire_quiet(context, url: str) -> bool:
    """Push every row for *url* into the past, and report whether any moved.

    This is how "revisit once the silence runs out" is tested without waiting
    out the silence. Note what it deliberately does not do: it leaves the rows
    in place rather than deleting them, because the SDK prunes quietDomains
    only on the next *write* — so an expired row sitting there is the state the
    product is actually in, and section 1.9's "an expired entry must not shadow
    a valid one" only means anything against it.
    """
    rows = await quiet_domains(context)
    moved = False
    for entry in rows:
        if isinstance(entry, dict) and entries_for([entry], url):
            entry["time"] = past()
            moved = True
    if moved:
        await set(context, QUIET_DOMAINS, rows)
    return moved


async def expire_key(context, key: str) -> bool:
    """Push a bare `[start, end]` value — optOut, notificationCheck — into the past."""
    value = await get(context, key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    await set(context, key, past())
    return True
