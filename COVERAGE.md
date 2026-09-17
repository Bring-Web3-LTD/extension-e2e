# Coverage — QA plan → walk step

Every line of `QA_TEST_PLAN.md` that the walk can take, and where it takes
it. The step titles are what the run prints, so a failure in the log can be
found here by name.

Three shops in three tabs of one browser for the popup acts; one tab for the
rest. `step` = all three at once · `each` = one after another, tabs open ·
`one` = a single shop.

---

## 1. Popup

### 1.1 The popup appears — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Visit a supported retailer → the popup pops | the offer appears | step |
| Correct values / retailer text (7.7: from the server, nothing unresolved) | the offer text is complete | step |
| Retailer name / displayName shown (7.5) | the offer text is complete | step |
| Terms opens with content | Deal Terms opens and Back returns | step |
| Design per theme, logo / no logo | *not covered — visual, one platform in CI* | |
| Auto-close after ~30 min | *not covered — 30 minutes of wall clock per run* | |

### 1.2 Close — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| X silences for the server-set time (30 min) | close it, in every tab at once → quietDomains says quiet, for thirty minutes, one row each | step |
| Close button = same action | the Close button silences like the X | one |
| Revisit → no popup | revisiting stays silent | each |
| Entry exists, phase quiet, right window | quietDomains says quiet, for thirty minutes, one row each | step |
| Three tabs closing at once lose no row | quietDomains says quiet … one row each | step |

### 1.3 Activate — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Activate → back on the retailer, confirmation with correct content | activate | each |
| Does not pop again for 2h; phase activated; `*.<domain>` wildcard; type kds | quietDomains says activated, for two hours | step |
| Revisit shows the confirmation again | revisiting shows the confirmation again, without asking the server | each |
| Confirmation comes from storage, no popup-check call | same step | each |
| X on the confirmation → phase quiet, timer restarts | X on the confirmation turns it quiet and restarts the timer | step |
| Other retailers still pop normally | one row per shop, none for the others (in the activated-row step); the silence names this shop and no other | step / each |
| Activated terms show the real rate (no `undefinedNaN`) | activate (confirmation text checked for unresolved values) | each |
| Terms from the confirmation | *the activated surface has no terms link in the iframe; covered on the offer* | |
| From the portal; old SDK path | *not covered — needs the portal / a second build* | |
| Activate from the widget, from the bar | widget: activate works from the expanded widget · offerbar: activating from the bar silences the shop | one |

### 1.4 Opt-out — `tests/optout.py`
| Plan | Step | Mode |
|---|---|---|
| Opt-out screen: scope choice, duration, full content | open opt-out → it offers both scopes and every duration | step |
| Nothing changes until Apply | nothing changes until Apply | step |
| After Apply, a confirmation | apply 'this site' — a different duration on each shop | step |
| All sites → no popup on any retailer, for the chosen time | apply 'all sites' 24h / 30d / forever on one shop → every shop stays silent | one + each |
| Next visit shows nothing | revisiting shows nothing | each |
| Back to activation | Back to activation returns to the offer | step |
| Single-site adds a quiet entry; all sites sets the global opt-out | the two apply steps (row per shop / `optOut` range) | |
| 60-day cap removed; forever persists past 60 days | apply 'this site' (forever > 60d) · a forever opt-out outlives sixty days | step / one |
| Expired opt-out cleared lazily | an expired opt-out is ignored and cleared lazily | one |
| Opt-out from the widget, from the bar | widget: clicking the badge opens the offer (opt-out reachable) · offerbar: opt-out from the bar | one |

### 1.5 Wallet — `tests/wallet.py`
| Plan | Step | Mode |
|---|---|---|
| Works without a wallet; `bring_id` saved (§4) | with no wallet the offer still shows | one |
| Connect a wallet → address shown | connecting after the popup forces a fresh activation (address read after connecting) | one |
| Fast path with no wallet | activating with no wallet takes the fast path | one |
| Connect after the popup → not the fast path | connecting after the popup forces a fresh activation | one |
| Fast path with a wallet already connected | connected before the popup: the address shows, the path is fast | one |
| Stays connected | the wallet stays connected on the next shop | one |
| Disconnect → `walletAddress` removed, offer still works | disconnecting removes the address and the offer still works | one |
| Switch the wallet (address change → one reward check) | notifications: a real address change fires exactly one check | one |

### 1.6 Back / forward / refresh — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Re-pops only if still eligible | reload / back / forward re-checks and the offer returns (×3) | each |
| After close, does not re-pop | reload / back / forward does not bring a silenced offer back (×3) | each |
| SPA route change, no duplicates | only one popup per page (the shops are real pages; no route to push) | step |

