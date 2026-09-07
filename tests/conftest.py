"""Fixtures every test shares: a browser with the extension, and evidence.

The environment is brought up once, by `run.py`, before pytest starts — not in
a session fixture. Under `-n` each worker is its own process, so a session
fixture would deploy once per worker, and the first thing they would each do is
find the others' half-built stack. Doing it outside pytest and passing the
result in through the environment keeps that impossible.

**One profile per lane, not per test.** A real user has one profile that
accumulates: an id, a downloaded retailer list, a warm cache, a migration that
ran once months ago. Throwing the profile away between tests would mean testing
"the very first run, ever" hundreds of times and the ordinary case never — and
first-run state is the one state a real user is almost never in. So the browser
is opened once per worker and kept.

What *is* cleared between tests is only what the previous test wrote:
quietDomains and the global opt-out. Those genuinely poison the next test — a
close silences the retailer for thirty minutes, and the next test would report
a missing popup for a product behaving exactly as designed. This is the same
reset the larger QA framework performs between its checks, and for the same
reason.

The few tests that really are about a first run ask for `fresh_context`.
"""
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
    """A profile directory nobody else is using.

    `gw0`, `gw1`… under xdist, `main` when serial — but the process id goes on
    the end either way. Without it, a second run started while the first is
    still going points Chrome at a user-data-dir that is already open, and the
    browser exits immediately with a launch error that looks nothing like the
    collision it is. Reproducing one failing test while the suite runs is
    exactly when that happens, which is exactly when it is most confusing.
    """
    worker = getattr(request.config, "workerinput", {}).get("workerid", "main")
    return f"{worker}-{os.getpid()}"


@pytest.fixture(scope="session")
def extension_dir() -> Path:
    return Path(os.environ["BRING_EXTENSION_DIR"])


@pytest.fixture(scope="session")
def env_name() -> str:
    return os.getenv("BRING_ENV_NAME", "")


def pytest_generate_tests(metafunc):
    """Run every test once per retailer.

    Retailers are deliberately *not* pinned to workers. A real user has one
    browser with one extension, and every shop they visit shares the same
    `quietDomains` list — so a worker that only ever sees one shop is testing a
    situation nobody is in, and never exercises the list holding rows for
    several retailers at once.

    So a worker's browser is shared across whichever retailers land on it, and
    the isolation that matters is done per row instead: between tests only the
    retailer just tested is forgotten, not the whole list. See the `context`
    fixture.
    """
    if "retailer" not in metafunc.fixturenames:
        return
    sites = retailers.sites()
    metafunc.parametrize("retailer", sites,
                         ids=[retailers.label(site) for site in sites])


@pytest.fixture
def widget_retailer() -> str:
    """The shop that answers with the collapsed badge.

    Only one does, so the widget tests name it rather than running every
    retailer through a surface it does not have.
    """
    return retailers.WIDGET_SITE


@pytest.fixture
def control(retailer) -> str:
    """A retailer this test must *not* affect.

    Never the one under test: proving that silencing a shop did not silence
    itself would pass and mean nothing. A run pinned to a single retailer has
    no control, and the tests that need one skip.
    """
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
    """The bot-check text this page is showing instead of its content, or ''.

    A retailer that answers with a challenge has not loaded, so the extension
    has nothing to match, no popup can appear, and every assertion about one is
    meaningless. Left undetected this reports a working product as broken —
    seventeen times in one run, when a single shop was blocked.

    Read from a short prefix of the body: a challenge page is a few lines, so
    anything longer is the shop itself, and a product page that happens to
    contain "access denied" in a review is not mistaken for one.
    """
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
    """One browser for this worker, kept for the whole lane.

    Opened once and reused, so the extension is a returning user with an id, a
    downloaded retailer list and a warm cache — which is the state a real user
    is in, and the state the first-profile-per-test design never reached.
    """
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
    """Make every navigation in this lane skip when the site serves a bot check.

    Wrapped once here rather than at each `page.goto` because there are dozens
    of those and the ones that forget are exactly the ones that report a
    Cloudflare page as a missing popup.
    """
    open_page = ctx.new_page

    async def new_page(*args, **kwargs):
        page = await open_page(*args, **kwargs)
        navigate = page.goto

        async def goto(url, **options):
            response = await navigate(url, **options)
            marker = await challenge_on(page)
            if marker:
                pytest.skip(
                    f"{url} answered with a bot check ({marker!r}) instead of "
                    f"the site, so the extension had no page to work on")
            return response

        page.goto = goto
        return page

    ctx.new_page = new_page


