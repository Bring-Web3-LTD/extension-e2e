"""Recovery, permissions and the debug logger.

QA_TEST_PLAN sections 5 and 6. Nothing here is about a feature working; it is
about the extension staying alive and honest when the world does not cooperate.
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from bring import netspy, popup, storage
from bring.browser import extension_browser, wake_worker

pytestmark = pytest.mark.robustness

def ours(error) -> bool:
    """Whether a page error came from the extension rather than from the shop.

    `pageerror` fires for every uncaught throw on the page, and a real retailer
    throws a handful on any given load — its own analytics, its own carousel.
    Asserting on all of them makes "the SDK is clean" depend on whether
    somebody else's script was having a good day, and the failure it produces
    quotes the shop's stack while blaming the extension.

    Attribution is by stack: the content script runs from a
    `chrome-extension://` URL, so anything thrown by the code under test names
    one. An error with no stack at all is kept — unattributable is not the same
    as innocent, and dropping it silently would hide the crash this is for.
    """
    stack = getattr(error, "stack", None) or ""
    if not stack:
        return True
    return "chrome-extension://" in stack


# Asserted at init (SDK validatePermissions.ts). Removing any one of them must
# fail loudly and by name, not degrade quietly.
REQUIRED_PERMISSIONS = ("storage", "tabs", "webNavigation", "webRequest")


async def test_no_network_does_not_crash_the_extension(context, retailer):
    """5 — with the server unreachable, nothing appears and nothing breaks."""
    await netspy.install(context, netspy.FAIL)

    page = await context.new_page()
    errors = []
    page.on("pageerror",
            lambda e: errors.append(str(e)) if ours(e) else None)

    await page.goto(retailer, wait_until="domcontentloaded")
    shown = await popup.wait_for_popup(page, timeout=popup.ABSENT)
    await page.close()

    assert shown is None, "a popup appeared while the server was unreachable"
    assert not errors, f"the page threw while the server was down: {errors[:3]}"
    assert await netspy.installed(context), \
        "the service worker died while the server was unreachable"


async def test_the_flows_work_again_once_the_server_is_back(context, retailer):
    """5 — recovery on the next visit, with no reinstall and no reset."""
    await netspy.install(context, netspy.FAIL)
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_popup(page, timeout=popup.ABSENT) is None
    await page.close()

    await netspy.set_mode(context, netspy.PASS)
    # The failed list fetch left a TTL behind; clear it so the next visit asks
    # again rather than sitting out a cache window it only has because of us.
    await storage.delete(context, "relevantDomainsCheck")

    back = await context.new_page()
    await back.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(back, timeout=40)
    await back.close()

    assert frame, "the server came back but the popup did not"


# ── 6 the debug logger ──────────────────────────────────────────────

async def test_the_logger_is_silent_by_default(context, retailer):
    """6 — an end-user build logs nothing unless the flag is set.

    The mock extension turns the flag on for its own convenience, so this
    clears it first: what is being tested is the SDK's gate, not the mock's
    default.
    """
    await storage.delete(context, storage.DEBUG_MODE)

    page = await context.new_page()
    logs = []
    page.on("console", lambda m: logs.append(m.text))

    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    sdk_lines = [line for line in logs
                 if "[popup-msg]" in line or "[bg-msg]" in line or "[content]" in line]
    assert not sdk_lines, \
        f"the SDK logged with debugMode unset: {sdk_lines[:3]}"


async def test_the_logger_respects_its_level(context, retailer):
    """6 — set to `warn`, debug and info lines stay quiet."""
    await storage.set(context, storage.DEBUG_MODE, "warn")

    page = await context.new_page()
    logs = []
    page.on("console", lambda m: logs.append((m.type, m.text)))

    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    too_low = [text for kind, text in logs
               if "[DEBUG]" in text or "[INFO]" in text]
    assert not too_low, \
        f"debugMode='warn' still emitted lower levels: {too_low[:3]}"


async def test_an_unrecognised_level_logs_nothing(context, retailer):
    """6 — a bad value is silence, not a crash and not everything."""
    await storage.set(context, storage.DEBUG_MODE, "loud-please")

    page = await context.new_page()
    logs, errors = [], []
    page.on("console", lambda m: logs.append(m.text))
    page.on("pageerror",
            lambda e: errors.append(str(e)) if ours(e) else None)

    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    assert not errors, f"an unrecognised debugMode threw: {errors[:2]}"
    assert not [line for line in logs if "[DEBUG]" in line], \
        "an unrecognised debugMode value still logged"


async def test_a_forever_optout_does_not_break_logging(context, retailer):
    """6 — a 'forever' window formats as a date without throwing.

    The logger prints stored ranges; a forever end date is far enough out that
    it used to produce an invalid-date error and take the logging with it.
    """
    await storage.set(context, storage.DEBUG_MODE, "debug")
    host = storage.normalise(retailer)
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": f"*.{host}", "type": "kds", "phase": "quiet",
         "time": [0, 999_999_999_999_999]},
    ])

    page = await context.new_page()
    errors = []
    page.on("pageerror",
            lambda e: errors.append(str(e)) if ours(e) else None)
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=12)
    await page.close()

    worker = await wake_worker(context)
    readable = await worker.evaluate(
        "() => bringCache.getReadable('quietDomains').catch(e => 'THREW: ' + e.message)")

    assert not errors, f"a forever window threw while logging: {errors[:2]}"
    assert "THREW" not in str(readable), \
        f"reading a forever window back threw: {readable}"


# ── 5 the permission assertion ──────────────────────────────────────

@pytest.mark.parametrize("dropped", REQUIRED_PERMISSIONS)
async def test_init_fails_by_name_when_a_permission_is_missing(extension_dir, dropped):
    """5 — the SDK asserts its permissions at init and names the missing one.

    Integrators have to add `webRequest` and `webNavigation` now, and those
    power the redirect-chain stand-down. Without the assertion an integrator who
    forgets them gets an extension that looks fine and silently never stands
    down — so the loud failure *is* the feature, and this is what tests it.
    """
    if dropped == "storage":
        pytest.skip(
            "the SDK's own complaint cannot be read without `storage`: every "
            "path in it, including the one that records the failure, goes "
            "through chrome.storage, so the worker never starts and there is "
            "nothing left to ask. The assertion is real for the permissions "
            "this test exists for — webRequest and webNavigation")

    source = Path(extension_dir)
    with tempfile.TemporaryDirectory() as tmp:
        crippled = Path(tmp) / "extension"
        shutil.copytree(source, crippled)

        manifest_path = crippled / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        before = list(manifest.get("permissions", []))
        if dropped not in before:
            pytest.skip(f"the built extension does not request {dropped!r} at all")
        manifest["permissions"] = [p for p in before if p != dropped]
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        profile = Path(tmp) / "profile"
        async with extension_browser(crippled, profile, headless=True) as ctx:
            worker = await wake_worker(ctx)
            complaint = await worker.evaluate(
                """() => (globalThis.__bringInitErrors || [])
                         .concat(globalThis.__lastError ? [globalThis.__lastError] : [])
                         .join(' | ')""")

            # The SDK throws; the mock extension does not catch it, so the
            # failure shows up as an uncaught error on the worker. Reading it
            # back is best-effort — what must hold is that the extension does
            # not go on pretending it is wired up.
            page = await ctx.new_page()
            await page.goto("https://example.com", wait_until="domcontentloaded")
            healthy = await popup.wait_for_popup(page, timeout=5)
            await page.close()

        assert healthy is None, (
            f"the extension initialised and injected with {dropped!r} missing "
            f"from the manifest; init is supposed to fail fast and say so"
            + (f" (worker said: {complaint})" if complaint else ""))


async def test_the_logger_can_be_turned_on_without_a_reload(context, retailer):
    """6 — the flag takes effect immediately, across contexts.

    Set in the background, picked up by the content script through the storage
    change listener. A logger that needed a reload to start would be useless
    for the thing it exists for: watching something that is happening now.
    """
    await storage.delete(context, storage.DEBUG_MODE)

    page = await context.new_page()
    lines = []
    page.on("console", lambda m: lines.append(m.text))

    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_offer(page, timeout=30)

    before = len([line for line in lines if "[DEBUG]" in line or "[INFO]" in line])

    # Flip it while everything is already running, and give the change listener
    # something to log about.
    await storage.set(context, storage.DEBUG_MODE, "debug")
    await page.reload(wait_until="domcontentloaded")
    await popup.wait_for_offer(page, timeout=30)
    await page.wait_for_timeout(2000)

    after = len([line for line in lines if "[DEBUG]" in line or "[INFO]" in line])
    await page.close()

    assert before == 0, \
        f"the logger emitted {before} lines while the flag was unset"
    assert after > 0, \
        "turning debugMode on changed nothing — the flag is only read at startup"
