import os

# Hand-picked: shops that stay live in most countries. Three, one per opt-out
# duration, so a single pass covers every choice. Order matters only in that
# the first is the default when a run is pinned to a single retailer.
DEFAULT = (
    "https://www.luminskin.com",
    "https://www.perfectlens.ca",
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


def label(url: str) -> str:
    """A short name for a retailer, used in test ids and worker groups."""
    return host(url).removeprefix("www.")


def origin(url: str) -> str:
    """`https://host` for a retailer URL, for routing and for glob patterns."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/')[0]}"


def host(url: str) -> str:
    return origin(url).split("://")[1]