### 1.7 Revisit after opt-out / activate / close — `tests/popup.py`, `tests/optout.py`
| Plan | Step | Mode |
|---|---|---|
| Silenced ones stay silent | revisiting stays silent (after activate, after close) · revisiting shows nothing (after opt-out) | each |
| Others pop normally | the silence names this shop and no other | each |
| Once the time ends, the popup returns | the offer returns once the window ends | each |

### 1.8 Stand-down — `tests/standdown.py`
| Plan | Step | Mode |
|---|---|---|
| Marker on the URL → no popup | arriving with an affiliate marker on the URL stands down (irclickid / cjevent / ranMID, one per shop) | each |
| Marker only on a redirect hop → no popup | a marker only on a redirect hop stands down (affiliate host + affiliate_id on a 3xx hop) | each |
| Landed URL checked without a chain | the URL-marker step is a direct arrival | each |
| Domain-wide: apex, subdomain, another path | another path and the bare apex are quiet too | each |
| Extend, never shorten | a stand-down extends a silence and never shortens it | one |
| Never overrides our activation | a stand-down never downgrades an activation | one |
| `*.<domain>`, type kdi, right end time (`standDownOffset`) | every stand-down step | |
| Clean arrival still pops | a clean arrival still pops | step |
| OB / TB: stood-down shop gets no bar | offerbar: a stood-down shop gets no bar | one |
| TLD gate | *not covered — needs a retailer on an unserved TLD* | |

### 1.9 Follow-up matchers — `tests/followups.py`, `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Before the thank-you page, no call | before a rule is armed, the thank-you page reports nothing | one |
| Thank-you page reports once, then stops | an armed rule reports once on the thank-you page and stops | one |
| Expired entry does not shadow a valid one | an expired row does not hide a live one | one |
| Delayed popup (Nth visit), count / TTL / scope, persistence, server-driven quietDomains | *not covered — needs a retailer with a rule configured in the environment (admin). Add a step when one exists.* | |
| Only on SDK ≥ 1.8.0 | *not covered — second build* | |

### 1.10 Server-built iframe URL — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Token in the fragment, not the query | the token rides in the fragment, not the query | step |
| Server path / query preserved; env origin override | *not covered — needs a second environment configuration* | |

### 1.11 Self-heal
*Not covered — needs a site that rebuilds the page and deletes the popup; none of the three shops does.*

### 1.12 Top frame only — `tests/popup.py`
| Plan | Step | Mode |
|---|---|---|
| Exactly one popup, in the top frame | only one popup per page | step |
| Third-party frame does not kill the injection | the offer appears (real shops carry ad / chat frames) | step |

---

## 2. Offer bar / Top bar — `tests/offerbar.py`
| Plan | Step |
|---|---|
| Pops with the right retailer, found by inline search, without wallet connect | the bar appears over the results |
| Only one bar; the page makes room and is restored | the bar appears over the results · closing the bar silences the engine and gives the space back |
| Close → silenced (1.2) | closing the bar silences the engine and gives the space back |
| Activate (1.3), confirmation until closed | activating from the bar silences the shop, not Google |
| Opt-out (1.4) | opt-out from the bar |
| Revisit (1.7) | the "no bar on the next search" halves of close and opt-out |
| Hijack protection (1.8) | a stood-down shop gets no bar |
| Activating from the bar silences `*.<retailer>`, not the engine | activating from the bar silences the shop, not Google |
| Analytics carry the server's `triggerType` | the analytics carry the server's trigger type — `keyword` when the environment's list matches the search URL as a registered keyword (type 'k'/'kd'), `domain` when the bar came through the retailer link instead; the walk reads the list the way the SDK does and expects accordingly |
| Found by visiting the retailer directly (KD) with a bar layout | *not covered — no retailer on this environment is configured for the bar on direct visits* |
| Follow-up matchers on the bar (1.9) | *not covered — see 1.9* |

The results page is the walk's own, served on Google's search URL
(`google.com/search?q=c+ondor`) with a link to the retailer. The extension
recognises the search page by its URL, scrapes the links, matches the
retailer, asks the server and injects the bar — all real. Google itself is
not: a fresh browser gets a different page each time (another language, no
link to the retailer, a captcha after a few searches — measured), and none
of that is about the extension.

---

