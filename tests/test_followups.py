"""Follow-up matchers: the watchers the server arms on your next navigations.

QA_TEST_PLAN section 1.9. Instead of shipping every thank-you-page URL to the
client, the server arms a rule and the extension watches the navigations that
follow. Two shapes, and they are opposites (SDK followups.ts):

    ctl.type 't'   TOF, terminate on fire. The trigger matches once, the rule
                   reports it and is dropped. This is the thank-you page.
    ctl.type 'f'   FOT, fire on terminate. Every navigation inside `ctl.regex`
                   spends one of `ctl.cnt`; when the budget runs out the rule
                   fires with whatever it accumulated. This is "pop on the Nth
                   visit".

`ctl.scope` decides who spends the budget — `tab` counts only its own tab,
`browser` counts any of them — and `ttl` drops the rule when its time is up.

**Why these tests can exist at all.** The server arms the rule, and there is no
way to ask it to. But `bring_followups` is ordinary storage, so the rule is
written here instead, with regexes this suite controls. What that gives up is
"the server sends the right rule"; what it keeps is everything the client is
responsible for — counting down, honouring scope, expiring, surviving a
restart, and reposting when it fires. That is the half where the bugs live, and
it is the half section 1.9 describes.

Patterns are matched against `reverseStr(host) + path`, the same way the
retailer list is (domainsListSearch.ts), so a rule for `example.com` is written
against `moc.elpmaxe`.
"""
import json
import time

import pytest

from bring import netspy, storage

pytestmark = pytest.mark.followups

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


KEY = "followups"
MINUTE = 60_000

# Not a retailer, so the popup never interferes with the counting.
HOST = "https://example.com"
SCOPE_RE = "^moc\\.elpmaxe"                     # any page on example.com
TRIGGER_RE = "^moc\\.elpmaxe/thank-you"          # the "conversion" page


def rule(*, id="e2e-followup", type="f", scope="browser", cnt=3,
         regex=SCOPE_RE, trigger=TRIGGER_RE, ttl=10 * MINUTE, tab_id=None):
    """One armed watcher, in the shape `armFollowups` leaves in storage."""
    return {
        "id": id,
        "ctl": {"type": type, "scope": scope, "regex": regex, "cnt": cnt},
        "ttl": ttl,
        "trigger": trigger,
        "expiresAt": int(time.time() * 1000) + ttl,
        "tabId": tab_id,
        "matches": [],
    }


async def arm(context, *rules):
    await storage.set(context, KEY, list(rules))


async def armed(context) -> list:
    return await storage.get(context, KEY) or []


async def budget(context, rule_id="e2e-followup"):
    """What is left of a rule's counter, or None once it has gone."""
    for record in await armed(context):
        if isinstance(record, dict) and record.get("id") == rule_id:
            return record.get("ctl", {}).get("cnt")
    return None


async def visit(page, path):
    await page.goto(f"{HOST}{path}", wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)


# ── the counter ─────────────────────────────────────────────────────

async def test_the_counter_goes_down_one_navigation_at_a_time(context, page):
    """1.9 — every navigation in scope spends exactly one of the budget."""
    await arm(context, rule(cnt=3))
    assert await budget(context) == 3, "the rule was not armed"

    await visit(page, "/one")
    assert await budget(context) == 2, "the first navigation did not spend budget"

    await visit(page, "/two")
    assert await budget(context) == 1, "the second navigation did not spend budget"


