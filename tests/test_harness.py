"""The harness checking itself.

Two tests, and they are not ceremony. The lane fixture keeps one browser for a
whole retailer and clears only what the last test wrote — if that reset ever
stops running, dozens of tests start failing on a silenced retailer and every
message blames the product. If the browser stops being reused, the suite goes
back to testing a first-ever install over and over. Both failures are
expensive to diagnose from the tests they break, and cheap to catch here.

Not parametrised by retailer: this is about the harness, and asking it four
times says nothing more.
"""
import time

from bring import storage

LEAK_MARKER = "harness-leak-check.example"
_lanes = []


async def test_the_lane_reuses_one_browser_and_keeps_its_identity(context):
    """Accumulated state survives, because a real profile accumulates."""
    _lanes.append(id(context))
    assert await storage.get(context, "id"), \
        "the extension has no user id — it never finished its first run"

    now = int(time.time() * 1000)
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": LEAK_MARKER, "type": "kds", "phase": "quiet",
         "time": [now, now + 3600_000]}])


async def test_the_lane_clears_the_previous_test_silences(context):
    """…but silences do not, or every later test fails on a quiet retailer."""
    _lanes.append(id(context))
    assert _lanes[0] == _lanes[-1], (
        "the lane opened a second browser instead of reusing the first — every "
        "test is back to running as a brand-new install")

    rows = await storage.quiet_domains(context)
    assert not any(r.get("domain") == LEAK_MARKER for r in rows), (
        "the previous test's quietDomains survived into this one; every test "
        "after a close or an activate will now fail on a silenced retailer")

    assert await storage.get(context, "id"), \
        "the reset wiped the user id — it should clear silences only"
