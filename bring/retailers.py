"""Which retailers this run tests against.

Two, minimum, and that is not arbitrary: close, activate and opt-out each have
to prove the silence they wrote applies to *this* retailer and not to another
one. With a single site that half of every scope assertion quietly does not
run, and the suite reports green having checked less than it claims.

A retailer here is only correct while it is live in the country the browser is
in. A shop with no offer in this country returns `isValid = false` and no
popup, which is right — and reads as a product failure if the list is stale.
When a name in here starts failing everywhere, check that before the SDK.
"""
import os

# Deliberately short and hand-picked, and the same names the QA automation
# smoke-tests: large international retailers that stay live in most countries.
DEFAULT = (
    "https://www.aliexpress.com",
    "https://www.iherb.com",
)


def sites() -> list:
    """The retailers to test, overridable without touching the code."""
    override = os.getenv("BRING_RETAILERS", "").strip()
    if override:
        return [s.strip() for s in override.split(",") if s.strip()]
    return list(DEFAULT)


def primary() -> str:
    """The retailer a test acts on."""
    return sites()[0]


def control() -> str:
    """A different retailer, for proving a silence did not leak onto it."""
    return sites()[1]


def origin(url: str) -> str:
    """`https://host` for a retailer URL, for routing and for glob patterns."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/')[0]}"


def host(url: str) -> str:
    return origin(url).split("://")[1]
