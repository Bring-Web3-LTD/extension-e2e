"""The offer bar: a strip over Google's results, not a popup on a shop.

QA_TEST_PLAN section 2. The same offer as the popup, shown as a bar and without
the wallet-connect option — but reached completely differently. Nobody visits a
retailer to see it. You search, and if what you typed matches a term the backend
has registered, the bar appears over the results.

So these tests are **not** parametrised by retailer. The retailer is whatever
the server decides the search belongs to; the input is the *keyword*, and only
keywords the backend actually carries produce a bar. `condor` is the one known
to work here — a shop's own name usually is not registered, and a search that
produces no bar is then correct behaviour rather than a finding.

Two things make this the most fragile file in the suite, and both are handled
rather than left to chance. Google answers an obviously-automated browser with a
bot check instead of results, which the launch options in bring/browser.py hide.
And "no bar appeared" covers both a broken bar and an unregistered keyword,
which `netspy.server_said_offerbar` tells apart by asking what the server
actually decided.
"""
import os
import time

import pytest

from bring import netspy, popup, search, storage

pytestmark = pytest.mark.offerbar

MINUTE = 60_000

# Google. Amazon is in the backend's SEARCH_ENGINE_ENTITIES too, but the bar is
# not live on it, so a search there proves nothing.
ENGINE = os.getenv("BRING_SEARCH_ENGINE", "google")
ENGINE_HOME = search.ENGINES[ENGINE]["home"]

# Terms the backend has registered against a retailer. Hand-written, and that is
# a real liability: a term goes stale the moment the catalogue changes, and then
# every test here skips while appearing to have run. The skip messages say which
# of the two happened, so a stale list announces itself rather than hiding.
KEYWORDS = [k.strip() for k in
            os.getenv("BRING_OFFERBAR_KEYWORDS", "c ondor").split(",") if k.strip()]


@pytest.fixture(params=KEYWORDS)
def keyword(request) -> str:
    return request.param


async def open_bar(page, context, term):
    """Search for *term* and return the bar, or skip with the reason there is none."""
    reason = await search.search(page, term, engine=ENGINE)
    if reason:
        pytest.skip(reason)

    frame = await popup.wait_for_popup(page, timeout=20, route="offerbar")
    if frame:
        return frame

    decided = await netspy.server_said_offerbar(context)
    if decided is None:
        pytest.skip(
            f"searching {term!r} never reached a popup check — the results page "
            f"did not match anything the extension watches, so this says nothing "
            f"about the bar")
    if decided is False:
        pytest.skip(
            f"the server was asked about {term!r} and did not ask for an offer "
            f"bar — the term is not registered against a retailer here")

    pytest.fail(f"the server asked for an offer bar on {term!r} and none appeared")


