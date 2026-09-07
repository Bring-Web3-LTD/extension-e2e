"""A Chrome with the extension loaded, and a handle on its service worker.

Every test gets its own profile directory. The extension keeps its state in
`chrome.storage.local`, which is per-profile and not per-page, so two tests
sharing a browser would be writing over each other's quiet domains — and the
one that ran second would report a missing popup for a product that is fine.
A profile per test is also what makes running them in parallel safe.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import async_playwright

# Loading any page runs the content script, which is what starts a dormant MV3
# worker. Cheap, always reachable, and not a retailer — so it cannot silence one.
WAKE_URL = "https://example.com"

LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    # Chrome throttles and eventually kills MV3 workers in the background; a
    # test that waits for a reward check would otherwise be racing the browser.
    "--disable-features=DisableLoadExtensionCommandLineSwitch",
    # Chromium announces itself as automated, and a search engine answers with
    # a CAPTCHA instead of results. That is not a cosmetic problem here: the
    # offer bar only exists over search results, so a browser that cannot get
    # a results page cannot test the offer bar at all.
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    # The rest of the automation tells. Each is checked by the fingerprinting
    # scripts the big shops run, and any one of them alone is enough to be sent
    # to a challenge page instead of the shop.
    "--disable-automation",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
    "--disable-popup-blocking",
    "--start-maximized",
    "--lang=en-US,en",
]

# What an automated Chrome still admits to after the launch flags, and what a
# real one says instead. All of it is read by the same fingerprinting scripts:
# `navigator.webdriver` is the blunt one, but an empty plugin list, no language
# list and a WebGL vendor of "Google Inc." are each on their own enough to earn
# a challenge page.
#
# Run before any of the page's own code, so the page never sees the original
# values. This is not about defeating a site's security — it is about a QA
# browser being served the same shop a person is served, which is the only way
# a check of the extension means anything.
STEALTH = """
(() => {
  // The flag Playwright cannot unset from the command line alone.
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // A real profile has languages and plugins; an automated one has neither.
  Object.defineProperty(navigator, 'languages',
                        { get: () => ['en-US', 'en'] });
  Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
    ],
  });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

  // Headless Chrome reports no chrome runtime object at all.
  window.chrome = window.chrome || {};
  window.chrome.runtime = window.chrome.runtime || {};

  // Headless answers 'denied' for notifications while the real prompt is
  // 'default', and the mismatch with Notification.permission is itself a tell.
  const query = navigator.permissions && navigator.permissions.query;
  if (query) {
    navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : query.call(navigator.permissions, parameters);
  }

  // SwiftShader gives away a headless GPU; report what a real machine reports.
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Intel Inc.';                 // UNMASKED_VENDOR
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';   // UNMASKED_RENDERER
    return getParameter.call(this, parameter);
  };
})();
"""


def chromium_major() -> str:
    """The major version Playwright ships, so the user agent matches the binary.

    A user agent claiming a version the browser is not is its own tell.
    """
    try:
        import json
        import playwright

        data = json.loads(
            (Path(playwright.__file__).parent / "driver" / "package"
             / "browsers.json").read_text(encoding="utf-8"))
        for browser in data.get("browsers", []):
            if browser.get("name") == "chromium":
                return str(browser.get("browserVersion", "130")).split(".")[0]
    except Exception:
        pass
    return "130"


def real_user_agent() -> str:
    """A user agent without the two words that give the game away.

    Playwright's default contains `HeadlessChrome` even when headed, and
    `--enable-automation` sets `navigator.webdriver`. Either one is enough for
    a search engine to serve a bot check.
    """
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_major()}.0.0.0 Safari/537.36")


@asynccontextmanager
async def extension_browser(extension_dir: Path, profile_dir: Path, *,
                            headless: bool = False, trace: bool = True):
    """Yield a BrowserContext with the extension installed.

    :param headless: uses Chrome's new headless, which is the only one that
        loads extensions at all. The old one silently ran without them, which
        looks exactly like a product that stopped injecting.
    :param trace: arm tracing without writing anything. The browser outlives a
        single test, so each test records its own chunk and keeps it only if it
        failed — one trace for a whole lane would be too large to open and
        would name sixty tests at once.
    """
    extension_dir = Path(extension_dir).resolve()
    profile_dir = Path(profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    args = list(LAUNCH_ARGS) + [
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
    ]
    if headless:
        args.append("--headless=new")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,          # the flag above decides; see docstring
            args=args,
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
            user_agent=real_user_agent(),
            # Playwright adds this by default, and it is what sets
            # navigator.webdriver.
            ignore_default_args=["--enable-automation"],
            # A real profile has a locale and a timezone; the automation
            # defaults are another thing a fingerprint checks.
            locale="en-US",
            timezone_id="America/New_York",
        )

        # Before any page script runs, on every page and frame, for the life of
        # the context — including pages the product opens itself, which is where
        # the affiliate redirect lands.
        await context.add_init_script(STEALTH)

        if trace:
            try:
                await context.tracing.start(screenshots=True, snapshots=True,
                                            sources=True)
            except Exception:
                pass        # tracing is evidence, not a reason to fail a run
        try:
            await wake_worker(context)
            yield context
        finally:
            await context.close()


async def wake_worker(context, timeout: float = 30):
    """The extension's service worker, started if it was asleep.

    An MV3 worker only runs while it has something to do, so waiting for a
    registration event on a profile where the extension is already installed
    waits forever. Loading a page is what actually starts one.
    """
    worker = current_worker(context)
    if worker:
        return worker

    page = await context.new_page()
    try:
        try:
            await page.goto(WAKE_URL, wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            pass            # even a failed navigation runs the content script

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            worker = current_worker(context)
            if worker:
                return worker
            await asyncio.sleep(0.25)
    finally:
        await page.close()

    raise RuntimeError(
        "The extension's service worker never started. The extension did not "
        "load — check that the unpacked directory has a manifest.json and that "
        "the browser is not running old headless."
    )


def current_worker(context):
    """The extension's service worker right now, or None."""
    for worker in context.service_workers:
        if worker.url.startswith("chrome-extension://"):
            return worker
    return None


async def extension_id(context) -> str:
    """The id Chrome gave this unpacked extension."""
    worker = await wake_worker(context)
    return worker.url.split("/")[2]
