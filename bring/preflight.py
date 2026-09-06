"""Check the targets before the suite spends twenty minutes on them.

A retailer is only a valid target while the environment under test actually
carries it. When it does not, every popup assertion fails with "no popup" — and
that is the correct product behaviour being reported as a bug. The QA framework
learned this the expensive way: `lego.com` sat in its smoke list for months
testing a retailer with no local offer, and every run blamed the extension.

The cheap, exact test is the extension's own: navigate, and see whether it sent
a `/check/popup` at all. No call means the URL never matched the retailer list,
which is a fact about the target and the environment — not about the popup. One
browser, a few seconds per retailer, and a config error instead of a wall of red.
"""
import tempfile
from pathlib import Path

from bring import netspy, popup, retailers
from bring.browser import extension_browser
from bring.storage import settled


class TargetError(RuntimeError):
    """The retailers this run was given cannot answer the questions asked."""


async def verify(extension_dir: Path, sites=None, *, headless: bool = True,
                 timeout: float = 25) -> list:
    """Ask the extension about each retailer. Returns [(site, ok, reason)]."""
    sites = list(sites or retailers.sites())
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        async with extension_browser(extension_dir, Path(tmp) / "profile",
                                     headless=headless) as ctx:
            await settled(ctx)
            await netspy.install(ctx, netspy.PASS)

            for site in sites:
                await netspy.reset(ctx)
                page = await ctx.new_page()
                try:
                    await page.goto(site, wait_until="domcontentloaded",
                                    timeout=int(timeout * 1000))
                except Exception as e:
                    results.append((site, False, f"the site would not load ({e})"))
                    await page.close()
                    continue

                frame = await popup.wait_for_offer(page, timeout=timeout)
                checks = await netspy.count(ctx, netspy.POPUP_CHECK)
                landed = page.url
                await page.close()

                if frame:
                    results.append((site, True, "popup shown"))
                elif checks:
                    # The extension recognised it and the server declined —
                    # not in this country, or already attributed. A real target
                    # for the suite's negative cases, but not for the positive
                    # ones, so it is worth saying out loud.
                    results.append((site, False,
                                    "recognised as a retailer, but the server "
                                    "returned no offer for it here"))
                else:
                    results.append((site, False,
                                    f"never matched the retailer list "
                                    f"(landed on {landed[:80]})"))

    return results


def report(results) -> None:
    """Print the verdict, and stop the run when nothing usable is left."""
    for site, ok, reason in results:
        # Plain ASCII: this goes to a Windows console that is not always UTF-8,
        # and a mangled character in the one line explaining a failed run is a
        # bad trade for a nicer dash.
        print(f"   {'ok ' if ok else 'no '} {site} - {reason}")

    usable = [site for site, ok, _ in results if ok]
    if not usable:
        raise TargetError(
            "None of the configured retailers produced a popup on this "
            "environment.\n"
            "Every popup test would fail with 'no popup', which would be a "
            "fact about the retailer list, not about the extension.\n"
            "Pick retailers this environment carries:\n"
            "  BRING_RETAILERS=https://www.example.com,https://www.other.com")

    if len(usable) < 2:
        raise TargetError(
            f"Only {usable[0]} is usable, and the suite needs two.\n"
            f"Close, activate and opt-out each have to prove the silence they "
            f"wrote applies to one retailer and not to another; with a single "
            f"site that half of every scope assertion silently does not run.")