## 3. Notifications — `tests/notifications.py`
| Plan | Step |
|---|---|
| Reward approval: wallet connected | variant reward_approval — "New cashback reward", Details |
| walletless_1: just earned, Connect | variant walletless_1 — Just earned 0.42, Connect |
| walletless_2 | variant walletless_2 — Just earned 0.42, Total 0.73, Connect (see note) |
| walletless_3: earned + claimable + deadline, Claim | variant walletless_3 — Just earned 0.42, Claimable 0.55, deadline, Claim |
| walletless_4: claimable + deadline, Claim + Stop Reminding | variant walletless_4 |
| Amounts, deadline date, buttons per variant | every variant step checks the text, the button and the deadline |
| Amount rounding 0.01–0.1 → 3 decimals | amounts between 0.01 and 0.1 show three decimals, not four |
| X closes; a shown notification is stored and removed on close | closing the notification removes it |
| Stop Reminding | Stop Reminding turns the reminders off |
| Details / Claim → the claim flow; Connect → wallet prompt | *not covered — real funds, a wallet extension* |

Two places where the plan's table and the product differ, checked in the
code rather than assumed:

- **walletless_2** in the table has a deadline and a Connect button. The
  server shows a deadline only once a READY purchase is 122 days old
  (`REMINDER_PURCHASE_STATUSES: ['READY']`, `checkIfInRemindingPeriod`), and
  a READY purchase in that period is also the claimable sum, which makes the
  button Claim. "Expiring but not claimable" cannot occur; the walk's
  walletless_2 is the product's: a new reward plus an older one, Total shown,
  Connect.
- **Rounding**: the table says `0.0457 → 0.046`. The server keeps the first
  significant digit plus one and truncates (`formatToFirstNonZeroPlus`), so
  `0.0457 → 0.045`. Three decimals, never a fourth, never rounded up. The
  walk asserts the product's rule.
- **reward_approval** with a wallet connected uses the simple layout ("New
  cashback reward", Details) and shows no amount.

### 3.1 No request storm
| Plan | Step |
|---|---|
| Unchanged address → no call | a new address checks once; the same address again costs nothing |
| Real address change → one call, marker updated | a real address change fires exactly one check |
| The broadcast is always answered | the broadcast is always answered |
| Failed check backs off 1 hour, no retries | a failed check backs off for an hour |
| Non-JSON / error body → same backoff, no notification | a block page instead of JSON … · an error body with no nextCall … |
| Recovery after the hour | the check resumes once the backoff expires |
| Disconnect / reconnect same → quiet; different → one check | disconnecting removes the address; reconnecting is quiet |
| Active check window skips the server | an active check window skips the server |

---

## 4. Storage
| Plan | Step |
|---|---|
| No wallet → `bring_id` saved | wallet: with no wallet the offer still shows |
| Retailer list downloaded and usable | the run refuses to start until `relevantDomains` is filled; the preflight checks the shops are on it |
| Opt-out cleanup is lazy | optout: an expired opt-out is ignored and cleared lazily |
| `quietDomains` pruned on write, skipped-if-expired on read | popup: an expired row does not hide a live one · the offer returns once the window ends |
| `standDownOffset` used on stand-down | standdown: every step compares the window to it |
| `notificationCheck`, `lastCheckedWalletAddress`, `notification`, `disableReminders` | notifications, throughout |
| Bad-data table (corrupted / missing values) | *not covered by decision: the tool behaves like a user and never writes a broken row* |
| Environment, cache, type codes, country, server-driven quietDomains | *not covered — environment configuration* |

---

## 5–8. Robustness, debug logger, iframe UI, backend
| Plan | Where |
|---|---|
| 5 No network / server down | notifications: a failed check backs off for an hour (a dead network, produced from inside the worker) |
| 5 Corrupted data, missing manifest permissions | *not covered — see §4; a rebuilt extension* |
| 6 Debug logger | *not covered — dev aid* |
| 7.1 Widget | `tests/widget.py`: badge collapsed · click opens · activate works · expanded X and Close silence 30 min · badge X silences 30 min · expanded state survives navigation |
| 7.2 Terms view and agree line | popup: Deal Terms opens and Back returns · the agree line carries both links |
| 7.3 Analytics to the backend, `isWidget` | widget: dismissing the badge is reported as the widget · the badge reports itself on load and on click · the expanded close is not reported as the widget · offerbar: reported as a keyword |
| 7.3 AB variants, 7.4 z-index, 7.5 themes, 7.6 legacy SDK, 7.7 admin default text | *not covered — visual / admin / second build* |
| 8 Backend: portal order, matchers | *not covered — needs the portal* |