@pytest.fixture
async def context(request, lane):
    """The lane's browser, carrying its history, minus the last test's silences.

    Only quietDomains and the global opt-out are cleared. Everything else — the
    user id, the retailer list, the cache, the migration marker — is left to
    accumulate, because that is what a real profile does and several of the
    behaviours worth testing only exist once it has.
    """
    if current_worker(lane) is None:
        pytest.skip("the extension's service worker is gone — the browser died "
                    "earlier in this lane")

    # Only this retailer's rows, not the whole list. The browser is shared with
    # every other shop this worker tests, exactly as a real one is, so wiping
    # `quietDomains` wholesale would throw away rows that belong to a retailer
    # nobody asked about — and, once tabs overlap, rows another assertion is
    # about to read.
    #
    # The global opt-out is different: it silences everything by definition, so
    # no test wants to inherit one.
    # The control shop is cleared too, and for the opposite reason. Half the
    # scope assertions read it to prove a silence did *not* spread, so a row
    # left there by an earlier test for that shop — same browser, same lane —
    # is indistinguishable from leakage, and the test blames the product for
    # the previous test's litter. It is the shop under test's own silence that
    # must survive nothing; the control's must not exist at the start.
    site = request.getfixturevalue("retailer") if "retailer" in request.fixturenames else None
    if site:
        await storage.forget(lane, site)
        other = retailers.control_for(site)
        if other:
            await storage.forget(lane, other)
    else:
        # A test with no retailer of its own — the bar tests, which reach a shop
        # through a search and only learn which one from the answer. There is no
        # name to forget selectively, and leaving the list alone is not the safe
        # option it looks like: getQuietDomain short-circuits locally, so one
        # row from an earlier close means the next search sends no popup check
        # at all, and the test skips saying the search matched nothing. Wiping
        # is safe here because a lane runs its tests one at a time.
        await storage.delete(lane, storage.QUIET_DOMAINS)
    await storage.delete(lane, storage.OPT_OUT)

    # And the reward-check backoff, for the same reason. A successful check
    # tells the client not to ask again for a day
    # (`notificationCheck` = [now, now + nextCall]), and checkNotifications.ts
    # returns early while that range is live — so one passing test leaves every
    # later one in the lane unable to provoke a check at all. The window is
    # then read back unchanged and the failure reads "a 1440-minute backoff
    # instead of an hour", which is the previous test's number, not this
    # test's.
    await storage.delete(lane, storage.NOTIFICATION_CHECK)

    # Armed for every test, not only the ones that count calls. A silence
    # window is the server's decision, so the exact assertion is "the extension
    # stored what the server sent" — and that needs the answer recorded before
    # the test acts. Also resets the mode, so a test that made the network fail
    # cannot leave the next one talking to a dead server.
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

    # Evidence first, while the pages the test used are still open. A separate
    # autouse fixture cannot be relied on for this: pytest tears fixtures down
    # in reverse setup order, so one that does not depend on this one runs
    # *after* the tabs are gone — and the screenshot is missing from exactly
    # the failures that needed it.
    await capture(request, lane)

    # Every route the test installed goes with it. They are registered on the
    # lane's context, which outlives the test, and Playwright matches the most
    # recently registered first — so a test that answers `https://shop.com/**`
    # with its own markup keeps answering it for every later test in the lane.
    # That is not a subtle skew: the stand-down tests build a redirect chain on
    # those same URLs and were served an injection test's page instead, landing
    # the browser on the hop and reporting that the product failed to follow a
    # redirect it was never sent.
    try:
        await lane.unroute_all(behavior="ignoreErrors")
    except Exception:
        pass

    # Then close whatever the test left open, so the next one starts on a clean
    # tab rather than inheriting a silenced retailer's page — but never the
    # last one. Chrome quits when a persistent context loses its final tab, and
    # it takes the extension's service worker with it, so closing them all ends
    # the lane: every remaining test in this worker then skips with "the
    # browser died earlier", blaming a crash that was really this loop.
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
    """A brand-new profile, for the few tests that are about a first run.

    Migration, the keys written on first install, and the permission assertion
    all need an extension that has never run. Everything else should use the
    lane's browser — a fresh profile is slower and, for anything but these,
    less like a real user.
    """
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
    """A tab. Most tests need exactly one.

    Left open when the test failed. This fixture depends on `context`, so
    pytest tears it down first — and closing the tab here means the evidence
    capture that runs a moment later has nothing to photograph, which is how
    two runs' worth of failures arrived with no screenshot. The context's own
    teardown closes whatever is still open.
    """
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
    """Loaded on the retailer, with the popup already waited for.

    Returns `(page, frame)` — frame is None when no popup appeared, which is a
    finding for most tests and the expected result for a few, so it is handed
    over rather than asserted here.
    """
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
    """On a failure, write down what the browser and the extension were showing.

    A failed extension test is close to unreadable without this: the page is
    gone by the time anyone looks, and the interesting half was never on screen
    — it was in `chrome.storage.local`. So both are kept, next to the trace.
    """
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
