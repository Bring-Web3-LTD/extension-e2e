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
    // Bound to globalThis. An unbound reference is called with `this` set to
    // the spy object, and fetch throws 'Illegal invocation' — which the SDK
    // catches as a network failure, so the popup silently stops appearing
    // and the recorder reports a call that never actually went out.
    globalThis.__bringSpy = { calls: [], errors: [],
                              mode: 'pass',
                              real: globalThis.fetch.bind(globalThis) };
    globalThis.fetch = async (input, init) => {
      const spy = globalThis.__bringSpy;
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const call = { url, method: (init && init.method) || 'GET', at: Date.now(),
                     status: null, body: null };
      spy.calls.push(call);

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

      const response = await spy.real(input, init);
      call.status = response.status;
      // Read a clone, and do NOT wait for it. Awaiting the body here puts the
      // recorder inside the SDK's critical path: measured, it delayed the
      // popup check enough that the popup stopped appearing at all, so the
      // instrumentation was changing the behaviour it existed to observe.
      // Fire-and-forget fills `body` in a moment later, which every reader
      // here is happy with.
      try {
        response.clone().json()
          .then(b => { call.body = b; })
          .catch(() => { call.body = null; });   // not JSON, which some tests are about
      } catch (e) { /* body already consumed or unclonable */ }
      return response;
    };
    // Anything the wrapper itself breaks is recorded rather than swallowed:
    // instrumentation that changes the behaviour it observes is worse than
    // none, and this is how it announces itself.
    const wrapped = globalThis.fetch;
    globalThis.fetch = async (input, init) => {
      try { return await wrapped(input, init); }
      catch (e) {
        globalThis.__bringSpy.errors.push(String(e && e.message || e));
        throw e;
      }
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


async def errors(context) -> list:
    """Anything the wrapper itself threw. Should always be empty."""
    worker = await wake_worker(context)
    return await worker.evaluate("() => (globalThis.__bringSpy || {}).errors || []")


async def reset(context):
    """Forget the calls so far, keeping the wrapper and the mode."""
    worker = await wake_worker(context)
    await worker.evaluate(
        "() => { if (globalThis.__bringSpy) globalThis.__bringSpy.calls = []; }")


# ── the endpoints worth naming ──────────────────────────────────────

NOTIFICATION_CHECK = "/check/notification"
POPUP_CHECK = "/check/popup"
DOMAINS = "/domains"


async def last_body(context, contains: str):
    """What the server answered the most recent matching call, parsed.

    The point of recording it: a silence window is the server's decision, so
    "roughly half an hour" is the wrong assertion — it passes for a client that
    stores a value it invented. Comparing against the number the server
    actually sent is exact, and survives the server retuning it.
    """
    matches = [c for c in await calls(context, contains) if c.get("body")]
    return matches[-1]["body"] if matches else None


class PageCalls:
    """Requests made by the *pages*, which the worker's wrapper never sees.

    The iframe sends its own analytics — that is where `triggerType` lives, and
    section 2 turns on it being `keyword` for a search and `domain` otherwise.
    Those go out from a frame, so they are collected by routing rather than by
    the worker wrapper. Two mechanisms because there are genuinely two callers.
    """

    def __init__(self):
        self.seen = []

    async def watch(self, target, pattern: str):
        async def handler(route):
            request = route.request
            body = None
            try:
                body = request.post_data_json
            except Exception:
                pass
            self.seen.append({"url": request.url, "method": request.method,
                              "body": body})
            await route.continue_()

        await target.route(pattern, handler)

    def bodies(self, contains: str = ""):
        return [c["body"] for c in self.seen
                if c.get("body") and contains in c["url"]]

    def events(self):
        """Every analytics event, flattened out of single and batched posts."""
        found = []
        for body in self.bodies():
            if not isinstance(body, dict):
                continue
            batch = body.get("events")
            found.extend(batch if isinstance(batch, list) else [body])
        return found


async def server_quiet_ms(context):
    """The silence window the last popup check told the client to write, or None."""
    body = await last_body(context, POPUP_CHECK)
    if not isinstance(body, dict):
        return None
    value = body.get("time")
    return int(value) if isinstance(value, (int, float)) else None


async def server_said_offerbar(context):
    """What the last popup check decided about the offer bar.

    Three answers, and they are not the same:
      True   the server asked for a bar — a missing one is a finding
      False  the server was asked and said popup, or no offer here
      None   the server was never asked, so nothing can be concluded

    Without this, "no bar appeared" covers both a broken bar and a search term
    the backend never registered against this retailer, and a suite that cannot
    tell them apart either invents failures or hides them.
    """
    body = await last_body(context, POPUP_CHECK)
    if not isinstance(body, dict):
        return None
    return bool(body.get("isOfferBar"))


async def set_host_wallet(context, address: str):
    """Set or clear the wallet the *host extension* holds, without announcing it.

    Two keys hold a wallet and they are not the same. `bring_walletAddress` is
    the SDK's copy; plain `walletAddress` belongs to the extension the SDK is
    embedded in, and that is the one `getWalletAddress(tabId)` asks the content
    script for on every navigation. Clearing only the first leaves the second
    in place, and the next page load quietly checks with the old wallet — which
    is how a wallet-less notification variant came back showing the
    wallet-connected screen.

    No event is fired: use this to put the state right *before* a navigation,
    and `broadcast_wallet` when the announcement itself is what is under test.
    """
    worker = await wake_worker(context)
    if address:
        await worker.evaluate("(a) => chrome.storage.local.set({ walletAddress: a })",
                              address)
    else:
        await worker.evaluate("() => chrome.storage.local.remove('walletAddress')")


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
