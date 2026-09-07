import json
import os
import shutil
from pathlib import Path

import pytest

from bring import netspy, popup, retailers, storage, search
from bring.browser import current_worker, extension_browser

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = Path(os.getenv("BRING_ARTIFACTS", ROOT / "artifacts"))
PROFILES = ROOT / "profiles"


def pytest_configure(config):
    if not os.getenv("BRING_EXTENSION_DIR"):
        raise pytest.UsageError(
            "BRING_EXTENSION_DIR is not set. Start the suite with `python run.py`, "
            "which brings the environment up and downloads the extension it built."
        )


def headless() -> bool:
    return os.getenv("BRING_HEADLESS", "").lower() in ("1", "true", "yes")


def worker_id(request) -> str:

    worker = getattr(request.config, "workerinput", {}).get("workerid", "main")
    return f"{worker}-{os.getpid()}"


@pytest.fixture(scope="session")
def extension_dir() -> Path:
    return Path(os.environ["BRING_EXTENSION_DIR"])


@pytest.fixture(scope="session")
def env_name() -> str:
    return os.getenv("BRING_ENV_NAME", "")


def pytest_generate_tests(metafunc):
    if "retailer" not in metafunc.fixturenames:
        return
    sites = retailers.sites()
    metafunc.parametrize("retailer", sites,
                         ids=[retailers.label(site) for site in sites])


@pytest.fixture
def widget_retailer() -> str:
    return retailers.WIDGET_SITE


@pytest.fixture
def control(retailer) -> str:
    other = retailers.control_for(retailer)
    if not other:
        pytest.skip("this run is pinned to one retailer, so there is no "
                    "control to prove the silence was scoped")
    return other


# What a shop's edge serves instead of the shop when it decides the browser is
# a robot. Cloudflare's is the common one; the others are here because each
# looks exactly like a missing popup and there is nothing to distinguish them
# from a real defect except the words on the page.
CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "needs to be verified before you can proceed",
    "just a moment",
    "pardon our interruption",
    "enable javascript and cookies to continue",
    "access denied",
    "request unsuccessful",
)


async def challenge_on(page) -> str:
    # The URL first: a challenge often announces itself there before it has
    # rendered anything, and some render nothing a human would read at all.
    marker = search.blocked_url(page.url)
    if marker:
        return marker

    try:
        text = await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0, 600)")
    except Exception:
        return ""
    lowered = (text or "").strip().lower()
    if len(lowered) > 600:
        return ""
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return marker
    return ""


# ── the browser ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
async def lane(request, extension_dir):
    profile = PROFILES / worker_id(request)
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    async with extension_browser(extension_dir, profile, headless=headless()) as ctx:
        _skip_on_challenge(ctx)
        # The SDK's first-run migration writes quietDomains itself. Seeding
        # storage before it finishes gets those rows overwritten a moment
        # later, and the test then fails on a state nobody put there.
        await storage.settled(ctx)
        yield ctx

    shutil.rmtree(profile, ignore_errors=True)


def _skip_on_challenge(ctx):
    open_page = ctx.new_page

    async def new_page(*args, **kwargs):
        page = await open_page(*args, **kwargs)
        navigate = page.goto

        async def goto(url, **options):
            response = await navigate(url, **options)
            marker = await challenge_on(page)
            if marker:
                # Remembered as well as reported: the next test that needs a
                # control should not pick this shop and lose itself too.
                retailers.mark_blocked(url)
                pytest.skip(
                    f"{url} answered with a bot check ({marker!r}) instead of "
                    f"the site, so the extension had no page to work on")
            return response

        page.goto = goto
        return page

    ctx.new_page = new_page


@pytest.fixture
async def context(request, lane):
    if current_worker(lane) is None:
        pytest.skip("the extension's service worker is gone — the browser died "
                    "earlier in this lane")

    site = request.getfixturevalue("retailer") if "retailer" in request.fixturenames else None
    if site:
        await storage.forget(lane, site)
        other = retailers.control_for(site)
        if other:
            await storage.forget(lane, other)
    else:
        await storage.delete(lane, storage.QUIET_DOMAINS)
    await storage.delete(lane, storage.OPT_OUT)

    await storage.delete(lane, storage.NOTIFICATION_CHECK)

    await netspy.install(lane, netspy.PASS)
    await netspy.reset(lane)

    # A trace chunk per test: one trace for a whole lane would be unusable, and
    # unopenable at sixty tests' worth of snapshots.
    started = False
    try:
        await lane.tracing.start_chunk(title=request.node.name)
        started = True
    except Exception:
        pass

    request.node._bring_context = lane
    yield lane

    await capture(request, lane)

    try:
        await lane.unroute_all(behavior="ignoreErrors")
    except Exception:
        pass

    for tab in list(lane.pages)[1:]:
        try:
            await tab.close()
        except Exception:
            pass
    if lane.pages:
        try:
            await lane.pages[0].goto("about:blank")
        except Exception:
            pass

    if started:
        report = getattr(request.node, "_bring_report", None)
        failed = report is not None and not report.passed
        try:
            if failed:
                out = ARTIFACTS / _safe(request.node.nodeid)
                out.mkdir(parents=True, exist_ok=True)
                await lane.tracing.stop_chunk(path=str(out / "trace.zip"))
            else:
                # Discarded: a green run would otherwise leave a few megabytes
                # per test behind, and nobody opens the trace of a passing test.
                await lane.tracing.stop_chunk()
        except Exception:
            pass


@pytest.fixture
async def fresh_context(request, extension_dir):
    profile = PROFILES / f"fresh-{_safe(request.node.nodeid)}"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    async with extension_browser(extension_dir, profile, headless=headless()) as ctx:
        _skip_on_challenge(ctx)
        await storage.settled(ctx)
        request.node._bring_context = ctx
        yield ctx
        await capture(request, ctx)

    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
async def page(request, context):
    tab = await context.new_page()
    yield tab

    report = getattr(request.node, "_bring_report", None)
    if report is not None and not report.passed:
        return
    try:
        await tab.close()
    except Exception:
        pass


@pytest.fixture
async def on_retailer(page, retailer):
    await page.goto(retailer, wait_until="domcontentloaded")
    # The offer, not whatever shape the server chose to show it in: on an
    # environment with the widget enabled this opens the badge first.
    frame = await popup.wait_for_offer(page, timeout=30)
    return page, frame


# ── evidence ────────────────────────────────────────────────────────

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item._bring_report = report


async def capture(request, ctx):
    report = getattr(request.node, "_bring_report", None)
    if report is None or report.passed:
        return
    if ctx is None:
        return

    out = ARTIFACTS / _safe(request.node.nodeid)
    out.mkdir(parents=True, exist_ok=True)

    try:
        state = await storage.dump(ctx)
        (out / "storage.json").write_text(json.dumps(state, indent=2, default=str),
                                          encoding="utf-8")
    except Exception as e:
        (out / "storage-error.txt").write_text(str(e), encoding="utf-8")

    for index, tab in enumerate(ctx.pages):
        try:
            await tab.screenshot(path=str(out / f"page-{index}.png"), full_page=False)
            (out / f"page-{index}.url").write_text(tab.url or "", encoding="utf-8")
        except Exception:
            pass


def _safe(node_id: str) -> str:
    return node_id.replace("/", "_").replace("::", "__").replace(".py", "")
