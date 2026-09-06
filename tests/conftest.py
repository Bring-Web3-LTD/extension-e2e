"""Fixtures every test shares: a browser with the extension, and evidence.

The environment is brought up once, by `run.py`, before pytest starts — not in
a session fixture. Under `-n` each worker is its own process, so a session
fixture would deploy once per worker, and the first thing they would each do is
find the others' half-built stack. Doing it outside pytest and passing the
result in through the environment keeps that impossible.
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from bring import pages, popup, retailers, storage
from bring.browser import extension_browser

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = Path(os.getenv("BRING_ARTIFACTS", ROOT / "artifacts"))
PROFILES = ROOT / "profiles"


def pytest_configure(config):
    if not os.getenv("BRING_EXTENSION_DIR"):
        raise pytest.UsageError(
            "BRING_EXTENSION_DIR is not set. Start the suite with `python run.py`, "
            "which brings the environment up and downloads the extension it built."
        )


@pytest.fixture(scope="session")
def extension_dir() -> Path:
    return Path(os.environ["BRING_EXTENSION_DIR"])


@pytest.fixture(scope="session")
def env_name() -> str:
    return os.getenv("BRING_ENV_NAME", "")


@pytest.fixture
def retailer() -> str:
    """The retailer this test acts on."""
    return retailers.primary()


@pytest.fixture
def control() -> str:
    """A retailer this test must *not* affect."""
    return retailers.control()


@pytest.fixture
async def context(request, extension_dir):
    """A fresh browser, with the extension, for exactly this test.

    A profile per test rather than per session. The extension's state is
    per-profile, so two tests sharing one would overwrite each other's quiet
    domains — and the second would report a missing popup for a product that is
    behaving correctly.
    """
    name = _safe(request.node.nodeid)
    profile = PROFILES / name
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)

    trace = ARTIFACTS / name / "trace.zip"
    headless = os.getenv("BRING_HEADLESS", "").lower() in ("1", "true", "yes")

    async with extension_browser(extension_dir, profile, headless=headless,
                                 trace_to=trace) as ctx:
        # The SDK's first-run migration writes quietDomains itself. Seeding
        # storage before it finishes gets those rows overwritten a moment
        # later, and the test then fails on a state nobody put there.
        await storage.settled(ctx)
        request.node._bring_context = ctx
        yield ctx

    # The trace is only worth keeping when something went wrong; a green run
    # would otherwise leave a few megabytes per test behind.
    report = getattr(request.node, "_bring_report", None)
    if report is None or report.passed:
        shutil.rmtree(ARTIFACTS / name, ignore_errors=True)
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
async def page(context):
    """A tab. Most tests need exactly one."""
    tab = await context.new_page()
    yield tab
    try:
        await tab.close()
    except Exception:
        pass


@pytest.fixture
async def on_retailer(page, retailer):
    """Loaded on the retailer, with the popup already waited for.

    Returns `(page, frame)` — frame is None when no popup appeared, which is a
    finding for most tests and the expected result for a few, so it is handed
    over rather than asserted here.
    """
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    return page, frame


# ── evidence ────────────────────────────────────────────────────────

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item._bring_report = report


@pytest.fixture(autouse=True)
async def capture_on_failure(request):
    """On a failure, write down what the browser and the extension were showing.

    A failed extension test is close to unreadable without this: the page is
    gone by the time anyone looks, and the interesting half was never on screen
    — it was in `chrome.storage.local`. So both are kept, next to the trace.
    """
    yield

    report = getattr(request.node, "_bring_report", None)
    if report is None or report.passed:
        return
    ctx = getattr(request.node, "_bring_context", None)
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
