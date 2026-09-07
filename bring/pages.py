import asyncio
import json
import re

BASE_STYLE = "body{font-family:system-ui;padding:40px;line-height:1.6}"


def plain(title: str = "Test retailer") -> str:
    """An ordinary, quiet page. The baseline the other variants deviate from."""
    return f"""<!doctype html><html><head><title>{title}</title>
<style>{BASE_STYLE}</style></head>
<body><h1>{title}</h1><p id="content">Product listing.</p></body></html>"""


def hydration_wipe(delay_ms: int = 1200) -> str:
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
    return f"""<!doctype html><html><head><title>Framed retailer</title>
<style>{BASE_STYLE}</style></head>
<body><h1>Framed retailer</h1>
<iframe id="own-frame" src="/e2e-inner" width="600" height="200"></iframe>
</body></html>"""


def with_third_party_frame() -> str:
    return f"""<!doctype html><html><head><title>Retailer with widget</title>
<style>{BASE_STYLE}</style></head>
<body><h1>Retailer with widget</h1>
<iframe id="third-party" src="https://www.example.com/" width="400" height="150"></iframe>
</body></html>"""


def spa(routes: int = 2) -> str:
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
    async def handler(route):
        await route.fulfill(status=status, content_type="text/html; charset=utf-8",
                            body=html)

    await target.route(url_glob, handler)


async def serve_site(target, origin: str, pages: dict, default: str):
    async def handler(route):
        path = route.request.url[len(origin):].split("?")[0] or "/"
        body = pages.get(path, default)
        await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                            body=body)

    await target.route(f"{origin}/**", handler)


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
