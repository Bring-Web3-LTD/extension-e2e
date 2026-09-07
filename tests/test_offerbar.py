"""The bar: a strip over Google's results, not a popup on a shop.

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

**The bar has two layouts and they share no selectors.** The server answers a
search with `isOfferBar`, with `framed`, or with both; the SDK prefers `framed`
when both are present (handleTabEvents.ts), so the surface served here is
usually `/framed` — the top bar, whose ids are `#tb-*`, not `#offerbar-*`. Which
one arrives is the server's decision and not what these tests are about, so
every test asks the frame for its own control set instead of assuming. Looking
for the wrong ids does not fail loudly: the frame is found, the buttons are not,
and a healthy bar is reported as missing.
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
    """Search for *term*; return the bar and the controls it uses.

    Two values because the caller needs both and only the frame knows the
    second: `(frame, ctl)` where `ctl` is the selector map for the layout that
    actually arrived.
    """
    reason = await search.search(page, term, engine=ENGINE)
    if reason:
        pytest.skip(reason)

    frame = await popup.wait_for_bar(page, timeout=25)
    if frame:
        return frame, await popup.controls_for(frame)

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

    pytest.fail(f"the server asked for a bar on {term!r} and none appeared")


async def search_again(page, term) -> bool:
    """Search *term* once more; True if a bar came back.

    Every silence in this file is checked this way before its stored row is
    read. The row is the reason, not the behaviour — a correct-looking entry
    with the bar still appearing is the failure that matters, and asserting on
    storage alone cannot see it.
    """
    reason = await search.search(page, term, engine=ENGINE)
    if reason:
        pytest.skip(f"could not search again to check the silence held: {reason}")
    return await popup.wait_for_bar(page, timeout=12) is not None


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
    frame, ctl = await open_bar(page, context, keyword)

    body = await popup.body_text(frame)
    assert body.strip(), "the bar rendered an empty frame"
    assert await popup.visible(frame, ctl["activate"]), \
        f"the bar for {keyword!r} has no activate button"


async def test_the_bar_offers_no_wallet_connect(page, context, keyword):
    """2 — the bar carries the offer; the wallet flow belongs to the popup."""
    frame, _ = await open_bar(page, context, keyword)

    assert not await popup.visible(frame, popup.OFFER["connect_wallet"]), \
        "the bar is showing the wallet-connect control"


async def test_only_one_bar_at_a_time(page, context, keyword):
    """2 — one bar, however many frames a results page carries.

    A results page is full of iframes and the content script runs in all of
    them, so without the top-frame-only guard this is where duplicates appear.
    """
    await open_bar(page, context, keyword)

    found = popup.bars(page)
    assert len(found) == 1, f"{len(found)} bars on one results page"


async def test_the_page_makes_room_and_gets_it_back(page, context, keyword):
    """2 — the bar pushes the results down, and gives the space back on close.

    A bar that overlays the results hides them; one that reserves space and then
    keeps it leaves a gap on a page the user is still reading.

    Measured on the mechanism, not on the page's height. `scrollHeight` was the
    obvious choice and is the wrong one: a results page keeps growing while it
    lazy-loads, so the number after the close is larger than the one before it
    for reasons that have nothing to do with the bar, and a correct product
    fails. The top bar reserves by writing `width`/`height`/`transform` onto
    `body` with `!important` (resizePage.ts) and restoring them on cleanup, so
    those three properties answer the question exactly.
    """
    frame, ctl = await open_bar(page, context, keyword)

    while_open = await page.evaluate(popup.BODY_RESERVATION)
    if ctl is popup.TOPBAR:
        assert while_open["transform"], \
            "the top bar did not push the page down; it is covering the results"
    elif not await popup.visible(frame, ctl["spacer"]):
        pytest.skip("this bar reserves no space, so there is none to give back")

    closed = (await popup.click(frame, ctl["close_top"], settle=2)
              or await popup.click(frame, ctl["close_bottom"], settle=2))
    assert closed, "the bar has no close control"
    assert await popup.bars_gone(page, timeout=10), \
        "the bar is still on the page after closing it"

    after = await page.evaluate(popup.BODY_RESERVATION)
    assert not after["transform"], \
        (f"the bar closed but the page is still pushed down "
         f"({after['transform']!r}) — the reserved space was never given back")


async def test_closing_the_bar_silences_the_engine_it_was_shown_on(
        page, context, keyword):
    """2 — dismissing the bar quiets bars on that search engine for half an hour.

    Deliberate, and worth stating because the opposite is the intuitive guess:
    the bar belongs to the search, not to the shop, so closing it says "no bars
    on my searches for a while" rather than "never offer me this retailer".
    Both bar layouts send exactly that (`Framed.tsx`, `Offerbar.tsx`).

    Asserted against the engine this run actually searched, not against a name.
    Both layouts hard-code `domain: ['google.com']` on close while
    `searchEngineDomain` sits unused beside it, so on any other engine the
    silence lands on Google — the bar keeps reappearing where it was dismissed,
    and stops appearing somewhere the user never touched. On Google the two
    coincide and this passes; point the suite at another engine and it fails,
    which is the point.
    """
    frame, ctl = await open_bar(page, context, keyword)

    closed = (await popup.click(frame, ctl["close_top"], settle=2)
              or await popup.click(frame, ctl["close_bottom"], settle=2))
    assert closed, "the bar has no close control"

    # The behaviour first: search again and the bar must stay away.
    assert not await search_again(page, keyword), \
        f"the bar came back on the next search for {keyword!r} after being closed"

    # Then the reason it stayed away, and for how long.
    rows = await storage.quiet_domains(context)
    assert rows, "closing the bar wrote nothing to quietDomains"

    engine = storage.entry_for(rows, ENGINE_HOME)
    assert engine, (
        f"the bar stayed away, but nothing in quietDomains says why — rows: "
        f"{[r.get('domain') for r in rows if isinstance(r, dict)]}")

    window = storage.window_ms(engine)
    assert window is not None and abs(window - 30 * MINUTE) <= 2 * MINUTE, (
        f"closing the bar silenced {engine.get('domain')!r} for "
        f"{window / MINUTE if window else None} minutes, not the half hour the "
        f"bar asks for")


async def test_activating_from_the_bar_silences_the_retailer(page, context, keyword):
    """2 — activating from the bar behaves exactly like activating in the popup.

    The shop is what goes quiet, not the search engine: the offer was accepted,
    so the user is on their way to that retailer and must not be offered it
    again. The engine stays free, because nothing was dismissed there.
    """
    frame, ctl = await open_bar(page, context, keyword)

    assert await popup.click(frame, ctl["activate"], settle=6), \
        "the bar has no activate button"

    rows = await storage.quiet_domains(context)
    shop = retailer_row(rows)
    assert shop, f"activating from the bar wrote no retailer row: {rows}"
    assert shop.get("phase") == "activated", \
        f"expected phase 'activated', got {shop.get('phase')!r}"

    engine = storage.entry_for(rows, ENGINE_HOME)
    assert not engine, (
        f"activating from the bar also silenced the search engine "
        f"({engine.get('domain')!r}) — accepting an offer is not dismissing one")


async def test_the_activated_shop_shows_the_confirmation_until_it_is_closed(
        page, context, keyword):
    """2 — after activating from the bar, the shop itself carries on the flow.

    The row activation writes is `phase: activated`, and that is what a visit to
    the shop reads: the confirmation, not a fresh offer. It keeps showing until
    the user closes it, and only then does the shop go quiet — the same two
    steps the popup has, reached from the bar.
    """
    frame, ctl = await open_bar(page, context, keyword)
    assert await popup.click(frame, ctl["activate"], settle=6), \
        "the bar has no activate button"

    shop = retailer_row(await storage.quiet_domains(context))
    if not shop:
        pytest.skip("could not learn which shop this keyword activated")

    # `domain` is stored reversed and possibly wildcarded; the host is enough
    # to visit it.
    host = str(shop["domain"]).lstrip("*.")
    tab = await context.new_page()
    await tab.goto(f"https://{host}", wait_until="domcontentloaded")

    confirmation = await popup.wait_for_popup(tab, timeout=25, route="activated")
    if confirmation is None:
        # An offer instead of the confirmation is the finding worth naming.
        offer = popup.frames(tab, "offer")
        await tab.close()
        assert not offer, (
            f"visiting {host} after activating from the bar showed a fresh "
            f"offer rather than the activated confirmation")
        pytest.skip(f"{host} showed nothing at all on the visit after activating")

    assert await popup.click(confirmation, popup.ACTIVATED["close_x"], settle=3), \
        "the activated confirmation has no close control"
    await tab.close()

    # Closed: the shop is now quiet, and a revisit brings nothing back.
    again = await context.new_page()
    await again.goto(f"https://{host}", wait_until="domcontentloaded")
    back = await popup.wait_for_popup(again, timeout=15)
    await again.close()

    assert back is None, (
        f"{host} showed a popup again after the activated confirmation was "
        f"closed; the shop should stay quiet until its window expires")


async def test_the_bar_has_an_opt_out(page, context, keyword):
    """2 — opt-out is reachable from the bar, as it is from the popup."""
    frame, ctl = await open_bar(page, context, keyword)

    assert await popup.visible(frame, ctl["optout"]), \
        "the bar has no opt-out control"
    assert await popup.click(frame, ctl["optout"], settle=2)

    # The bar's own opt-out, which is not the popup's: one line, the three
    # durations, and no Apply — choosing a duration is the action.
    assert await popup.visible(frame, ctl["optout_panel"]), \
        "the bar's opt-out control did not open the opt-out screen"
    for choice in ("optout_24h", "optout_30d", "optout_forever"):
        assert await popup.visible(frame, ctl[choice]), \
            f"the bar's opt-out screen is missing its {choice} option"


#: Each duration the bar's opt-out offers, and the window it must write.
OPTOUT_WINDOWS = {
    "optout_24h": 24 * 60 * MINUTE,
    "optout_30d": 30 * 24 * 60 * MINUTE,
}


@pytest.mark.parametrize("choice", sorted(OPTOUT_WINDOWS))
async def test_opting_out_from_the_bar_silences_the_engine_for_that_long(
        page, context, keyword, choice):
    """2 — opting out from the bar stops bars on the engine for the period chosen.

    Checked in that order: search again and see no bar, then read the row that
    explains it and confirm the window matches the button that was pressed. A
    row saying 24 hours while the bar reappears is not a pass, and neither is a
    bar that stays away for a window nobody asked for.
    """
    frame, ctl = await open_bar(page, context, keyword)

    assert await popup.click(frame, ctl["optout"], settle=2), \
        "the bar has no opt-out control"
    assert await popup.visible(frame, ctl["optout_panel"]), \
        "the bar's opt-out control did not open the opt-out screen"
    assert await popup.click(frame, ctl[choice], settle=4), \
        f"the bar's opt-out has no {choice} button to press"

    assert not await search_again(page, keyword), (
        f"a bar appeared on the next search for {keyword!r} after opting out "
        f"for {choice}")

    rows = await storage.quiet_domains(context)
    engine = storage.entry_for(rows, ENGINE_HOME)
    assert engine, (
        f"no bar came back, but nothing in quietDomains covers the engine — "
        f"rows: {[r.get('domain') for r in rows if isinstance(r, dict)]}")

    window = storage.window_ms(engine)
    expected = OPTOUT_WINDOWS[choice]
    assert window is not None and abs(window - expected) <= 5 * MINUTE, (
        f"opting out for {choice} silenced {engine.get('domain')!r} for "
        f"{window / MINUTE if window else None} minutes, expected "
        f"{expected / MINUTE:.0f}")


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
    frame, ctl = await open_bar(page, context, keyword)

    assert (await popup.click(frame, ctl["close_top"], settle=2)
            or await popup.click(frame, ctl["close_bottom"], settle=2)), \
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

    again = await popup.wait_for_bar(page, timeout=15)
    assert again is None, (
        f"a bar appeared for {keyword!r} on {shop['domain']}, which is "
        f"stood down")
