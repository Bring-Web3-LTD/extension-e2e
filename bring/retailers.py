import os

# Hand-picked: shops that stay live in most countries. Order matters only in
# that the first is the default when a run is pinned to a single retailer.
DEFAULT = (
    "https://www.luminskin.com",
    "https://www.perfectlens.ca",
    "https://www.firesideoutdoor.com",
    "https://www.smallrig.com",
)


def sites() -> list:
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


#: Shops that answered with a bot check this run. A blocked shop is still a
#: fine subject — its own tests skip and say so — but it is a terrible
#: *control*, because every test that merely navigates to it to prove a silence
#: did not spread lands on the challenge page and skips too. Measured: one
#: blocked shop took twelve checks off the three healthy ones with it.
_blocked = set()


def mark_blocked(site: str) -> None:
    """Remember that *site* served a bot check, so it stops being chosen."""
    if site:
        _blocked.add(host(site))


def is_blocked(site: str) -> bool:
    return host(site) in _blocked


def control_for(site: str) -> str:

    others = [s for s in sites() if s != site]
    healthy = [s for s in others if not is_blocked(s)]
    return (healthy or others or [None])[0]


def label(url: str) -> str:
    """A short name for a retailer, used in test ids and worker groups."""
    return host(url).removeprefix("www.")


def origin(url: str) -> str:
    """`https://host` for a retailer URL, for routing and for glob patterns."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/')[0]}"


def host(url: str) -> str:
    return origin(url).split("://")[1]
