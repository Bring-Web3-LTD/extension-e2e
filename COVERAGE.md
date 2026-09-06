# What this checks, against QA_TEST_PLAN

Every checkable line of the plan, and the test that covers it. Written to be
read next to the plan itself, so a gap is visible rather than inferred.

- **yes** — asserted, and a regression fails the run
- **part** — asserted in part; the missing half is named
- **no** — not covered; the plan item still needs a person

Two things are deliberately out of scope and marked so throughout: the Bring
portal, and per-platform themes. Both were agreed as out for now.

---

## 1. Popup

### 1.1 The popup appears

| Plan item | | Where |
|---|---|---|
| Popup pops on a supported retailer | **yes** | `test_popup_appears`, walk *the offer appears* |
| ...with the correct values and full content | **part** | Asserted: renders, has an activate button, no `undefined`/`NaN`. **Not asserted: the design, or that the cashback figure matches the retailer.** |
| Default text vs retailer-specific text | **no** | Needs the expected text per retailer from the catalogue |
| Retailers with and without a logo | **no** | |
| Terms open with the right content | **yes** | `test_deal_terms_opens_in_popup_and_comes_back` — opens, has text, no `undefinedNaN`, Back returns |
| **Auto-close after ~30 min untouched** | **no** | Needs the popup's own timer moved, not the storage window |

### 1.2 Close the popup (X or Close)

| Plan item | | Where |
|---|---|---|
| X and Close are the same action | **yes** | `test_close_silences_this_retailer_only`, both variants |
| Silences for the server's window | **yes** | Asserted as **30 minutes ±2**, and separately equal to the `time` the server sent on that check |
| Revisit → no popup | **yes** | same test, and walk *revisiting stays silent* |
| quietDomains: entry exists, `phase = quiet`, right window | **yes** | same |

### 1.3 Activate

| Plan item | | Where |
|---|---|---|
| Second popup pops with the right content | **yes** | `test_activate_shows_the_confirmation` |
| You are taken back to the retailer | **part** | The redirect is followed and waited for; the landing URL is not asserted |
| Popup closes on **all open tabs** | **yes — and it does not** | `test_activation_leaves_other_tabs_alone`. The content script only closes when `iframePath === request.path`, and `handleActivate` sends no path. **The plan and the product disagree; the test asserts the product.** |
| Terms from the confirmation | **no** | |
| No popup again for the server's window | **yes** | asserted as **2 hours ±2 min** |
| quietDomains `phase = activated` | **yes** | `test_activate_writes_a_wildcard_entry` |
| X on the second popup → `quiet`, timer restarts | **yes** | `test_dismissing_the_confirmation_switches_to_quiet` |
| Until X, revisiting shows it again | **yes** | `test_activated_confirmation_returns_on_revisit` |
| Other retailers still pop | **yes** | `test_a_silence_in_one_tab_does_not_leak_into_another` |
| From the portal | **no** | out of scope |
| `*.<domain>` on every activation path | **part** | The popup path is asserted. **Fast activation, OB/TB, portal and old SDK are not.** |
| Confirmation comes from storage, **no popup-check call** | **yes** | `test_the_confirmation_comes_back_without_asking_the_server` — counts the calls |
| Reached as an inline link → does not show | **no** | |
| Activated terms show the real rate, not `undefinedNaN` | **part** | The `undefined`/`NaN` check is there; the rate is not compared to the retailer's |

### 1.4 Opt-out

| Plan item | | Where |
|---|---|---|
| Screen opens with scope, duration, full content | **yes** | `test_optout_screen_offers_scope_and_duration` |
| Nothing changes until Apply | **yes** | `test_nothing_happens_until_apply` |
| Confirmation after Apply | **yes** | `test_optout_this_site_silences_for_the_time_chosen` |
| All sites → no popup anywhere | **yes** | `test_optout_all_sites_silences_everything` |
| You can close the confirmation | **no** | |
| Back to activation returns | **yes** | `test_back_to_activation_returns_to_the_offer` |
| Single site adds a quiet entry; all sites sets the global | **yes** | both tests assert the other did not happen |
| Expired opt-out clears lazily, on the next write | **yes** | `test_expired_optout_is_cleared_lazily`, `test_quiet_domains_are_pruned_on_write_not_on_read` |
| **60-day cap removed** — forever survives past 60 days | **yes** | `test_forever_optout_outlives_sixty_days` moves the clock 61 days |
| Each duration honoured | **yes** | run per choice: 24h, 30d, forever — and the walk gives each shop a different one at the same time |

### 1.5 Wallet

