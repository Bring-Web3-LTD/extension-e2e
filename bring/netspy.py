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


async def await_call(context, contains: str, timeout: float = 10) -> int:
    """Wait until the worker has made a matching request; return how many.

    For the tests whose subject is "a request happened". Sleeping and counting
    asks the same question with a guess attached, and the guess is what fails
    when a shop or the server is a second slower than usual.
    """
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        seen = len(await calls(context, contains))
        if seen or asyncio.get_event_loop().time() >= deadline:
            return seen
        await asyncio.sleep(0.25)


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
    matches = [c for c in await calls(context, contains) if c.get("body")]
    return matches[-1]["body"] if matches else None


class PageCalls:
   
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

    body = await last_body(context, POPUP_CHECK)
    if not isinstance(body, dict):
        return None
    # `framed` is the top-bar layout and arrives alongside `isOfferBar`; the
    # SDK prefers it. Either one means a bar was asked for.
    return bool(body.get("isOfferBar") or body.get("framed"))


async def set_host_wallet(context, address: str):
    worker = await wake_worker(context)
    if address:
        await worker.evaluate("(a) => chrome.storage.local.set({ walletAddress: a })",
                              address)
    else:
        await worker.evaluate("() => chrome.storage.local.remove('walletAddress')")


async def broadcast_wallet(page, address: str):
    worker = await wake_worker(page.context)
    if address:
        await worker.evaluate("(a) => chrome.storage.local.set({ walletAddress: a })",
                              address)
    else:
        await worker.evaluate("() => chrome.storage.local.remove('walletAddress')")

    await page.evaluate(
        "() => window.dispatchEvent(new CustomEvent('BRING:WALLET_UPDATED', "
        "{ detail: {} }))")
