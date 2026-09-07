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
