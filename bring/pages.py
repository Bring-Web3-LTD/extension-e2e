import json
import re


async def serve(target, url_glob: str, html: str, *, status: int = 200):
    async def handler(route):
        await route.fulfill(status=status, content_type="text/html; charset=utf-8",
                            body=html)

    await target.route(url_glob, handler)


#: Hosts that mean the user is mid-affiliate-hop — a coupon or deals site sent
#: them here and already owns the click. Taken from the backend's own
#: `WILDFIRE_STANDDOWN_DOMAINS` (utils/affiliateIdentifiers.ts), which is what
#: the server compiles into the stand-down regex the SDK matches the redirect
#: chain against.
#:
#: This is the case the feature was built for. A click id sitting on the final
#: URL is the other half of the same list and easier to fake, but it is not the
#: hijack that was actually happening: somebody arriving from a coupon site,
#: with the popup appearing over a click that was already paid for.
STANDDOWN_HOSTS = (
    "anrdoezrs.net",
    "click.linksynergy.com",
    "awin1.com",
    "dpbolvw.net",
    "jdoqocy.com",
)


def coupon_hop(shop: str, host: str = None, index: int = 0) -> str:
    host = host or STANDDOWN_HOSTS[index % len(STANDDOWN_HOSTS)]
    return f"https://{host}/click-e2e?url={shop}"


def exactly(url: str):
    """Match one URL and nothing else, query string included."""
    return re.compile(r"^" + re.escape(url) + r"$")


async def redirect_through(target, entry_url: str, hop_url: str,
                           final_url: str):
    async def enter(route):
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=("<!doctype html><meta charset=utf-8><title>entry</title>"
                  f"<script>location.replace({json.dumps(hop_url)})</script>"))

    async def bounce(route):
        await route.fulfill(status=302, headers={"location": final_url}, body="")

    await target.route(exactly(entry_url), enter)
    await target.route(exactly(hop_url), bounce)


