"""Which retailers this run tests against, and how they spread over workers.

Every test runs once per retailer. That is the unit of parallelism: one worker
takes AliExpress, another takes the next shop, each in its own browser with its
own profile. Adding a retailer adds a lane rather than lengthening a queue — so
`-n 4` across four retailers costs roughly what one retailer costs alone.

Two is the floor, and not arbitrarily: close, activate and opt-out each have to
prove the silence they wrote applies to *this* retailer and not to another one.
So every retailer also needs a control — a different shop it must not have
touched — taken from the rest of the list.

A retailer here is only correct while it is live where the browser is. A shop
with no offer in this country returns `isValid = false` and no popup, which is
right, and reads as a product failure if the list has gone stale. The preflight
asks before the suite runs, so a stale name is a config error rather than forty
red tests. When a name here starts failing everywhere, check that before the SDK.
"""
import os

# Hand-picked: shops that stay live in most countries. Order matters only in
# that the first is the default when a run is pinned to a single retailer.
DEFAULT = (
    "https://www.aliexpress.com",
    "https://www.perfectlens.ca",
    "https://www.firesideoutdoor.com",
    "https://www.smallrig.com",
)


def sites() -> list:
    """Every retailer this run tests, overridable without touching the code.

    `BRING_RETAILERS` replaces the list. `BRING_RETAILER` pins the run to one
    of them, which is how a single failure is reproduced without waiting on the
    other lanes.
    """
    override = os.getenv("BRING_RETAILERS", "").strip()
    chosen = ([s.strip() for s in override.split(",") if s.strip()]
              if override else list(DEFAULT))

    pinned = os.getenv("BRING_RETAILER", "").strip()
    if pinned:
        # A bare host is accepted as well as a full URL — nobody types the
        # scheme when they are re-running one failure.
        match = [s for s in chosen if pinned in s]
        return match or [pinned if "://" in pinned else f"https://{pinned}"]
    return chosen


#: The one shop the server shows the collapsed badge on, so the widget tests
#: have somewhere to run. Kept out of `sites()` on purpose: every other test
#: wants the ordinary popup, and a retailer that answers with a badge would
#: make each of them open it first for no reason.
WIDGET_SITE = os.getenv("BRING_WIDGET_RETAILER", "https://www.ebay.com")


def primary() -> str:
    """The first retailer, for the few places that need only one."""
    return sites()[0]


def control_for(site: str) -> str:
    """A different retailer, to prove a silence did not leak onto it.

    Returns None when the run was pinned to a single retailer: the tests that
    need a real control then skip, rather than asserting that a shop did not
    silence itself, which would pass while proving nothing.
    """
    others = [s for s in sites() if s != site]
    return others[0] if others else None


def label(url: str) -> str:
    """A short name for a retailer, used in test ids and worker groups."""
    return host(url).removeprefix("www.")


def origin(url: str) -> str:
    """`https://host` for a retailer URL, for routing and for glob patterns."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/')[0]}"


def host(url: str) -> str:
    return origin(url).split("://")[1]
