import asyncio
import json
import os
import re
import time
from pathlib import Path

from bring import retailers, storage

ARTIFACTS = Path(os.getenv("BRING_ARTIFACTS",
                           Path(__file__).resolve().parent.parent / "artifacts"))


class Result:
    """What one shop did in one step."""

    def __init__(self, ok: bool, detail: str = ""):
        self.ok = ok
        self.detail = detail

    @staticmethod
    def passed(detail=""):
        return Result(True, detail)

    @staticmethod
    def failed(detail):
        return Result(False, detail)

    @staticmethod
    def blocked(detail):
        """The shop served an anti-bot page, so nothing was observed.

        Counted as a pass because the extension did nothing wrong — the walk
        never reached a page to watch it on. Printed with `--` so it is
        visible in the log without being a red line.
        """
        return Result(True, f"-- {detail}")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


class Walk:
    def __init__(self, context, sites):
        self.context = context
        self.sites = sites
        self.tabs = {}
        self.failures = 0
        self.steps = 0
        self.act = "walk"
        self._n = 0

    # ── tabs ────────────────────────────────────────────────────────

    async def open_tabs(self):
        for site in self.sites:
            self.tabs[site] = await self.context.new_page()

    async def close_tabs(self):
        for tab in self.tabs.values():
            try:
                await tab.close()
            except Exception:
                pass
        self.tabs.clear()

    # ── acts ────────────────────────────────────────────────────────

    def begin(self, act: str, title: str):
        self.act = _slug(act)
        self._n = 0
        print("\n" + "=" * 64)
        print(f"{act} - {title}")
        print("=" * 64)

    def note(self, text: str):
        print(f"\n-- {text}")

    async def clear(self, note="clearing the quiet list for everyone"):
        """Wipe the shared list between scenarios — only ever between steps,
        never inside one, so there is no half-written state to lose."""
        await storage.delete(self.context, storage.QUIET_DOMAINS)
        await storage.delete(self.context, storage.OPT_OUT)
        self.note(note)

    # ── steps ───────────────────────────────────────────────────────

    async def step(self, title, work):
        """Run *work(site, tab)* for every shop at once."""
        started = time.time()
        outcomes = await asyncio.gather(
            *(self._guarded(work, site, self.tabs[site]) for site in self.sites))
        results = dict(zip(self.sites, outcomes))
        await self._record(title, results, time.time() - started)
        return results

    async def each(self, title, work):
        """Run *work(site, tab)* for every shop, one after another."""
        started = time.time()
        results = {}
        for site in self.sites:
            results[site] = await self._guarded(work, site, self.tabs[site])
        await self._record(title, results, time.time() - started)
        return results

    async def one(self, title, work, site=None):
        """Run *work(site, tab)* on one shop. Without an open tab for it a
        fresh one is made for the step and closed after."""
        site = site or self.sites[0]
        tab = self.tabs.get(site)
        fresh = tab is None
        if fresh:
            tab = await self.context.new_page()
        started = time.time()
        try:
            result = await self._guarded(work, site, tab)
            await self._record(title, {site: result}, time.time() - started)
        finally:
            if fresh:
                try:
                    await tab.close()
                except Exception:
                    pass
        return result

    async def _guarded(self, work, site, tab):
        try:
            return await work(site, tab)
        except Exception as e:
            return Result.failed(f"{type(e).__name__}: {e}"[:110])

    async def _record(self, title, results, seconds):
        self._n += 1
        self.steps += 1
        failed = [site for site, r in results.items() if not r.ok]
        self.failures += len(failed)
        mark = "ok " if not failed else "FAIL"
        print(f"\n[{mark}] {title}   ({seconds:.0f}s)")
        for site, result in results.items():
            flag = "  ok " if result.ok else "  X  "
            print(f"   {flag}{retailers.label(site):<18} {result.detail}")
        # Evidence for what went wrong, and for what could not be observed:
        # a bot check or a results page the extension did not recognise is
        # not a red line, but the page that caused it is worth a look.
        blocked = [site for site, r in results.items()
                   if r.ok and r.detail.startswith("--")]
        if failed or blocked:
            await self._capture(title, failed + blocked)

    async def _capture(self, title, failed):
        """Screenshots of the failed tabs and the storage behind them."""
        out = ARTIFACTS / self.act / f"{self._n:02d}-{_slug(title)}"
        try:
            out.mkdir(parents=True, exist_ok=True)
            state = await storage.dump(self.context)
            (out / "storage.json").write_text(
                json.dumps(state, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            try:
                (out / "storage-error.txt").write_text(str(e), encoding="utf-8")
            except Exception:
                pass
        for site in failed:
            tab = self.tabs.get(site)
            pages = [tab] if tab else list(self.context.pages)
            for index, page in enumerate(pages):
                name = retailers.label(site) if tab else f"page-{index}"
                try:
                    await page.screenshot(path=str(out / f"{name}.png"))
                    (out / f"{name}.url").write_text(page.url or "", encoding="utf-8")
                except Exception:
                    pass
        print(f"   -> evidence in {out.relative_to(ARTIFACTS.parent)}")
