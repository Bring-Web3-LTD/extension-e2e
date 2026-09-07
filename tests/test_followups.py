import time
import pytest
from bring import netspy, storage

pytestmark = pytest.mark.followups

def ours(error) -> bool:

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
    await netspy.install(context, netspy.PASS)
    await arm(context, rule(type="t", cnt=5))
    await netspy.reset(context)

    await visit(page, "/thank-you?order=1")

    # Waited for, not counted after a fixed pause. Measured: the fire and its
    # repost take about 1.3s, and the 1.2s this used to sleep missed it by a
    # tenth of a second — which read as "the rule never fired" when the SDK's
    # own log said `Trigger matched (TOF) - firing and dropping watcher`.
    assert await netspy.await_call(context, netspy.POPUP_CHECK),         "reaching the trigger page sent no report to the server"

    assert await budget(context) is None, \
        "a TOF rule stayed armed after firing; it should terminate on fire"

    await netspy.reset(context)
    await visit(page, "/thank-you?order=2")
    assert not await netspy.calls(context, netspy.POPUP_CHECK), \
        "the rule reported a second time after it should have been dropped"
