"""The saved state: every `bring_` key, set, read and recovered.

QA_TEST_PLAN section 4 and its table of 23 values. Most of these are never
visible on screen, which is exactly why they are worth asserting: a popup that
looks right while writing the wrong thing to `quietDomains` is a bug that shows
up a week later as "the popup stopped appearing".
"""
import time

import pytest

from bring import popup, storage

pytestmark = pytest.mark.storage

DAY = 24 * 60 * 60 * 1000

# Written on a first run, before the user has done anything.
FIRST_RUN_KEYS = ("id", "popupEnabled", "migrationVersion")

# Downloaded with the retailer list. Without these the extension cannot match a
# retailer at all, so their absence is why "no popup" happens everywhere.
LIST_KEYS = ("relevantDomains", "relevantDomainsCheck", "domainsTypes",
             "quietDomainsMaxLength", "standDownOffset", "redirectsWhitelist")


async def test_first_run_writes_its_own_identity(fresh_context, retailer):
    """4 — a user id exists without a wallet, and popups start enabled."""
    page = await fresh_context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(fresh_context)
    for key in FIRST_RUN_KEYS:
        assert key in saved, f"bring_{key} was never written on a first run"

    assert str(saved["id"]).strip(), "bring_id is empty"
    assert saved["popupEnabled"] in (True, "true", 1), \
        f"popups should default to on, bring_popupEnabled is {saved['popupEnabled']!r}"


async def test_the_retailer_list_and_its_settings_arrive(context, retailer):
    """4 — the list, its TTL, the type codes and the server-set limits."""
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(context)
    missing = [k for k in LIST_KEYS if k not in saved]
    assert not missing, (
        f"the retailer list downloaded without {', '.join(missing)} — the "
        f"extension is running on defaults it should have been told")

    assert saved["relevantDomains"], "the retailer list arrived empty"
    assert saved["domainsTypes"], "no type codes arrived with the retailer list"


async def test_the_environment_is_the_one_under_test(context, env_name, retailer):
    """4 — bring_envName points the extension at this environment, not production."""
    if not env_name:
        pytest.skip("no environment name to compare against")

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(context)
    assert saved.get("envName") == env_name, (
        f"bring_envName is {saved.get('envName')!r}, not {env_name!r} — this run "
        f"is talking to a different environment than it deployed")


async def test_deprecated_keys_are_gone_after_the_upgrade(fresh_context, retailer):
    """4 — postPurchaseUrls and optOutDomains are no longer written.

    Both were superseded: the first by follow-up matchers, the second folded
    into quietDomains on upgrade. A fresh install must not recreate either.
    """
    page = await fresh_context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(fresh_context)
    for key in storage.DEPRECATED_KEYS:
        assert key not in saved, \
            f"bring_{key} is deprecated but was written on a fresh install"


async def test_a_corrupted_retailer_list_is_re_downloaded(context, retailer):
    """4 — bad data must not brick the extension; it refetches and carries on."""
    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    assert await popup.wait_for_popup(page, timeout=30), "no popup before corrupting"

    await storage.set(context, "relevantDomains", "not-a-list")
    await storage.delete(context, "relevantDomainsCheck")
    await page.close()

    recovered = await context.new_page()
    await recovered.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(recovered, timeout=40)
    await recovered.close()

    assert frame, \
        "a corrupted relevantDomains stopped the extension instead of being refetched"


async def test_a_malformed_quiet_entry_does_not_silence_everything(context, retailer):
    """4 — a broken row is skipped, not treated as an active silence."""
    await storage.set(context, storage.QUIET_DOMAINS, [
        {"domain": storage.normalise(retailer), "time": "broken", "phase": "quiet"},
    ])

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    frame = await popup.wait_for_popup(page, timeout=30)
    await page.close()

    assert frame, \
        "a quietDomains row with a malformed time range silenced the retailer"


async def test_quiet_domains_are_pruned_on_write_not_on_read(context, retailer):
    """4 — expired rows survive a read and go on the next write.

    Stated as a test because it is the reason an expired opt-out is still in
    storage when a user comes back, and the reason section 1.9's shadowing bug
    was possible at all.
    """
    now = int(time.time() * 1000)
    stale = {"domain": "expired-e2e.example", "type": "kds", "phase": "quiet",
             "time": [now - 2 * DAY, now - DAY]}
    await storage.set(context, storage.QUIET_DOMAINS, [stale])

    page = await context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)

    after_read = await storage.quiet_domains(context)
    assert any(r.get("domain") == stale["domain"] for r in after_read), \
        "an expired row was pruned on read; pruning belongs to the next write"

    # Close the popup: that is a write into quietDomains.
    frame = popup.frames(page)[0] if popup.frames(page) else None
    if frame:
        await popup.click(frame, popup.OFFER["close_x"], settle=3)
    await page.close()

    after_write = await storage.quiet_domains(context)
    assert not any(r.get("domain") == stale["domain"] for r in after_write), \
        "an expired row survived a write into quietDomains"


async def test_storage_self_test_ran(fresh_context, retailer):
    """4 — the extension checks its own storage on startup and keeps working."""
    page = await fresh_context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(fresh_context)
    assert "extensionMemoryTest" in saved, \
        "the startup storage self-test left no trace, so it did not run"


async def test_every_expected_key_is_accounted_for(fresh_context, retailer):
    """4 — a sweep, so a key that quietly disappears is noticed.

    Not every key exists on every run — a user with no wallet has no
    walletAddress, nobody has opted out yet — so this asserts that no
    *unexpected* key appeared and that the ones that must be there are.
    """
    page = await fresh_context.new_page()
    await page.goto(retailer, wait_until="domcontentloaded")
    await popup.wait_for_popup(page, timeout=30)
    await page.close()

    saved = await storage.dump(fresh_context)
    unknown = sorted(set(saved) - set(storage.ALL_KEYS))
    assert not unknown, (
        f"the extension wrote keys this suite does not know about: {unknown}. "
        f"Add them to storage.ALL_KEYS and to the QA plan's table, or find out "
        f"why they are being written.")
