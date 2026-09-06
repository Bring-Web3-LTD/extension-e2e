"""Controlled pages served on a real retailer's own URL.

Some of the QA plan cannot be produced by visiting a shop and hoping. A site
that rebuilds its DOM after hydration and deletes our popup (1.11), a page
carrying a captcha or ad frame that used to answer the injection request before
the top frame did (1.12), a link that reaches the retailer through an affiliate
redirect hop (1.8) — none of these are on demand at a real retailer, and two of
them only ever appear on some retailers on some days.

So the navigation stays real and the DOM does not: Playwright fulfils the
request for the retailer's own URL with markup we wrote. The extension still
sees a real navigation to a real retailer domain, still matches it against the
retailer list, and still runs webRequest and webNavigation over it — which is
the part under test — while the page itself is whatever the case needs.

Anything asserting on the retailer's *real* content must not use these.
"""
import asyncio

BASE_STYLE = "body{font-family:system-ui;padding:40px;line-height:1.6}"


def plain(title: str = "Test retailer") -> str:
    """An ordinary, quiet page. The baseline the other variants deviate from."""
    return f"""<!doctype html><html><head><title>{title}</title>
<style>{BASE_STYLE}</style></head>
<body><h1>{title}</h1><p id="content">Product listing.</p></body></html>"""


def hydration_wipe(delay_ms: int = 1200) -> str:
    """A page that replaces its whole body after load, taking our popup with it.

    This is what React and Remix hosts do when they hydrate, and it is the exact
    behaviour section 1.11's self-heal exists for: the popup appears, the host
    wipes it, and the extension must put it back — at most three times, and only
    until about two seconds after load, so it cannot turn into a flicker loop.
    """
    return f"""<!doctype html><html><head><title>Hydrating retailer</title>
<style>{BASE_STYLE}</style></head>
<body><h1>Hydrating retailer</h1><p id="content">Server markup.</p>
<script>
  window.addEventListener('load', () => setTimeout(() => {{
    // Replace the documentElement's children the way a hydrating framework
    // does — not innerHTML on body, which some frameworks leave alone.
    document.body.replaceChildren();
    const h = document.createElement('h1');
    h.textContent = 'Hydrating retailer';
    const p = document.createElement('p');
    p.id = 'content';
    p.textContent = 'Client markup.';
    document.body.append(h, p);
    window.__wiped = true;
  }}, {delay_ms}));
</script></body></html>"""


def with_same_origin_frame() -> str:
    """A retailer page that embeds its own pages in an iframe.

    The content script runs in every frame, so before the top-frame fix this
    shape produced more than one popup, or one behind another.
    """
    return f"""<!doctype html><html><head><title>Framed retailer</title>
<style>{BASE_STYLE}</style></head>
<body><h1>Framed retailer</h1>
<iframe id="own-frame" src="/e2e-inner" width="600" height="200"></iframe>
</body></html>"""


def with_third_party_frame() -> str:
    """A retailer page carrying a cross-origin widget frame.

    Stands in for hCaptcha, reCAPTCHA, ad and chat frames. The regression this
    guards is the opposite of the one above: a third-party frame answering the
    injection request first, and the top frame then never getting the popup at
    all. So the expected result here is that the popup *does* appear.
    """
    return f"""<!doctype html><html><head><title>Retailer with widget</title>
<style>{BASE_STYLE}</style></head>
<body><h1>Retailer with widget</h1>
<iframe id="third-party" src="https://www.example.com/" width="400" height="150"></iframe>
</body></html>"""


def spa(routes: int = 2) -> str:
    """A page that changes route without reloading, via history.pushState.

    Section 1.6: on an in-app route change the old popup must close before a new
    one shows, and there must never be two.
    """
    return f"""<!doctype html><html><head><title>SPA retailer</title>
<style>{BASE_STYLE}</style></head>
<body><h1>SPA retailer</h1><p id="content">Route 0</p>
<script>
  window.__go = (n) => {{
    history.pushState({{}}, '', '/e2e-route-' + n);
    document.getElementById('content').textContent = 'Route ' + n;
    window.dispatchEvent(new PopStateEvent('popstate'));
  }};
</script></body></html>"""


INNER = f"""<!doctype html><html><head><title>Inner</title>
<style>{BASE_STYLE}</style></head><body><p>Inner page.</p></body></html>"""


async def serve(target, url_glob: str, html: str, *, status: int = 200):
    """Answer every request matching *url_glob* with *html*.

    :param target: a Page or a BrowserContext. Context-wide when the case
        involves more than one tab.
    """
    async def handler(route):
        await route.fulfill(status=status, content_type="text/html; charset=utf-8",
                            body=html)

    await target.route(url_glob, handler)


async def serve_site(target, origin: str, pages: dict, default: str):
    """Serve a small site: exact paths from *pages*, everything else *default*.

    Lets one route cover a page and the frames or routes it pulls in, which is
    what the same-origin-frame and SPA cases need.
    """
    async def handler(route):
        path = route.request.url[len(origin):].split("?")[0] or "/"
        body = pages.get(path, default)
        await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                            body=body)

    await target.route(f"{origin}/**", handler)


async def redirect_through(target, entry_url: str, hops: list, final_html: str,
                           final_url: str):
    """Send *entry_url* through *hops* by real 3xx responses, then serve a page.

    The point of section 1.8's redirect-chain case is that the affiliate
    parameter sits on an intermediate hop and not on the URL the browser ends
    up at — so a clean-looking final URL is not enough for the extension to
    stay silent. Fulfilling with a 302 makes webRequest see genuine hops.
    """
    chain = [entry_url] + list(hops)

    for current, nxt in zip(chain, chain[1:] + [final_url]):
        async def handler(route, location=nxt):
            await route.fulfill(status=302, headers={"location": location}, body="")

        await target.route(current, handler)

    async def land(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                            body=final_html)

    await target.route(final_url, land)


async def wait_for(page, expression: str, timeout: float = 10) -> bool:
    """Poll a JS expression in the page until it is true, or give up."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.evaluate(f"() => !!({expression})"):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)
    return False
