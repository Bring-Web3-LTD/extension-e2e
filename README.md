# extension-e2e

End-to-end checks for the Bring browser extension. One command answers one
question: **is the extension safe to release right now.**

```bash
python run.py
```

That brings up an environment, loads the extension it built into a real Chrome,
walks the QA plan against live retailers, and prints `PASS` or the list of what
broke.

It is deliberately small and fast. It is not the QA automation framework in
`qa-automations` — that one is broader (portal, countries, wallet platforms,
history, dashboards) and still being built. This is the extension only, usable
today.

---

## How it runs

The way a person would, in **one browser**:

1. Three shops open in **three tabs**. Every popup step happens on all three
   — the offer appears, activate, close, opt-out, stand-down — and after each
   step the shared `quietDomains` list is read and checked. The list is
   cleared only between scenarios, after every tab has finished the step.
2. The follow-up rule, the wallet, the widget (on the one shop that has it)
   and the offer bar (a search-results page linking to a retailer) each get a
   tab of their own.
3. The notifications: the reward check and its backoffs, then the five
   variants, produced by seeding purchases into the environment's database.

Steps that only read or click run on the three tabs **at once**. Steps that
end in a popup check — navigating to the shop, activating, revisiting — run
**one tab after another**, with the tabs still open: three checks in the same
instant have the server answering one with `quietDomainsChanged`, and the SDK
then replaces its whole list, wiping what the other tabs just wrote. Nobody
clicks Activate in three tabs in the same tenth of a second either.

Every wait is for a **state**, not a time — a row appearing, a phase
changing, a frame rendering — with a generous ceiling. Fixed waits are used
only where the claim is an absence ("no popup appears").

---

## What it covers

| Act | QA plan | File |
|---|---|---|
| The popup: appears, text, terms, activate, close, navigation, silence expires, expired rows | 1.1 1.2 1.3 1.6 1.7 1.9 1.10 1.12 7.2 7.7 | `tests/popup.py` |
| Opt-out: this site × 3 durations, all sites × 3, nothing until Apply, lazy cleanup | 1.4, 4 | `tests/optout.py` |
| Stand-down: marker on the URL, on a hop, whole domain, extend never shorten, never downgrade | 1.8 | `tests/standdown.py` |
| Follow-up rule: the thank-you page reports once and stops | 1.9 | `tests/followups.py` |
| Wallet: none, fast path, fresh activation after connecting, stays connected, disconnect | 1.5 | `tests/wallet.py` |
| Widget: badge, expand, both closes, persistence, `isWidget` analytics | 7.1 7.3 | `tests/widget.py` |
| Offer bar: appears, close, activate, opt-out, keyword trigger, stood-down shop | 2 | `tests/offerbar.py` |
| Notifications: request storm, backoffs, recovery, the five variants, rounding, close, stop reminding | 3 3.1 | `tests/notifications.py` |

`COVERAGE.md` maps every line of the plan to a step, and lists what is not
covered and why.

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
the CI role). The walk needs ECS, CloudFormation and S3 read access.

---

## Running it

```bash
python run.py                       # everything, in order
python run.py --only popup          # one act (popup, optout, standdown, followups,
                                    #          wallet, widget, offerbar, notifications)
python run.py --headless            # how CI runs it
python run.py --skip-env            # environment is already up, do not ask AWS
python run.py --reuse-extension     # and do not re-download it either
python run.py --skip-preflight      # do not check the retailers first
python run.py --retailers https://a.com,https://b.com,https://c.com
```

### The environment

Every run calls the same `dev-env-deployer` ECS task the QA automation uses:

1. Look for the environment's CloudFormation stack.
2. Up and healthy → **reuse it**, after checking it actually serves retailers.
3. Mid-deploy → wait for whoever is deploying it.
4. Broken → refuse, and print the deployer command that removes it. This tool
   never deploys over or repairs an environment; that belongs to the deployer.
5. Absent → deploy it from `main` + `main`, wait for CloudFormation, then wait
   for it to actually serve a retailer list.

Then the mock extension that environment built is pulled from S3 and loaded.
`BRING_BACKEND_BRANCH` and `BRING_FRONTEND_BRANCH` pick the branches.

### The preflight

A named retailer goes stale the moment the environment stops carrying it, and
then every popup step fails with "no popup" — correct product behaviour,
reported as a bug. So each run asks first: navigate to each retailer and watch
whether the extension sent a `/check/popup` at all. Nothing usable and the run
stops with exit code `2` and says which retailer to replace.

---

## When something fails

Every step prints one line per shop. A failed step leaves evidence in
`artifacts/<act>/<NN-step>/`:

- `<shop>.png` and `<shop>.url` — the page at the moment of failure
- `storage.json` — every `bring_*` value, which is usually the whole explanation

A shop that answered with a bot check, or a Google results page the extension
did not recognise, is printed with `--` and leaves the same evidence — it is
not a failure, because nothing was observed, but the page is worth a look.

Exit codes: `0` pass · `1` something failed · `2` no usable retailer ·
`3` the environment could not be brought up (not a product finding).

---

## How it is put together

```
run.py              environment, extension, preflight, the walk, verdict
bring/
  walk.py           one browser, the tabs, step/each/one, the tally, evidence
  config.py         ARNs, branches, names, keys
  env.py            ECS deploy-or-reuse, and the extension zip
  browser.py        Chrome with the extension
  storage.py        read/write bring_* through the SDK's own bringCache
  popup.py          finding the iframe; every selector in one place
  pages.py          controlled markup and redirects on a real retailer's URL
  netspy.py         count and steer the extension's own fetches
  retailers.py      which shops to walk through
  preflight.py      are those shops still retailers on this environment
  db.py, seed.py    the environment's database, and the purchases that
                    produce each notification variant
tests/
  steps.py          arrive, activate, close, opt out, revisit
  <act>.py          one file per area of the plan; each is a list of steps
```

Three things are worth knowing before changing it.

**Storage is written, not just read.** Half the QA plan is time-gated — a
30-minute silence, a two-hour stand-down, an hour of backoff, a "forever"
opt-out that has to outlive 60 days. Nothing can wait those out, and all of
them are timestamps: the walk moves the clock. Writes go through `bringCache`
rather than `chrome.storage` directly, because the SDK reads from its
in-memory cache first. Only rows in the shape the SDK itself writes are ever
written; the tool behaves like a user, never like a corrupted profile.

**Some pages are ours.** A coupon-site redirect through an affiliate host
cannot be asked for on demand, so Playwright serves the entry page and the
3xx hop (`bring/pages.py`): the navigation chain, the domain and the
extension's view of it stay real. The offer bar's search-results page is
served the same way, on Google's own search URL, with a link to the retailer:
Google gives a fresh browser a different page each time (another language,
no retailer link, a captcha after a few searches — measured), and the bar is
about what the extension does with a results page, not about Google.

**Network assertions come from inside the worker.** The calls under test are
made by the MV3 service worker; `bring/netspy.py` wraps `fetch` there, which
also lets the walk produce the three failures section 3.1 is about. MV3
recycles the worker freely and the wrapper with it, so a missing request is
never taken as proof on its own — the walk looks for the mark the product
leaves in storage instead.
