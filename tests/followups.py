import time
from bring import netspy, storage
from bring.walk import Result

KEY = "followups"
MINUTE = 60_000
HOST = "https://example.com"                    # not a retailer: no popup in the way
SCOPE_RE = "^moc\\.elpmaxe"                     # any page on example.com
TRIGGER_RE = "^moc\\.elpmaxe/thank-you"         # the "conversion" page


def rule(*, id="e2e-followup", type="t", scope="browser", cnt=5,
         ttl=10 * MINUTE):
    return {
        "id": id,
        "ctl": {"type": type, "scope": scope, "regex": SCOPE_RE, "cnt": cnt},
        "ttl": ttl,
        "trigger": TRIGGER_RE,
        "expiresAt": int(time.time() * 1000) + ttl,
        "tabId": None,
        "matches": [],
    }


async def budget(context, rule_id="e2e-followup"):
    """What is left of a rule's counter, or None once it has gone."""
    for record in await storage.get(context, KEY) or []:
        if isinstance(record, dict) and record.get("id") == rule_id:
            return record.get("ctl", {}).get("cnt")
    return None


async def visit(tab, path):
    await tab.goto(f"{HOST}{path}", wait_until="domcontentloaded")
    await tab.wait_for_timeout(1200)


def make_nothing_before_activation(walk):
    async def check(site, tab):
        await storage.delete(walk.context, KEY)
        await netspy.install(walk.context, netspy.PASS)
        await netspy.reset(walk.context)
        await visit(tab, "/thank-you?order=0")
        if await storage.get(walk.context, KEY):
            return Result.failed("a rule appeared with nothing armed")
        try:
            calls = await netspy.calls(walk.context, netspy.POPUP_CHECK)
        except netspy.SpyGone:
            calls = []
        if calls:
            return Result.failed(f"{len(calls)} report(s) went out with no rule armed")
        return Result.passed("no rule, no report")
    return check


def make_reports_once_and_stops(walk):
    async def check(site, tab):
        await storage.set(walk.context, KEY, [rule()])
        await visit(tab, "/thank-you?order=1")

        # The rule leaves storage when it fires. Wait for that, not a request.
        deadline = time.time() + 20
        while await budget(walk.context) is not None and time.time() < deadline:
            await tab.wait_for_timeout(250)
        if await budget(walk.context) is not None:
            return Result.failed("reaching the trigger page did not fire the rule "
                                 "— a TOF watcher is dropped when it fires, and "
                                 "this one is still armed")

        # Fired once means fired once: a fresh recorder for the second visit.
        await netspy.install(walk.context, netspy.PASS)
        await netspy.reset(walk.context)
        await visit(tab, "/thank-you?order=2")
        try:
            again = await netspy.calls(walk.context, netspy.POPUP_CHECK)
        except netspy.SpyGone:
            again = []
        if again:
            return Result.failed("the rule reported a second time after it "
                                 "should have been dropped")
        return Result.passed("fired once, then gone")
    return check


async def act(walk):
    walk.begin("ACT 4", "follow-up matchers")
    await walk.one("before a rule is armed, the thank-you page reports nothing",
                   make_nothing_before_activation(walk), site=HOST)
    await walk.one("an armed rule reports once on the thank-you page and stops",
                   make_reports_once_and_stops(walk), site=HOST)
    await storage.delete(walk.context, KEY)
    await walk.clear()
