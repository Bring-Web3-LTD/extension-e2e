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
    await open_bar(page, context, keyword)

    found = popup.bars(page)
    assert len(found) == 1, f"{len(found)} bars on one results page"


async def test_the_page_makes_room_and_gets_it_back(page, context, keyword):
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

    # A detached frame here is the confirmation closing itself as it is
    # clicked, which is the outcome asked for — not a failure to click.
    try:
        clicked = await popup.click(confirmation, popup.ACTIVATED["close_x"],
                                    settle=3)
    except Exception as detached:
        if "detached" not in str(detached).lower():
            raise
        clicked = True
    assert clicked, "the activated confirmation has no close control"
    await tab.close()

    # Closed: the shop is now quiet, and a revisit brings nothing back.
    again = await context.new_page()
    await again.goto(f"https://{host}", wait_until="domcontentloaded")
    back = await popup.wait_for_popup(again, timeout=popup.ABSENT)
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

    again = await popup.wait_for_bar(page, timeout=popup.ABSENT)
    assert again is None, (
        f"a bar appeared for {keyword!r} on {shop['domain']}, which is "
        f"stood down")