| Plan item | | Where |
|---|---|---|
| Works with no wallet | **yes** | `test_popup_works_with_no_wallet_connected` |
| Connect a wallet | **yes** | `test_connecting_a_wallet_shows_its_address` |
| **Switch the wallet** | **yes** | `test_switching_the_wallet_updates_what_the_popup_shows` |
| **Disconnect** | **yes** | `test_disconnecting_clears_the_wallet_and_the_offer_still_works` — and the offer keeps working after |
| Fast path when the popup had a wallet, or none | **yes** | `test_activation_is_local_when_no_wallet_is_connected` counts the activate call |
| No fast path when a wallet arrives after the popup | **yes** | `test_connecting_after_the_popup_forces_a_fresh_activation` |

### 1.6 Back / forward / refresh / in-app navigation

| Plan item | | Where |
|---|---|---|
| They re-check, and re-pop when still eligible | **yes** | `test_navigation_re_pops_when_the_retailer_is_still_eligible`, walk *reload/back/forward re-checks* |
| After activate or close they do not re-pop | **yes** | `test_navigation_does_not_resurrect_a_closed_popup`, walk *does not bring a silenced offer back* |
| SPA route change: old popup closes, no duplicates | **yes** | `test_spa_route_change_leaves_exactly_one_popup` |

### 1.7 Revisit after opt-out / activate / close

| Plan item | | Where |
|---|---|---|
| Silenced stays silent, others pop, and it returns when the window ends | **yes** | `test_silence_ends_when_its_window_does`, `test_the_silence_is_what_keeps_it_quiet` — the row is removed and the offer comes back, which is what proves the silence was the cause |

### 1.8 Hijack protection / stand-down

| Plan item | | Where |
|---|---|---|
| Existing marker on the URL → no popup, `isValid = false` | **yes** | `test_no_popup_on_an_affiliate_arrival` (4 markers); the walk also asserts the server's verdict |
| Marker only on an intermediate 3xx hop | **yes** | `test_no_popup_when_the_marker_is_only_on_a_hop` — real 302s |
| Marker on the landed URL, no redirect | **yes** | `test_no_popup_on_an_affiliate_arrival` |
| Domain-wide quiet: apex, subdomain, other path | **yes** | `test_stand_down_quiets_the_whole_domain` |
| Extend, never shorten | **yes** | `test_stand_down_extends_but_never_shortens` |
| Never overrides our own activation | **yes** | `test_a_stand_down_does_not_downgrade_an_activation` |
| **TLD gate** — an unserved TLD is skipped | **no** | |
| quietDomains: `*.<domain>`, type `kdi`, right end time | **yes** | asserted against `standDownOffset` read from storage |
| Applies to the bar | **yes** | `test_a_stood_down_retailer_gets_no_bar` |
| **Stops inline search on a stood-down domain** | **no** | |

### 1.9 Follow-up matchers

| Plan item | | Where |
|---|---|---|
| Thank-you page reports once, then stops | **yes** | `test_the_thank_you_page_reports_once_and_stops` |
| Nothing reported before the trigger | **yes** | `test_it_does_not_report_before_the_trigger` |
| Counter decrements per matching navigation | **yes** | `test_the_counter_goes_down_one_navigation_at_a_time` |
| Only matching navigations cost budget | **yes** | `test_navigations_outside_the_scope_cost_nothing` |
| Rule dropped when the budget runs out | **yes** | `test_the_rule_leaves_when_its_budget_runs_out` |
| TTL drops the rule | **yes** | `test_a_rule_past_its_ttl_is_dropped` |
| tab scope vs browser scope | **yes** | two tests |
| Survives a reload and a background restart | **yes** | `test_the_rule_and_its_counter_survive_a_worker_restart` |
| **Server-driven quietDomains** | **no** | |
| **Only on SDK ≥ 1.8.0** | **no** | |
| Expired entry does not shadow a valid one | **yes** | `test_expired_row_does_not_hide_an_active_one` |

*Note: the rules are written into storage rather than armed by the server —
there is no way to ask it to. That gives up "the server sends the right rule"
and keeps everything the client is responsible for.*

### 1.10 Server-built iframe URL

| Plan item | | Where |
|---|---|---|
| Server path and query preserved, client fills the rest | **yes** | `test_client_fills_in_only_what_the_server_left_out` |
| Token in the fragment, not the query | **yes** | `test_token_rides_in_the_fragment` |
| Loads and renders across variants | **part** | Offer, activated, offerbar and notification are exercised; **framed is not** |
| Env override swaps only the origin | **no** | |

### 1.11 Self-heal

