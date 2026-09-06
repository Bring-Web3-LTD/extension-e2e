# extension-e2e

End-to-end checks for the Bring browser extension. One command answers one
question: **is the extension safe to release right now.**

```bash
python run.py
```

That brings up an environment, loads the extension it built into a real Chrome,
drives the popup and the notification flows against live retailers, and prints
`PASS` or the list of what broke.

It is deliberately small and fast. It is not the QA automation framework in
`qa-automations` — that one is broader (portal, offer bar, countries, wallet
platforms, history, dashboards) and still being built. This is the extension
only, usable today.

---

## What it runs

| Area | QA plan | File |
|---|---|---|
| Popup appears, closes, silence is scoped and expires | 1.1, 1.2, 1.6, 1.7, 7.2 | `tests/test_popup.py` |
| Activation, confirmation, wildcard silence, phase | 1.3 | `tests/test_activate.py` |
| Opt-out scope, duration, the removed 60-day cap | 1.4, 7.6 | `tests/test_optout.py` |
| Hijack protection and stand-down | 1.8 | `tests/test_standdown.py` |
| Server-built iframe URL, token in the fragment | 1.10 | `tests/test_iframe_url.py` |
| Self-heal, top-frame-only injection, SPA routes | 1.11, 1.12, 1.6 | `tests/test_injection.py` |
| Wallet states and the fast activation path | 1.5 | `tests/test_wallet.py` |
| Notification request storm, backoff, recovery | 3.1 | `tests/test_notifications.py` |
| Saved `bring_*` state, all 23 keys | 4 | `tests/test_storage.py` |
| No network, corrupt data, permissions, debug logger | 5, 6 | `tests/test_robustness.py` |

**Not covered yet**, and honest about it:

- The five notification variants (section 3). They are chosen by fields the
  server signs into the notification token, and those come from rows in the
  environment's `purchases` table. Those tests carry the `needs_db` marker and
  skip with an explanation rather than pretending.
- Follow-up matchers — thank-you page, pop-on-the-Nth-visit (section 1.9).
  The rule is armed by the server; the client half is covered where it can be
  (expired rows not shadowing live ones).
- Offer bar / top bar (section 2), portal, wallet platform themes, countries.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
playwright install chromium

cp .env.example .env            # then fill in ECKO_API_KEY
```

AWS credentials come from the usual place (`aws configure`, `AWS_PROFILE`, or
the CI role). The suite needs ECS, CloudFormation and S3 read access.

---

## Running it

```bash
python run.py                       # everything
python run.py -m popup              # one area, by marker
python run.py -k standdown          # one file
python run.py --headless -n 6       # how CI runs it
python run.py --skip-env            # environment is already up, do not ask AWS
python run.py --reuse-extension     # and do not re-download it either
```

Markers: `popup`, `notification`, `storage`, `robustness`, `needs_db`.

### The environment

Every run calls the same `dev-env-deployer` ECS task the QA automation uses:

1. Look for the environment's CloudFormation stack.
2. Up and healthy → **reuse it**, after checking it actually serves retailers.
3. Mid-deploy → wait for whoever is deploying it.
4. Broken → refuse, and print the deployer command that removes it. This tool
   never deploys over or repairs an environment; that belongs to the deployer,
   which knows about the shared database, base-path mappings and secrets a
   hand-rolled cleanup would silently skip.
5. Absent → deploy it from `main` + `main`, wait for CloudFormation, then wait
   for it to actually serve a retailer list.

Then the mock extension that environment built is pulled from S3 and loaded.

Both branches are `main` today. `BRING_BACKEND_BRANCH` and
`BRING_FRONTEND_BRANCH` change that without touching code, which is how this
becomes a pre-merge check later.

---

## When something fails

`run.py` ends with the list of failed checks and a one-line reason each.
Per-failure evidence lands in `artifacts/<test>/`:

- `trace.zip` — open with `playwright show-trace`; every action, DOM snapshot
  and network call
- `storage.json` — every `bring_*` value at the moment of failure, which is
  usually the whole explanation
- `page-N.png` and `page-N.url`

Passing tests leave nothing behind.

Exit codes: `0` pass · `1` something failed · `3` the environment could not be
brought up (not a product finding).

---

## How it is put together

```
run.py              environment, extension, pytest, verdict
bring/
  config.py         ARNs, branches, names, keys
  env.py            ECS deploy-or-reuse, and the extension zip
  browser.py        Chrome with the extension; one profile per test
  storage.py        read/write bring_* through the SDK's own bringCache
  popup.py          finding the iframe; every selector in one place
  pages.py          controlled markup served on a real retailer's URL
  netspy.py         count and steer the extension's own fetches
  retailers.py      which shops to test against
tests/              one file per QA plan area
```

Three things are worth knowing before changing it.

**Storage is written, not just read.** Half the QA plan is time-gated — a
30-minute silence, a two-hour stand-down, an hour of backoff, a "forever"
opt-out that has to outlive 60 days. No suite can wait those out, and all of
them are timestamps. `storage.expire_quiet` and `storage.expire_key` move the
clock instead. Writes go through `bringCache` rather than `chrome.storage`
directly, because the SDK reads a value from its in-memory cache first — a raw
write lands in the browser but not in the extension.

**Some pages are ours.** A shop does not hydrate-and-wipe on demand, does not
always carry a captcha frame, and has no route we can push. So Playwright
answers the retailer's own URL with markup we wrote (`bring/pages.py`): the
navigation, the domain and the extension's view of it stay real, only the DOM
is ours. Anything asserting on real retailer content must not use these.

**Network assertions come from inside the worker.** The calls under test are
made by the MV3 service worker, and whether route interception sees a worker's
requests varies by browser build — a counter that silently records nothing
makes "the fix works" and "the test is broken" look identical.
`bring/netspy.py` wraps `fetch` on the worker instead, which also lets a test
produce the three failures section 3.1 is about: a dead network, a WAF block
page, and an error body with no `nextCall`.

---

## Retailers

Two by default (`bring/retailers.py`), and two is the minimum: close, activate
and opt-out all have to prove the silence they wrote applies here and *not* to
another shop. With one site, half of every scope assertion quietly does not run.

A retailer is only a valid target while it is live in the country the browser
is in — a shop with no local offer correctly returns no popup, and reads as a
product failure if the list has gone stale. Override without editing code:

```bash
BRING_RETAILERS=https://www.aliexpress.com,https://www.iherb.com python run.py
```
