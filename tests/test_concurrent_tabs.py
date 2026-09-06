"""Several shops open at once, in one browser, sharing one extension.

This is the shape a real user is in and the rest of the suite is not: tests run
one at a time, so nothing they do overlaps. Here the tabs are genuinely
simultaneous — `asyncio.gather` over real navigations — and what is under test
is the thing that only breaks when they are.

`quietDomains` is one list for every retailer. Every close, activate and opt-out
is a read-modify-write of it (addQuietDomain: read, cleanup, find-or-append,
write). Two tabs finishing a second apart both read the list before either
writes, and the second write puts back a list that never contained the first
one's row — a lost update. The user sees a retailer they silenced start popping
again, which is unreproducible by hand and invisible in a serial suite.

These tests are deliberately few. Running the whole suite concurrently would
not add coverage, it would add interference: `opt out of all websites` silences
every other tab mid-assertion, and the no-network test disconnects them all.
Concurrency is worth testing, not worth running everything under.
"""
import asyncio

import pytest

from bring import popup, retailers, storage

pytestmark = pytest.mark.concurrent


async def open_and_wait(context, url):
    """A tab on *url* with its offer open, or (tab, None) if none appeared."""
    tab = await context.new_page()
    await tab.goto(url, wait_until="domcontentloaded")
    frame = await popup.wait_for_offer(tab, timeout=40)
    return tab, frame


async def test_four_shops_at_once_each_get_their_own_popup(context):
    """One extension, four tabs open together — each still gets its offer.

    The serial version of this passes on an extension that can only ever track
    one tab at a time. This one does not.
    """
    sites = retailers.sites()
    await storage.delete(context, storage.QUIET_DOMAINS)

    opened = await asyncio.gather(*(open_and_wait(context, s) for s in sites))

    got = {site: frame is not None for site, (_, frame) in zip(sites, opened)}
    for tab, _ in opened:
        await tab.close()

    missing = [site for site, ok in got.items() if not ok]
    assert not missing, (
        f"with {len(sites)} shops open at once, no popup appeared on: "
        f"{', '.join(missing)}")


async def test_closing_in_several_tabs_at_once_loses_no_row(context):
    """The lost update, stated as a test.

    Four tabs, four closes, as close to simultaneous as a browser allows. Each
    close is a read-modify-write of the same list, so if two overlap the second
    can put back a copy that never had the first one's row in it. Every shop
    that was closed has to end up in `quietDomains`; a missing one is a silence
    the user asked for and did not get.
    """
    sites = retailers.sites()
    await storage.delete(context, storage.QUIET_DOMAINS)

    opened = await asyncio.gather(*(open_and_wait(context, s) for s in sites))
    ready = [(site, tab, frame) for site, (tab, frame) in zip(sites, opened) if frame]
    if len(ready) < 2:
        for tab, _ in opened:
            await tab.close()
        pytest.skip(f"only {len(ready)} of {len(sites)} shops showed an offer, "
                    f"so there is nothing concurrent to test")

    await asyncio.gather(*(popup.click(frame, popup.OFFER["close_x"], settle=1)
                           for _, _, frame in ready))
    # The writes are asynchronous inside the extension; give them room to land
    # before reading, or this measures the read rather than the write.
    await asyncio.sleep(4)

    rows = await storage.quiet_domains(context)
    for _, tab, _ in ready:
        await tab.close()

    lost = [site for site, _, _ in ready if not storage.entries_for(rows, site)]
    assert not lost, (
        f"{len(lost)} of {len(ready)} shops closed at the same time left no row "
        f"in quietDomains: {', '.join(lost)}. Their writes overwrote each "
        f"other — the list is read, modified and written back whole.\n"
        f"stored: {[r.get('domain') for r in rows if isinstance(r, dict)]}")


async def test_a_silence_in_one_tab_does_not_leak_into_another(context):
    """Two shops open together; silencing one must not silence the other.

    The scoping tests elsewhere prove this with the tabs opened one after the
    other. Doing it while both are live is the version that catches a write
    keyed on the wrong tab's domain.
    """
    sites = retailers.sites()
    if len(sites) < 2:
        pytest.skip("this needs two shops")
    first, second = sites[0], sites[1]
    await storage.delete(context, storage.QUIET_DOMAINS)

    (tab_a, frame_a), (tab_b, frame_b) = await asyncio.gather(
        open_and_wait(context, first), open_and_wait(context, second))

    if not frame_a or not frame_b:
        for tab in (tab_a, tab_b):
            await tab.close()
        pytest.skip("both shops need an offer open for this to mean anything")

    assert await popup.click(frame_a, popup.OFFER["close_x"], settle=3)
    await asyncio.sleep(2)

    rows = await storage.quiet_domains(context)
    still_open = bool(popup.frames(tab_b))
    for tab in (tab_a, tab_b):
        await tab.close()

    assert storage.entries_for(rows, first), \
        f"closing {first} while another tab was open wrote nothing for it"
    assert not storage.entries_for(rows, second), (
        f"closing {first} also silenced {second}, which was only open in "
        f"another tab")
    assert still_open, \
        f"closing the offer on {first} also removed the one on {second}"


async def test_the_list_holds_every_shop_at_once(context):
    """4 — one list, several retailers, all live together.

    A worker that only ever sees one shop never has a `quietDomains` with more
    than one row in it, so nothing exercises reading the right row out of
    several. Here they are all there at once, and each has to resolve to itself.
    """
    sites = retailers.sites()
    await storage.delete(context, storage.QUIET_DOMAINS)

    opened = await asyncio.gather(*(open_and_wait(context, s) for s in sites))
    ready = [(site, tab, frame) for site, (tab, frame) in zip(sites, opened) if frame]
    if len(ready) < 2:
        for tab, _ in opened:
            await tab.close()
        pytest.skip("not enough shops showed an offer")

    for _, _, frame in ready:
        await popup.click(frame, popup.OFFER["close_x"], settle=1)
    await asyncio.sleep(4)

    rows = await storage.quiet_domains(context)
    for _, tab, _ in ready:
        await tab.close()

    for site, _, _ in ready:
        found = storage.entries_for(rows, site)
        assert found, f"{site} is missing from a list of {len(rows)} rows"
        assert len(found) == 1, (
            f"{site} resolved to {len(found)} rows; a lookup returns the first "
            f"match, so duplicates decide behaviour by insertion order")
