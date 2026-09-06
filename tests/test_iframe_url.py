"""The iframe URL: built by the server, completed by the client, token hidden.

QA_TEST_PLAN section 1.10. The server now builds the URL and the client only
fills in what was left out — and it moves the token into the fragment, so it
never travels in a query string that CDNs and proxies log.
"""
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from bring import popup

pytestmark = pytest.mark.popup

# What the content script adds when the server did not already set it
# (SDK injectIFrame.ts).
CLIENT_PARAMS = ("extensionId", "v", "themeMode", "textMode", "switchWallet",
                 "styleUrl", "userId")


async def src_of(page):
    src = await popup.iframe_src(page)
    assert src, "the injected iframe has no src"
    return urlparse(src), src


async def test_token_rides_in_the_fragment(on_retailer):
    """1.10 — `#token=…`, never `?token=`.

    A token in the query is logged by every CDN, proxy and browser history on
    the way; in the fragment it never leaves the browser.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    parts, src = await src_of(page)

    query = parse_qs(parts.query)
    assert "token" not in query, \
        f"the verify token is in the query string: {src[:160]}"

    fragment = parse_qs(parts.fragment)
    assert fragment.get("token"), \
        f"no token in the iframe URL fragment: {src[:160]}"
    assert unquote(fragment["token"][0]).strip(), "the token in the fragment is empty"


async def test_client_fills_in_only_what_the_server_left_out(on_retailer):
    """1.10 — the client adds its params, and the URL still points at the env.

    The server owns the path and its own query; the client contributes the
    handful of values only it knows. A server-set value wins, which is why this
    asserts presence rather than exact contents.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    parts, src = await src_of(page)

    query = parse_qs(parts.query)
    for name in ("extensionId", "v"):
        assert name in query, \
            f"the client did not add {name!r} to the iframe URL: {src[:200]}"

    assert parts.scheme in ("https", "http"), f"odd iframe scheme in {src[:120]}"
    assert parts.path and parts.path != "/", \
        f"the server-built path is missing from the iframe URL: {src[:160]}"


async def test_the_iframe_points_at_the_environment_under_test(on_retailer, env_name):
    """1.10 — the popup UI comes from this environment, not from production.

    An environment deployed without its own frontend serves whatever CloudFront
    falls back to, and the run then reports on an API and a UI from different
    places — which passes, and means nothing.
    """
    page, frame = on_retailer
    assert frame, "no popup appeared"
    _, src = await src_of(page)

    if not env_name:
        pytest.skip("no environment name to look for")
    assert env_name in src, (
        f"the iframe is not served from '{env_name}': {src[:200]}\n"
        f"The environment was probably deployed with SKIP_FRONTEND, so the UI "
        f"under test is production's.")


async def test_the_frame_actually_loaded_from_that_url(on_retailer):
    """1.10 — the src is not enough; the app inside it has to have verified."""
    page, frame = on_retailer
    assert frame, "no popup appeared"

    body = await popup.body_text(frame)
    assert body.strip(), \
        "the iframe URL was built but the app inside rendered nothing — the " \
        "token did not verify"