async def test_navigations_outside_the_scope_cost_nothing(context, page):
    """1.9 — the counter is for the rule's own pages, not for browsing at large."""
    await arm(context, rule(cnt=3))

    await page.goto("https://www.iana.org/help/example-domains",
                    wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    assert await budget(context) == 3, \
        "a navigation outside ctl.regex spent budget it should not have"


async def test_the_rule_leaves_when_its_budget_runs_out(context, page):
    """1.9 — at zero the watcher is done and stops being consulted."""
    await arm(context, rule(cnt=2))

    await visit(page, "/one")
    await visit(page, "/two")

    left = await budget(context)
    assert left is None or left <= 0, \
        f"the rule is still armed with {left} budget after spending it all"


async def test_a_browser_scoped_rule_counts_every_tab(context, page):
    """1.9 — browser scope means any tab spends from the same budget."""
    await arm(context, rule(cnt=3, scope="browser"))

    await visit(page, "/one")
    spent_here = await budget(context)

    other = await context.new_page()
    await other.goto(f"{HOST}/two", wait_until="domcontentloaded")
    await other.wait_for_timeout(1200)
    await other.close()

    spent_there = await budget(context)
    assert spent_there is not None and spent_there < spent_here, (
        f"a second tab did not spend from a browser-scoped rule "
        f"({spent_here} -> {spent_there})")


async def test_a_tab_scoped_rule_ignores_other_tabs(context, page):
    """1.9 — tab scope counts its own tab and nothing else.

    Armed against a tab id that is deliberately not this one, so any spend at
    all is the scope being ignored.
    """
    await arm(context, rule(cnt=3, scope="tab", tab_id=-999))

    await visit(page, "/one")
    await visit(page, "/two")

    assert await budget(context) == 3, \
        "a tab-scoped rule spent budget on navigations in a different tab"


# ── firing ──────────────────────────────────────────────────────────

async def test_the_thank_you_page_reports_once_and_stops(context, page):
    """1.9 — a TOF rule fires on the trigger, reposts, and is gone.

    The repost is the observable half: it goes back to `/check/popup` carrying
    the followups payload, which is how the server learns the page was reached.
    """
    await netspy.install(context, netspy.PASS)
    await arm(context, rule(type="t", cnt=5))
    await netspy.reset(context)

    await visit(page, "/thank-you?order=1")

    reposts = [c for c in await netspy.calls(context, netspy.POPUP_CHECK)]
    assert reposts, "reaching the trigger page sent no report to the server"

    assert await budget(context) is None, \
        "a TOF rule stayed armed after firing; it should terminate on fire"

    await netspy.reset(context)
    await visit(page, "/thank-you?order=2")
    assert not await netspy.calls(context, netspy.POPUP_CHECK), \
        "the rule reported a second time after it should have been dropped"


async def test_it_does_not_report_before_the_trigger(context, page):
    """1.9 — pages inside the scope but not the trigger stay quiet."""
    await netspy.install(context, netspy.PASS)
    await arm(context, rule(type="t", cnt=5))
    await netspy.reset(context)

    await visit(page, "/basket")
    await visit(page, "/checkout")

    assert not await netspy.calls(context, netspy.POPUP_CHECK), \
        "the rule reported on a page that is not its trigger"
    assert await budget(context) == 3, \
        "the budget did not come down while waiting for the trigger"


# ── ttl and persistence ─────────────────────────────────────────────

async def test_a_rule_past_its_ttl_is_dropped(context, page):
    """1.9 — a watcher does not outlive its time.

    The expiry is a timestamp, so it is moved rather than waited out; that is
    the same state the extension would be in ten minutes later.
    """
    expired = rule(cnt=5)
    expired["expiresAt"] = int(time.time() * 1000) - MINUTE
    await arm(context, expired)

    await visit(page, "/one")

    left = await budget(context)
    assert left is None, \
        f"an expired rule is still armed (budget {left})"


async def test_the_rule_and_its_counter_survive_a_worker_restart(context, page):
    """1.9 — state is saved, not held in memory.

    An MV3 worker is recycled whenever Chrome feels like it. A counter that
    lived only in memory would silently reset, and the Nth visit would never
    arrive.
    """
    await arm(context, rule(cnt=4))
    await visit(page, "/one")
    before = await budget(context)
    assert before == 3, f"expected 3 after one visit, got {before}"

    raw = await storage.get(context, KEY)
    assert isinstance(raw, list) and raw, \
        "the rule is not in storage at all, so nothing could survive a restart"
    assert raw[0]["ctl"]["cnt"] == before, (
        f"storage holds {raw[0]['ctl']['cnt']} but the live counter is {before} "
        f"— the count is being kept in memory")

    await visit(page, "/two")
    assert await budget(context) == 2, \
        "the counter did not continue from where storage left it"


async def test_a_malformed_rule_is_ignored_rather_than_crashing(context, page):
    """1.9 — bad data from the server must not take navigation down with it."""
    await storage.set(context, KEY, [
        {"id": "broken", "ctl": {"type": "f", "scope": "browser",
                                 "regex": "([unclosed", "cnt": 2},
         "ttl": MINUTE, "trigger": "([also-unclosed",
         "expiresAt": int(time.time() * 1000) + MINUTE,
         "tabId": None, "matches": []},
    ])

    errors = []
    page.on("pageerror",
            lambda e: errors.append(str(e)) if ours(e) else None)
    await visit(page, "/one")

    assert not errors, f"a malformed follow-up rule threw: {errors[:2]}"
    remaining = await armed(context)
    assert not any(r.get("id") == "broken" for r in remaining
                   if isinstance(r, dict)), \
        "a rule whose regex cannot compile stayed armed"