def retailer_row(rows):
    """The quiet row that is not the search engine's, if there is one."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain", ""))
        if domain and "google" not in domain and "amazon" not in domain:
            return row
    return None


async def test_the_bar_appears_over_search_results(page, context, keyword):
    """2 — searching a registered term puts the offer over the results."""
    frame = await open_bar(page, context, keyword)

    body = await popup.body_text(frame)
    assert body.strip(), "the offer bar rendered an empty frame"
    assert await popup.visible(frame, popup.OFFERBAR["activate"]), \
        f"the bar for {keyword!r} has no activate button"


async def test_the_bar_offers_no_wallet_connect(page, context, keyword):
    """2 — the bar carries the offer; the wallet flow belongs to the popup."""
    frame = await open_bar(page, context, keyword)

    assert not await popup.visible(frame, popup.OFFER["connect_wallet"]), \
        "the offer bar is showing the wallet-connect control"


async def test_only_one_bar_at_a_time(page, context, keyword):
    """2 — one bar, however many frames a results page carries.

    A results page is full of iframes and the content script runs in all of
    them, so without the top-frame-only guard this is where duplicates appear.
    """
    await open_bar(page, context, keyword)

    bars = popup.frames(page, "offerbar")
    assert len(bars) == 1, f"{len(bars)} offer bars on one results page"


async def test_the_page_makes_room_and_gets_it_back(page, context, keyword):
    """2 — the bar pushes the results down, and gives the space back on close.

    A bar that overlays the results hides them; one that reserves space and then
    keeps it leaves a gap on a page the user is still reading.
    """
    frame = await open_bar(page, context, keyword)

    if not await popup.visible(frame, popup.OFFERBAR["spacer"]):
        pytest.skip("this bar reserves no space, so there is none to give back")

    before = await page.evaluate("() => document.documentElement.scrollHeight")

    closed = (await popup.click(frame, popup.OFFERBAR["close_top"], settle=2)
              or await popup.click(frame, popup.OFFERBAR["close_bottom"], settle=2))
    assert closed, "the bar has no close control"
    assert await popup.wait_for_gone(page, timeout=10, route="offerbar"), \
        "the bar is still on the page after closing it"

    after = await page.evaluate("() => document.documentElement.scrollHeight")
    assert after <= before, \
        f"the page kept the {before - after}px the bar had reserved"


async def test_closing_the_bar_silences_the_retailer_not_the_engine(
        page, context, keyword):
    """2 — the silence lands on the shop, not on Google.

    The one that matters most here. The bar was shown over a search page, so the
    naive implementation silences `google.com` — which would stop every future
    offer bar for every retailer, while leaving the one just dismissed free to
    keep appearing.
    """
    frame = await open_bar(page, context, keyword)

    closed = (await popup.click(frame, popup.OFFERBAR["close_top"], settle=2)
              or await popup.click(frame, popup.OFFERBAR["close_bottom"], settle=2))
    assert closed, "the bar has no close control"

    rows = await storage.quiet_domains(context)
    assert rows, "closing the bar wrote nothing to quietDomains"

    engine = storage.entry_for(rows, ENGINE_HOME)
    assert not engine, (
        f"closing the bar silenced the search engine ({engine.get('domain')!r}); "
        f"no offer bar would ever appear again")

    shop = retailer_row(rows)
    assert shop, (
        f"closing the bar silenced nothing that looks like a retailer — rows: "
        f"{[r.get('domain') for r in rows if isinstance(r, dict)]}")


async def test_activating_from_the_bar_silences_the_retailer(page, context, keyword):
    """2 — activation from the bar behaves like the popup's, on the shop."""
    frame = await open_bar(page, context, keyword)

    assert await popup.click(frame, popup.OFFERBAR["activate"], settle=6), \
        "the bar has no activate button"

    rows = await storage.quiet_domains(context)
    shop = retailer_row(rows)
    assert shop, f"activating from the bar wrote no retailer row: {rows}"
    assert shop.get("phase") == "activated", \
        f"expected phase 'activated', got {shop.get('phase')!r}"

    engine = storage.entry_for(rows, ENGINE_HOME)
    assert not engine, \
        f"activating from the bar silenced the search engine ({engine.get('domain')!r})"


async def test_the_bar_has_an_opt_out(page, context, keyword):
    """2 — opt-out is reachable from the bar, as it is from the popup."""
    frame = await open_bar(page, context, keyword)

    assert await popup.visible(frame, popup.OFFERBAR["optout"]), \
        "the offer bar has no opt-out control"
    assert await popup.click(frame, popup.OFFERBAR["optout"], settle=2)
    assert await popup.visible(frame, popup.OPTOUT["card"]), \
        "the bar's opt-out control did not open the opt-out screen"


async def test_a_keyword_search_is_reported_as_a_keyword(page, context, keyword):
    """2 — the bar's analytics carry the server's triggerType.

    `keyword` for a search, `domain` for a shop visit. Getting it wrong breaks
    nothing a user sees, which is exactly why it needs a test: it quietly
    corrupts the numbers the offer bar is judged on.
    """
    analytics = netspy.PageCalls()
    await analytics.watch(context, "**/analytics")

    await open_bar(page, context, keyword)
    await page.wait_for_timeout(2500)

    events = analytics.events()
    if not events:
        pytest.skip("no analytics events were sent while the bar was shown")

    triggers = {e.get("triggerType") for e in events
                if isinstance(e, dict) and e.get("triggerType")}
    assert triggers, f"the bar's analytics carried no triggerType: {events[:2]}"
    assert triggers == {"keyword"}, \
        f"a keyword search should report triggerType 'keyword'; got {triggers}"


async def test_a_stood_down_retailer_gets_no_bar(page, context, keyword):
    """1.8 + 2 — hijack protection applies to the bar as much as the popup.

    Which shop the keyword belongs to is learned from the bar itself rather than
    assumed: guessing what `condor` resolves to would be writing the catalogue
    into the test, and the catalogue is exactly the thing that changes.
    """
    frame = await open_bar(page, context, keyword)

    assert (await popup.click(frame, popup.OFFERBAR["close_top"], settle=2)
            or await popup.click(frame, popup.OFFERBAR["close_bottom"], settle=2)), \
        "the bar has no close control"

    shop = retailer_row(await storage.quiet_domains(context))
    if not shop:
        pytest.skip("could not learn which retailer this keyword belongs to")

    now = int(time.time() * 1000)
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": shop["domain"], "type": "kdi", "phase": "quiet",
         "time": [now, now + 2 * 60 * MINUTE]},
    ])

    reason = await search.search(page, keyword, engine=ENGINE)
    if reason:
        pytest.skip(reason)

    again = await popup.wait_for_popup(page, timeout=15, route="offerbar")
    assert again is None, (
        f"an offer bar appeared for {keyword!r} on {shop['domain']}, which is "
        f"stood down")
