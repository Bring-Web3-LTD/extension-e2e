#!/usr/bin/env python
"""What the failures have in common.

    python triage.py [run.log]

A run that fails in thirty places rarely has thirty causes. This reads the
evidence each failure left in `artifacts/` — the page it died on, the URL that
page was showing, and the extension's saved state at that moment — and groups
them, so the question becomes "which four things broke" rather than "which
thirty tests are red".

The grouping is deliberately crude: the first line of the assertion. Two
failures whose message starts the same way almost always share a cause, and
crude-but-honest beats a clever classifier that hides the odd one out.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"


def failures_from(log: Path):
    """(test id, first line of the message) for everything that failed."""
    if not log.exists():
        return []
    text = log.read_text(encoding="utf-8", errors="replace")

    found = []
    # pytest's own summary is the most reliable source; it survives xdist.
    for line in text.splitlines():
        match = re.match(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", line.strip())
        if match:
            found.append((match.group(1), (match.group(2) or "").strip()))
    return found


def evidence(test_id: str) -> dict:
    """Whatever the harness managed to keep for this test."""
    safe = (test_id.replace("/", "_").replace("::", "__").replace(".py", ""))
    folder = ARTIFACTS / safe
    if not folder.exists():
        return {}

    out = {"folder": str(folder.relative_to(ROOT))}
    for url_file in sorted(folder.glob("*.url")):
        out.setdefault("urls", []).append(
            url_file.read_text(encoding="utf-8", errors="replace")[:120])
    out["screenshots"] = len(list(folder.glob("*.png")))
    out["trace"] = (folder / "trace.zip").exists()

    state = folder / "storage.json"
    if state.exists():
        try:
            saved = json.loads(state.read_text(encoding="utf-8"))
            rows = saved.get("quietDomains") or []
            out["quiet"] = [
                {"domain": r.get("domain"), "phase": r.get("phase"),
                 "type": r.get("type")}
                for r in rows if isinstance(r, dict)
            ][:4]
            out["keys"] = len(saved)
        except Exception as e:
            out["storage_error"] = str(e)[:80]
    return out


def main() -> int:
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "run4.log"
    failures = failures_from(log)

    if not failures:
        print(f"No failures recorded in {log.name}"
              + ("" if log.exists() else " (the log does not exist yet)"))
        return 0

    groups = defaultdict(list)
    for test_id, message in failures:
        # The first sentence is the claim; the values after it differ per test.
        key = re.split(r"[:(]", message, 1)[0].strip()[:90] or "(no message)"
        groups[key].append(test_id)

    print(f"{len(failures)} failures, {len(groups)} distinct causes\n")
    for key, tests in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"[{len(tests)}] {key}")
        for test_id in tests[:6]:
            print(f"      {test_id.split('::')[-1]}")
        if len(tests) > 6:
            print(f"      … and {len(tests) - 6} more")

        found = evidence(tests[0])
        if found:
            if found.get("urls"):
                print(f"      page was on: {found['urls'][0]}")
            if found.get("quiet"):
                print(f"      quietDomains: {found['quiet']}")
            print(f"      evidence: {found['folder']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
