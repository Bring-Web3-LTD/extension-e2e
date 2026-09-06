"""Count and steer the extension's own API calls, from inside its worker.

The calls under test are made by the MV3 service worker, not by a page, so
route interception is the wrong tool: whether Playwright sees a worker's
requests depends on the browser build, and a counter that silently records
nothing turns "the fix works" and "the test is broken" into the same green.

Wrapping `fetch` inside the worker is exact instead. It counts what the SDK
actually sent, and the same wrapper can make a call fail, answer with markup
instead of JSON, or answer with an error body that carries no `nextCall` —
which is precisely the set of failures section 3.1 is about, and none of them
can be produced by asking the real server nicely.

The wrapper lives on the worker's global scope, so it is lost if Chrome
recycles the worker. Re-install before asserting rather than assuming it
survived; `installed()` says whether it is still there.
"""
import json

from bring.browser import wake_worker

PASS = "pass"              # let it through, just count it
FAIL = "fail"              # reject, as a dead network would
NON_JSON = "nonjson"       # 200 with markup — a WAF block page
ERROR_BODY = "errorbody"   # valid JSON error, and no nextCall in it

_INSTALL = """
(mode) => {
  if (!globalThis.__bringSpy) {
    globalThis.__bringSpy = { calls: [], mode: 'pass', real: globalThis.fetch };
    globalThis.fetch = async (input, init) => {
      const spy = globalThis.__bringSpy;
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      spy.calls.push({ url, method: (init && init.method) || 'GET', at: Date.now() });

      if (spy.mode === 'fail') {
        throw new TypeError('Failed to fetch');
      }
      if (spy.mode === 'nonjson') {
        return new Response('<html><body>Blocked</body></html>',
                            { status: 200, headers: { 'content-type': 'text/html' } });
      }
      if (spy.mode === 'errorbody') {
        return new Response(JSON.stringify({ status: 500, message: 'e2e injected error' }),
                            { status: 500, headers: { 'content-type': 'application/json' } });
      }
      return spy.real(input, init);
    };
  }
  globalThis.__bringSpy.mode = mode;
  return true;
}
"""


async def install(context, mode: str = PASS):
    """Start counting the worker's requests, in *mode*."""
    worker = await wake_worker(context)
    await worker.evaluate(_INSTALL, mode)


async def installed(context) -> bool:
    """Whether the wrapper is still on this worker.

    False means Chrome recycled the worker and the counts since then are lost —
    which a test must not read as "no calls were made".
    """
    worker = await wake_worker(context)
    return bool(await worker.evaluate("() => !!globalThis.__bringSpy"))


async def set_mode(context, mode: str):
    """Change how the next calls are answered, keeping the counts so far."""
    worker = await wake_worker(context)
    await worker.evaluate(
        "(mode) => { if (globalThis.__bringSpy) globalThis.__bringSpy.mode = mode; }",
        mode)


async def calls(context, contains: str = "") -> list:
    """Every request the worker made, optionally only those matching *contains*."""
    worker = await wake_worker(context)
    seen = await worker.evaluate("() => (globalThis.__bringSpy || {}).calls || []")
    return [c for c in seen if contains in (c.get("url") or "")]


async def count(context, contains: str = "") -> int:
    return len(await calls(context, contains))


async def reset(context):
    """Forget the calls so far, keeping the wrapper and the mode."""
    worker = await wake_worker(context)
    await worker.evaluate(
        "() => { if (globalThis.__bringSpy) globalThis.__bringSpy.calls = []; }")


# ── the endpoints worth naming ──────────────────────────────────────

NOTIFICATION_CHECK = "/check/notification"
POPUP_CHECK = "/check/popup"
DOMAINS = "/domains"


async def broadcast_wallet(page, address: str):
    """Announce a wallet address the way a real wallet page does.

    Wallets re-broadcast from every frame on every page load, which is what
    section 3.1's dedup exists for. The mock extension keeps the address under
    its own unprefixed `walletAddress` key and reads it when the page fires
    `BRING:WALLET_UPDATED`, so setting one and firing the other is the genuine
    path — not a message injected halfway down it.
    """
    worker = await wake_worker(page.context)
    if address:
        await worker.evaluate("(a) => chrome.storage.local.set({ walletAddress: a })",
                              address)
    else:
        await worker.evaluate("() => chrome.storage.local.remove('walletAddress')")

    await page.evaluate(
        "() => window.dispatchEvent(new CustomEvent('BRING:WALLET_UPDATED', "
        "{ detail: {} }))")
