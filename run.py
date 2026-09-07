#!/usr/bin/env python
"""Run the extension end-to-end suite, from nothing to a verdict.

    python run.py                       everything, on a fresh or reused env
    python run.py -k notification       one area
    python run.py -m popup              one marker
    python run.py --headless -n 4       how CI runs it
    python run.py --skip-env            the environment is already up

The environment is brought up here rather than inside pytest on purpose: under
`-n` every worker is a separate process, and a session fixture would have each
of them deploy, then find the others' half-built stack.
"""
import argparse
import asyncio
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bring import config as cfg              # noqa: E402
from bring import preflight                  # noqa: E402
from bring.env import Environment, EnvError, find_manifest_dir   # noqa: E402

JUNIT = ROOT / "junit.xml"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=cfg.ENV_NAME,
                   help=f"environment name (default: {cfg.ENV_NAME})")
    p.add_argument("--platform", default=cfg.PLATFORM,
                   help=f"wallet platform whose key builds it (default: {cfg.PLATFORM})")
    p.add_argument("-n", "--workers", default="4",
                   help="tests in parallel; each gets its own browser (default: 4)")
    p.add_argument("--headless", action="store_true",
                   help="Chrome's new headless — the only one that loads extensions")
    p.add_argument("-k", default=None, help="pytest -k expression")
    p.add_argument("-m", default=None, help="pytest -m marker expression")
    p.add_argument("--reuse-extension", action="store_true",
                   help="skip the S3 download and use the one already unpacked")
    p.add_argument("--skip-env", action="store_true",
                   help="assume the environment is up; do not talk to AWS")
    p.add_argument("--skip-preflight", action="store_true",
                   help="do not check the retailers are live before running")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="anything else is passed to pytest")
    return p.parse_args()


def _has_xdist() -> bool:
    import importlib.util

    return importlib.util.find_spec("xdist") is not None


def prepare_extension(env, into: Path, reuse: bool) -> Path:
    if reuse and into.exists():
        found = find_manifest_dir(into)
        print(f"Reusing the unpacked extension at {found}")
        return found
    return env.download_extension(into)


def summarise(exit_code: int) -> int:
    """Print the one line the release decision is actually made on."""
    print("\n" + "=" * 62)
    if not JUNIT.exists():
        print("NO RESULTS — the suite did not get as far as running")
        print("=" * 62)
        return exit_code or 1

    root = ET.parse(JUNIT).getroot()
    suites = root.findall("testsuite") or [root]
    total = failures = errors = skipped = 0
    broken = []
    for suite in suites:
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            bad = case.find("failure") if case.find("failure") is not None else case.find("error")
            if bad is not None:
                message = (bad.get("message") or "").splitlines()[0][:140]
                broken.append(f"{case.get('classname', '')}.{case.get('name')}\n      {message}")

    passed = total - failures - errors - skipped
    if failures or errors:
        print(f"FAIL — {failures + errors} of {total} checks failed "
              f"({passed} passed, {skipped} skipped)")
        print("-" * 62)
        for line in broken:
            print(f"  x {line}")
        print("-" * 62)
        print("Screenshots, storage dumps and traces: ./artifacts/")
    elif passed == 0:
        print(f"NOTHING ASSERTED — {skipped} skipped, none ran")
    else:
        print(f"PASS — {passed} of {total} checks passed"
              + (f", {skipped} skipped" if skipped else ""))
        print("Safe to release as far as this suite can tell.")
    print("=" * 62)
    return exit_code


def main() -> int:
    args = parse_args()
    os.environ["BRING_ENV_NAME"] = args.env
    os.environ["BRING_PLATFORM"] = args.platform
    if args.headless:
        os.environ["BRING_HEADLESS"] = "1"

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
        # test fail with "no popup" — correct behaviour reported as a bug, and
        # twenty minutes spent to get there. Ask first.
        print(f"Checking the retailers are live on '{args.env}':")
        try:
            results = asyncio.run(preflight.verify(
                extension, headless=bool(args.headless)))
            preflight.report(results)
        except preflight.TargetError as e:
            print(f"\nCannot test: {e}", file=sys.stderr)
            return 2        # the run was asked for something impossible
        print()

    # A shard takes its slice of the checks and nothing else. Set by CI, where
    # four machines run at once; unset everywhere else, which runs the lot.
    #
    # Split by measured duration rather than by count: the tests are wildly
    # uneven — a notification variant is seventy seconds, a storage read is
    # under one — so four equal *counts* would finish minutes apart and the run
    # would be as slow as its unluckiest quarter.
    shard = os.getenv("BRING_SHARD", "").strip()
    shards = os.getenv("BRING_SHARDS", "").strip()

    command = [sys.executable, "-m", "pytest", f"--junitxml={JUNIT}"]

    if shard and shards:
        try:
            import pytest_split          # noqa: F401
            command += ["--splits", shards, "--group", shard]
            durations = ROOT / ".test_durations"
            if durations.exists():
                command += ["--durations-path", str(durations)]
            else:
                # Without recorded timings the split is by name order, which is
                # even but not balanced. Still four times fewer checks per
                # machine; record durations once to do better.
                print(f"shard {shard} of {shards} (no recorded timings — "
                      f"splitting evenly by count)")
        except ImportError:
            print("pytest-split is not installed, so this shard is running the "
                  "whole suite (pip install pytest-split)")
    if args.workers and args.workers != "0":
        if _has_xdist():
            # Plain `load`, not `loadgroup`: retailers are meant to mix within a
            # worker so its browser holds one `quietDomains` list covering
            # several shops, which is the situation a real user is in.
            command += ["-n", args.workers]
        else:
            # Serial still answers the question, just slower. Failing here
            # would mean no verdict at all over a missing convenience.
            print("pytest-xdist is not installed — running serially "
                  "(pip install pytest-xdist)")
    if args.k:
        command += ["-k", args.k]
    if args.m:
        command += ["-m", args.m]
    command += [a for a in args.rest if a != "--"]

    JUNIT.unlink(missing_ok=True)
    result = subprocess.run(command, cwd=ROOT)
    return summarise(result.returncode)


if __name__ == "__main__":
    sys.exit(main())