| Plan item | | Where |
|---|---|---|
| Host wipes the popup, it comes back | **yes** | `test_popup_survives_a_host_that_wipes_it` |
| Bounded — no endless flicker | **part** | One popup after healing is asserted; the three-attempt ceiling is not counted |
| Our own close does not re-inject | **yes** | `test_our_own_close_is_not_treated_as_a_wipe` |
| Injected before `<head>` exists | **no** | |

### 1.12 Top frame only

| Plan item | | Where |
|---|---|---|
| Same-origin iframe → exactly one popup | **yes** | `test_one_popup_on_a_page_with_a_same_origin_frame` |
| Third-party frame (captcha) → the popup still appears | **yes** | `test_popup_still_appears_beside_a_third_party_frame` |
| Applies to every surface | **part** | Popup and bar; framed mode is not |

---

## 2. Offer bar / Top bar

| Plan item | | Where |
|---|---|---|
| Appears over search results | **yes** | `test_the_bar_appears_over_search_results`, walk act 3 |
| Without the wallet-connect option | **yes** | `test_the_bar_offers_no_wallet_connect` |
| Only one bar at a time | **yes** | `test_only_one_bar_at_a_time` |
| The page makes room, and gets it back | **yes** | `test_the_page_makes_room_and_gets_it_back` |
| Close → silenced | **yes** | `test_closing_the_bar_silences_the_retailer_not_the_engine` |
| Activate | **yes** | `test_activating_from_the_bar_silences_the_retailer` |
| Opt-out | **yes** | `test_the_bar_has_an_opt_out` |
| Hijack protection | **yes** | `test_a_stood_down_retailer_gets_no_bar` |
| **Keyword search silences the retailer, not the engine** | **yes** | both close and activate assert Google was left alone |
| **triggerType `keyword`** | **yes** | `test_a_keyword_search_is_reported_as_a_keyword` |
| **OB vs TB told apart** | **no** | Both are the same surface here |
| Found by direct visit (KD) vs inline search | **no** | Only the keyword path |
| Placed next to a named element; nothing if absent | **no** | |
| Follow-ups in the bar | **no** | |

---

## 3. Notifications

| Plan item | | Where |
|---|---|---|
| Reward approval → Details | **yes** | `test_notification_variant[reward_approval]`, seeded |
| walletless_1 → Connect | **yes** | seeded |
| walletless_2 → Connect, with deadline | **yes** | seeded |
| walletless_3 → Claim | **yes** | seeded |
| walletless_4 → Claim + Stop Reminding | **yes** | seeded, with a reminder row |
| Amounts and deadline date | **part** | `undefined`/`NaN` are rejected; the figures are not compared to the seeded rows |
| **Details / Claim → the claim flow, funds arrive** | **no** | on-chain; out of reach |
| **Connect → login prompt** | **no** | |
| **Stop Reminding actually stops** | **yes** | `test_stop_reminding_turns_the_reminders_off` — asserts `disableReminders` |
| **X closes the notification** | **yes** | `test_closing_the_notification_removes_it` — and the stored copy goes with it |
| Rounding: 0.01–0.1 shows 3 decimals | **yes** | `test_small_amounts_round_to_three_decimals` |
| Checked more often after activation, throttled otherwise | **yes** | `test_an_active_check_window_skips_the_server` |

### 3.1 No notification-request storm

| Plan item | | Where |
|---|---|---|
| Unchanged address → no call | **yes** | `test_repeated_broadcasts_of_the_same_address_make_one_check` |
| Real change → exactly one call | **yes** | `test_a_real_address_change_fires_exactly_one_check`; the walk asserts **== 1** |
| The response always comes back | **yes** | `test_the_broadcast_is_always_answered` |
| Failed check backs off one hour | **yes** | `test_a_failed_check_backs_off_for_an_hour` — and rejects the old `[now, NaN]` |
| Non-JSON / error body with no `nextCall` | **yes** | `test_a_bad_reply_backs_off_the_same_way`, both |
| Recovery after the hour | **yes** | `test_the_check_resumes_once_the_backoff_expires` |
| Disconnect / reconnect | **yes** | `test_disconnecting_removes_the_address_and_reconnecting_is_quiet` |

---

## 4. Storage

