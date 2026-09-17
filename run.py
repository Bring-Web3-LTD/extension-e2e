#!/usr/bin/env python
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bring import config as cfg, preflight, retailers, storage      # noqa: E402
from bring.browser import extension_browser                         # noqa: E402
from bring.env import Environment, EnvError, find_manifest_dir      # noqa: E402
from bring.walk import Walk                                         # noqa: E402
from tests import (followups, notifications, offerbar, optout,      # noqa: E402
                   popup, standdown, wallet, widget)

# In order. The first three share the three shop tabs; the rest open their own.
SHARED_TABS = ("popup", "optout", "standdown")
ACTS = {
    "popup": popup.act,
    "optout": optout.act,
    "standdown": standdown.act,
    "followups": followups.act,
    "wallet": wallet.act,
    "widget": widget.act,
    "offerbar": offerbar.act,
    "notifications": notifications.act,
}


def parse_args():
    p = argparse.ArgumentParser(description="Walk the QA plan against a live environment.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=cfg.ENV_NAME,
                   help=f"environment name (default: {cfg.ENV_NAME})")
    p.add_argument("--platform", default=cfg.PLATFORM,
                   help=f"wallet platform whose key builds it (default: {cfg.PLATFORM})")
    p.add_argument("--headless", action="store_true",
                   help="Chrome's new headless — the only one that loads extensions")
    p.add_argument("--only", action="append", choices=sorted(ACTS),
                   help="run one act (repeatable); default: all, in order")
    p.add_argument("--retailers", default=None,
                   help="comma-separated shops instead of the default three")
    p.add_argument("--reuse-extension", action="store_true",
                   help="skip the S3 download and use the one already unpacked")
    p.add_argument("--skip-env", action="store_true",
                   help="assume the environment is up; do not talk to AWS")
    p.add_argument("--skip-preflight", action="store_true",
                   help="do not check the retailers are live before running")
    return p.parse_args()


def prepare_extension(env, into: Path, reuse: bool) -> Path:
    if reuse and into.exists():
        found = find_manifest_dir(into)
        print(f"Reusing the unpacked extension at {found}")
        return found
    return env.download_extension(into)


async def walk_through(extension: Path, only, headless: bool) -> int:
    profile = ROOT / "profiles" / f"walk-{os.getpid()}"
    sites = retailers.sites()
    chosen = [name for name in ACTS if not only or name in only]

    print(f"Shops:     {', '.join(retailers.label(s) for s in sites)}   "
          f"(one browser, {len(sites)} tabs)")
    print(f"Acts:      {', '.join(chosen)}")

    async with extension_browser(extension, profile, headless=headless) as context:
        # Refuse to walk on an extension that never got its retailer list:
        # every step after this would say "no popup" about nothing.
        if not await storage.settled(context, timeout=90):
            print("\nCannot walk: the extension never downloaded its retailer list "
                  "(relevantDomains is empty after 90s). The environment is up but "
                  "not answering, or the first-run fetch failed — nothing here "
                  "would be about the product.")
            return 3

        walk = Walk(context, sites)
        shared = [name for name in chosen if name in SHARED_TABS]
        if shared:
            await walk.open_tabs()
            try:
                for name in shared:
                    await ACTS[name](walk)
            finally:
                await walk.close_tabs()
        for name in chosen:
            if name not in SHARED_TABS:
                await ACTS[name](walk)

    print("\n" + "=" * 64)
    if walk.failures:
        print(f"FAIL - {walk.failures} of {walk.steps} step(s) did not do what they "
              f"should. Screenshots and storage dumps: ./artifacts/")
    else:
        print(f"PASS - all {walk.steps} steps did the right thing")
    print("=" * 64)
    return 1 if walk.failures else 0


def main() -> int:
    args = parse_args()
    os.environ["BRING_ENV_NAME"] = args.env
    os.environ["BRING_PLATFORM"] = args.platform
    if args.headless:
        os.environ["BRING_HEADLESS"] = "1"
    if args.retailers:
        os.environ["BRING_RETAILERS"] = args.retailers

    extension_root = ROOT / "extension" / args.env
    if args.skip_env:
        print(f"Skipping the environment check — assuming '{args.env}' is up")
        extension = find_manifest_dir(extension_root)
    else:
        try:
            env = Environment(args.env).ensure()
            extension = prepare_extension(env, extension_root, args.reuse_extension)
        except EnvError as e:
            print(f"\nCannot test: {e}", file=sys.stderr)
            return 3        # infrastructure, not a product finding

    os.environ["BRING_EXTENSION_DIR"] = str(extension)
    print(f"\nExtension: {extension}")
    print(f"API:       {cfg.env_api_url(args.env)}\n")

    if not args.skip_preflight:
        # A retailer that has left this environment's list makes every popup
        # step fail with "no popup" — correct behaviour reported as a bug, and
        # twenty minutes spent to get there. Ask first.
        print(f"Checking the retailers are live on '{args.env}':")
        try:
            results = asyncio.run(preflight.verify(extension, headless=bool(args.headless)))
            preflight.report(results)
        except preflight.TargetError as e:
            print(f"\nCannot test: {e}", file=sys.stderr)
            return 2        # the run was asked for something impossible
        print()

    return asyncio.run(walk_through(extension, args.only, bool(args.headless)))


if __name__ == "__main__":
    sys.exit(main())