| Plan item | | Where |
|---|---|---|
| A user id exists without a wallet | **yes** | `test_first_run_writes_its_own_identity` |
| Retailer list matches, with its type codes | **yes** | `test_the_retailer_list_and_its_settings_arrive` |
| `envName` points at the environment under test | **yes** | `test_the_environment_is_the_one_under_test` |
| Opt-out clears lazily | **yes** | see 1.4 |
| Corrupted list re-downloads | **yes** | `test_a_corrupted_retailer_list_is_re_downloaded` |
| Deprecated keys are gone | **yes** | `test_deprecated_keys_are_gone_after_the_upgrade` |
| **`extensionMemoryTest` written on startup** | **yes — and it is not** | `test_storage_self_test_ran`. `canSaveToMemory` is defined in the SDK and **never called**; the string does not appear in the built bundle. |
| No unexpected keys | **yes** | `test_every_expected_key_is_accounted_for` |
| Per-key bad-data behaviour, all 23 | **part** | `quietDomains` and `relevantDomains` only |
| Wallet address shows that wallet's history | **no** | |
| Type codes k / kd / a / kds | **part** | Read and matched on; not asserted per code |
| Dynamic URLs, base URLs, sub-domains | **part** | The wildcard covers subdomains; dynamic URLs are not varied |
| Not in this country → `isValid = false`, silenced | **no** | |

---

## 5. Robustness

| Plan item | | Where |
|---|---|---|
| No network → no crash, recovers | **yes** | `test_no_network_does_not_crash_the_extension`, `test_the_flows_work_again_once_the_server_is_back` |
| Corrupted / missing saved values | **part** | Two keys, not all |
| **Missing permission → init fails by name** | **yes** | `test_init_fails_by_name_when_a_permission_is_missing`, one per required permission |

---

## 6. Debug logger

| Plan item | | Where |
|---|---|---|
| Off by default | **yes** | `test_the_logger_is_silent_by_default` |
| Level filter | **yes** | `test_the_logger_respects_its_level` |
| Bad or unset value is silent | **yes** | `test_an_unrecognised_level_logs_nothing` |
| A forever opt-out does not break the logs | **yes** | `test_a_forever_optout_does_not_break_logging` |
| **Live toggle without a reload** | **yes** | `test_the_logger_can_be_turned_on_without_a_reload` |

---

## 7. Iframe UI

### 7.1 Widget

| Plan item | | Where |
|---|---|---|
| Badge appears collapsed, pulsing | **yes** | `test_the_badge_appears_collapsed` — asserts the frame is badge-sized |
| Click → animates to the full offer | **yes** | `test_clicking_the_badge_opens_the_offer` — asserts the frame grows |
| **Badge X is a dismiss, not a close** | **yes — and it silences** | `test_the_badge_dismiss_does_not_silence_the_retailer`. Dismissing writes `quietDomains`, so a badge the user never opened costs them the offer for half an hour. |
| Badge X sends `popup_close` with `isWidget` | **yes** | `test_dismissing_the_badge_reports_it_as_the_widget` |
| Expanded X behaves like a standard close | **yes** | `test_the_expanded_offer_close_does_silence` — 30 minutes, no collapse back |
| Expanded state remembered per platform | **yes** | `test_the_expanded_state_survives_navigation` |
| Activate / opt-out / terms from the widget | **yes** | three tests |
| The X circle is pure black in both themes | **no** | visual |
| Clicks pass through during the bounce-in | **no** | |

### 7.2 Terms view and agree line

| Plan item | | Where |
|---|---|---|
| Deal Terms opens the in-popup view, with Back | **yes** | `test_deal_terms_opens_in_popup_and_comes_back` |
| Cashback rate injected into the terms | **part** | Only that it is not `undefinedNaN` |
| Terms of Use link present and separate | **yes** | `test_popup_has_agree_line_with_both_links` |
| **Privacy link removed** | **yes** | same test |
| On every platform theme | **no** | out of scope |

### 7.3 Backend AB variants, no Google Analytics
**no** — neither the assigned variant nor the absence of gtag is checked.

### 7.4 Server-driven z-index
**no**

### 7.5 Per-platform themes
**no** — out of scope

### 7.6 Legacy-SDK forever opt-out
**part** — the ≥1.8.0 path is asserted (`test_forever_optout_outlives_sixty_days`);
the clamp-to-60-days-and-tag-`a` path on an older SDK is not.

### 7.7 Offer text comes only from the server
**no**

---

## The count

| | |
|---|---|
| Fully covered | **~67** |
| Partly covered | **~18** |
| Not covered | **~17** |

Roughly two thirds of the plan runs without a person. What still needs eyes:
the visual items (design, logos, themes, colours), the on-chain claim, the
portal, and the handful of behaviours listed as **no** above.

## What it found

Two things the plan says should be true and the product does not do:

1. **`extensionMemoryTest` is never written** — `canSaveToMemory` has no caller
   anywhere in the SDK, and the key never appears in the built bundle.
2. **The widget badge's X silences the retailer** — the plan makes it a dismiss,
   distinct from the expanded offer's close.

And one place where the plan and the product disagree, resolved in the
product's favour with the reason written down: **activating does not close the
popup in other tabs** (`test_activation_leaves_other_tabs_alone`).
