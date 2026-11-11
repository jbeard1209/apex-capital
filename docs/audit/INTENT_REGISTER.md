# Intent Register

This register lists every feature Jack asked for or that was built. For each one it gives
what the feature is **meant** to do, where it lives, what triggers it, and **how we would
know it is broken**.

Sources: project memory (all 70+ notes), CLAUDE.md, docs/ARCHITECTURE.md,
docs/OPERATIONS.md, docs/POSTMORTEMS.md, institutional-tracker/CONTROLS.md, the VM crontab
(read 2026-11-11, 101 lines), and the project threads through 2026-11-11.

Legend for the **Trigger** column:
- `VM cron` is a line in the VM crontab. Times are UTC. `1-5` means weekdays.
- `Mac launchd` is a `~/Library/LaunchAgents/com.apexcapital.*` job.
- `called by X` means it has no schedule of its own and runs only when X runs.

The **"Broken looks like"** column holds the observable symptom. **Alarm today** names the
control that would currently catch it, or `NONE`, which means it is on
[LIVENESS_GAPS.md](LIVENESS_GAPS.md).

> Status as of 2026-11-11: VM deployed sha `49c18f1` (2026-11-09T23:17Z), up 89 days,
> disk 79%. The repo's main branch is at `49c18f1`.

---

## 1. Money-moving: equities (Alpaca paper account)

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 1.1 | Daily pipeline | Runs the full firm: data, research, picks, manager, CRO, execution, reports. | `main.py` | VM cron 13:45, 16:00, 18:00, 20:15 weekdays | No new `run_*.log`; no fills on trading days; stale `state.json` | healthchecks.io ping (`APEX_HEALTHCHECK_URL`, main.py:2092). This is the **only** external dead-man's switch. Canary also checks. |
| 1.2 | Single writer | Only the VM places orders. The Mac must never trade the shared account. | `single_writer.py` | called by every order path | Orders from two hosts; duplicate buys | `two_brains` check via broker client_order_id prefix (memory: two_brains_one_account) |
| 1.3 | Order choke point | Every order goes through one gate that enforces the risk limits: 7% max position, cash floor, CRO block. | `risk_gate.py`, `alpaca_trader.py::submit_buy/submit_sell` | called by every order path | A broker call that bypasses the gate (there were 15 call sites in Aug) | `test_pipeline_smoke.py` choke-point test; guardrail_on_one_path |
| 1.4 | Manager | Reviews picks, sizes them, and executes. Respects the pick cap (trial 15→20) and the position count. | `manager_agent.py` | called by main.py | Picks APPROVED but no order; orders at qty 0 | Partial: reconciliation_check |
| 1.5 | CRO risk bulletin | HIGH or CRITICAL blocks **all** buys. A cleared alert must stop blocking. | `main.py` (CRO section), `risk_manager_agent.py` | main.py; VM cron 14:00, 21:00 | Buys blocked for weeks by a stale alert (1,496 blocked buys, memory: a-record-that-outlived-its-force) | Partial: alert-currency fix; no "blocked buys > N days" alarm |
| 1.6 | Max-hold exits | Sell a position after its max hold, measured from the **real** entry date at the broker. | `alpaca_trader.py`, entry_dates | main.py | Sells too early: a re-bought position gets dated from the order-window start (**OPEN chairman item, HIGH, overdue 7d**); never sells (13 positions, Sept) | coherence `positions_conserve` (currently VIOLATED: 1 ledger entry) |
| 1.7 | Short book | Open **and cover** shorts. `submit_sell` must accept a cover. | `alpaca_trader.py`, `tech_short_pod.py` | VM cron 13:20, 17:20, 20:20 weekdays | Shorts open but never close (qty<=0 rejection, weeks in 2026) | NONE specific |
| 1.8 | Reconciliation | The broker's positions and cash equal the ledger's. | `reconciliation_check.py` | VM cron 14:10, 16:25, 18:25, 20:40 weekdays | Drift that is not explained | Itself (emails) |
| 1.9 | Risk-limit governor | Derives the limits from data. Tightening is automatic; loosening needs sign-off (asymmetric). | `risk_limit_governor.py`, `derive_risk_limits.py` | VM cron 12:40 weekdays; Sun 08:00 | Limits loosened silently; governor stale | signoff_ledger |
| 1.10 | Pre-market check | Sanity-checks the account, data, and market calendar before the open. | `premarket_check.py` | VM cron 12:30 weekdays | No report; holiday trading | NONE (no heartbeat) |
| 1.11 | Execution quality | Measures slippage and fills against the decision price. | `execution_quality.json` producer | main.py | Stale file | freshness_sentinel |
| 1.12 | Catch-up | If the Mac missed a run (asleep), catch up. **Never** trades (single-writer). | `catch_up.py`, Mac launchd `com.apexcapital.catchup` | Mac launchd | Ran main.py 20×/day judging itself by a log it never wrote (fixed) | NONE |

## 2. Money-moving: pods

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 2.1 | Pod budgets | Each pod's budget comes from **real** cash: cash minus short liability minus the 20% floor, then split with a 25% floor and 60% cap per pod. | pod budget code (PRs #20, #24) | called by pod boards | Budgets computed from equity or a constant; a pod gets 0 forever | pod_health_monitor (partial) |
| 2.2 | Pod 1 R&D | R&D team leader proposes ideas, reacts to market events, runs the price-theory lens. | `rd_team_leader.py`, `rd_market_event_monitor.py`, `rd_price_theory_agent.py`, `rd_pod_board.py` | VM cron 05/08/16:00, 19:15, `*/30` in market hours (triggered), 14:30 pod board | Pod board approves nothing for weeks; ideas never become orders | pod_health_monitor 15:30 |
| 2.3 | Pod 2 ML | Turns model scores into pod trades. **Idle** as of Nov. | `ml_pod_board.py`, `ml_rd_bridge_agent.py` | VM cron 14:45 weekdays; bridge 05:30/08:30/16:30/19:45 | Idle forever with nobody deciding that it should be | NONE (idle is not an alarm) |
| 2.4 | Pod 3 ETF / Macro | ETF research, ETF pod board, macro regime. Respects the 7% position cap. | `etf_research_agent.py`, `etf_pod_board.py`, `macro_regime_model.py` | VM cron (several daily) | Position over 7%; no ETF trades | risk_gate cap |
| 2.5 | FI pod | Fixed income: credit conditions, risk monitor, executor. Its verb is "BUY". | `fi_pipeline.py`, `fi_risk_monitor.py`, `fi_position_monitor.py`, `fi_executor` | VM cron 14:30, 15:15 weekdays; quarterly | Orders APPROVED and never executed (66 days of an unknown verb, memory: unknown-verb-approved-nothing) | Partial |
| 2.6 | FE pod (First Eagle) | Deep-value intrinsic value: EPV-anchored, debt verified, 25–50% margin of safety. | `fe_pipeline.py` | VM cron 15:00 weekdays | Picks with bad fundamentals (ABBV P/E 256) | data_integrity_sentinel, fundamentals_sanity |
| 2.7 | KR tech-short pod | Korea / AI hardware. Verdict is **DO NOT SHORT** extended names (the anti-chasing law). | `tech_short_pod.py` | VM cron | Shorts names that are extended | NONE |
| 2.8 | Hedge exemption | An exemption counts as a success only if it beats SPY/QQQ. | `hedge_exemption_outcomes.json` | HR / IRM | A losing exemption graded as a success | NONE automated |

## 3. Money-moving: Kalshi / prediction markets

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 3.1 | Scanner | Finds near-certainty markets. Measures calibration **before** betting. Zero is not a price. | `kalshi_scanner.py`, `kalshi_client.py` | VM cron 02/14/18/22:10 | No report; counts book-less markets as priced | kalshi_watchdog 12:15 |
| 3.2 | Calibration | Resolves settled markets and grades the predicted against the realized rate. | `kalshi_calibration.py` | VM cron 11:45 (emails) | n stops growing | kalshi_watchdog |
| 3.3 | Mirror strategy | Proven only in the 98–100c × 1–7 day slice (n=2695). Paper only. Arming it is **Chairman-only**. | `kalshi_mirror_paper.py`, `data/.kalshi_armed` | VM cron 11:55 | Armed without Jack; trades outside the slice | arm-gate file + chairman carve-out |
| 3.4 | Copy-trader | Copies selected traders. Caps: $1 per trade, $3 per day, 15–85¢ band. | `kalshi_copytrade.py` | VM cron every minute (`--run-for 55`) | Not trading while armed; trading while halted; over the caps | kalshi_copytrade_manager |
| 3.5 | Copy-trade scout | Picks which traders to copy. | `kalshi_copytrade_scout.py` | VM cron 13:30 | No refresh; copying traders whose record went stale | NONE |
| 3.6 | Copy-trade manager | Runs every 15 minutes. Loss breaker at −$6 writes the halt file. | `kalshi_copytrade_manager.py`, `data/.kalshi_copytrade_halted` | VM cron `*/15` | Breaker never fires; halt never clears | Itself |
| 3.7 | Balance shards | Funds must sit in the shard the bot trades from. Seen 11-09: Default $0.57, Combos $15.57. | Kalshi account | n/a | Bot "armed" but cannot afford one contract | NONE (**gap**) |
| 3.8 | Hourly report | Pushes a status report to the `jbeard1209/apex-reports` repo. | Mac launchd `com.apexcapital.kalshireport` | Mac launchd hourly | Stops whenever the Mac sleeps | NONE |
| 3.9 | Daily status / watchdog | One-page Kalshi status; the watchdog checks the jobs ran. | `kalshi_daily_status.py`, `kalshi_watchdog.py` | VM cron 12:20, 12:15 | — | itself |
| 3.10 | Polymarket survey | Research only. The mirror strategy does not transplant. | research scripts | manual | — | n/a |

## 4. Governance: board, CIT, logic team

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 4.1 | Board meeting | Votes on CIT proposals every weekday. Decisions get recorded. | `board_meeting.py` | VM cron 13:30 weekdays | **Vote crashed in every meeting ~08-18 → 11-09** (fixed PR #22) | job_failure_monitor (new 11-09), hourly |
| 4.2 | Deputy board | Handles `DEPUTY_CATEGORIES` proposals between full boards. | `deputy_board.py` | called by board_meeting? **No cron line.** | Deputy path never runs | NONE: verify (M-gov) |
| 4.3 | Board → agent execution | An approved proposal gets **implemented**. | `cit_implementation_agent.py` | **On neither host's schedule; last ran June 10** | Approvals pile up; the same finding gets re-approved (BBIO 47×; 1,177 of 1,561 approvals are re-approvals) | NONE. **Owned by the thread "Make board decisions actually get executed"** |
| 4.4 | CIT director | Finds code bugs and improvements, then queues board proposals. | `cit_director.py` + `cit_*` | VM cron 10:00, 16:00, 22:00, 01:00, Sun 06:00 | Queue grows forever; proposals routed to a pod with no consumer | wiring_audit; TestWiringContracts |
| 4.5 | Proposal routing | rd/ml go to the pod boards. fi/fe/etf go to the main board queue (`_effective_pod`). | `cit_director.submit_to_pod_queue()` | — | `*_pod_pending.json` grows with no reader | TestWiringContracts |
| 4.6 | Logic team | Finds logical or systemic incoherence. The implementation agent acts on every rule. | `logic_team.py`, `logic_team_implementation_agent.py` | VM cron 00:00, 12:00 (+15 min impl) | A rule with no handler | TestWiringContracts |
| 4.7 | CIT bug fixer (Claude routine) | Fixes the bugs CIT queues. | Claude scheduled task | Claude scheduler | Re-posts the same PIPELINE_ALERT every 10 minutes on 9 unfixable items | NONE (memory: cit_bug_fixer_alert_loop) |
| 4.8 | Strategy council | Bull, bear, and chief set board-ratified firm goals and grade them. | `strategy_council.py` | VM cron Mon 12:00 | Goals never graded | NONE |
| 4.9 | Sign-off ledger | Autonomy gate. Shadow mode, then ENFORCE for the board and CIT. | `signoff_ledger.py`, `signoff_promotion_gate.py`, `signoff_ledger_redteam.py` | VM cron 08:30 daily; Sun 08:00 | Gate stuck in SHADOW; budget counted per host | NONE |
| 4.10 | Board issue ledger | IRM → board → "Fixed issues". Closed loop, with anonymity. | `board_issue_ledger` | board | Issues "fixed" while the detector stays silent | internal_audit |
| 4.11 | Sweep governance | Weekly sweep of governance queues. | `sweep_governance.py` | VM cron Mon 11:40 | — | NONE |

## 5. Governance: chairman queue / inbox

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 5.1 | Chairman inbox | One list of decisions only Jack can make. Shown at every session start. | `chairman_inbox.py`, `chairman_queue.py`, `data/chairman_decision_queue.json` | session hook; agents append | Items pile up unseen (28 pending today) | SessionStart hook |
| 5.2 | Router | Only crucial items reach Jack. The rest go to the board fallback. | PR #25 | on append | Noise reaches Jack; or crucial items get routed away | NONE |
| 5.3 | Decision SLA | 50 days without a decision escalates to the board (except chairman-only carve-outs). | `chairman_decision_sla.py` | VM cron 12:30 | Items overdue with no escalation | itself |
| 5.4 | Compactor | Merges duplicates. | `chairman_queue_compactor.py --apply` | VM cron 13:05 | Duplicates (the [LOOP] items) | NONE |
| 5.5 | Queue sync / re-close | Mac and VM inbox states agree; re-close rows the sync bug reopened. | `chairman_queue_sync`, `reclose_stale_reopened.py` (PRs #28–30) | manual / sync | Closed items reappear | NONE |
| 5.6 | Autoresolve | Stale or already-fixed items are autoresolved. Surface only real decisions. | per memory feedback_autoresolve | sweeps | Jack shown stale items | NONE |

## 6. People: HR

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 6.1 | HR evaluation | Grades agents; PIP lifecycle. | `hr_agent.py` | VM cron 13:30 weekdays; Sun 22:00 | Agents demoted to a status the loader refuses (43 in one pass) | agent_roster ratchet |
| 6.2 | KEEP veto | One peer KEEP vetoes a termination. It converts to a PIP plus a 2-month re-review. | `hr_agent.py` | — | Termination despite a KEEP | NONE |
| 6.3 | Retirement audit | Observe mode on weekdays; full audit Sundays. | `hr_retirement_audit.py` | VM cron 21:00 weekdays `--observe`; Sun 22:30 | **Failing smoke test** (retirement review reader, per the "Verify cleanup" thread) | smoke test |
| 6.4 | Roster | 155 agents. Shrink-only manifest. | `agent_roster.py --check` | VM cron 11:25 | An agent vanishes | itself |
| 6.5 | Orphan agents | Every agent with consumers must be scheduled somewhere. | `wiring_audit.py` | pre-push gate | Wired but never runs (12 found in Aug) | wiring_audit ratchet |
| 6.6 | Agent sweep | Daily agent health sweep. | `agent_sweep.py` | VM cron 04:10 | — | NONE |
| 6.7 | Missed winners | Weekly review of names we passed on that ran. | `missed_winners_agent.py` | VM cron Sun 19:00 | — | NONE |

## 7. Risk and independent oversight

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 7.1 | IRM | Independent risk review. Emails the Chairman. Feeds the board issue ledger. | `irm_review_agent.py` | VM cron 21:00 weekdays | No email; tips never consumed | NONE |
| 7.2 | Internal audit | Third line of defence. Poison-pills every coherence law; a BLIND law goes to the Chairman. | `internal_audit_agent.py`, `CONTROLS.md` | **check cron**: not seen in VM crontab | Laws that cannot fail | test suite (new law without pill fails) |
| 7.3 | Coherence laws | Two independent measurements must agree. A HOLDS must carry `n_examined`. | `coherence_laws.py` | VM cron weekends 14:30; also in main.py? | Laws silently pass on 0 items | internal_audit |
| 7.4 | Problem lifecycle sentinel | Flags STALE, SILENT_DROP, and LOOPING problems. | `problem_lifecycle_sentinel.py` | VM cron :55 every 2 h | — | itself |
| 7.5 | Metric sanity sentinel | Catches impossible metrics. | `metric_sanity_sentinel.py` | VM cron every 4 h | — | itself |
| 7.6 | Data integrity sentinel | Fundamentals, risk, freshness, JSON health; self-heals. | `data_integrity_sentinel.py` | VM cron :20 every 4 h | — | itself |
| 7.7 | Freshness sentinel | Expected files rewritten on time. | `freshness_sentinel.py` | VM cron :15 hourly 07–21 | Fresh file, broken content (the board crash) | itself |
| 7.8 | Job failure monitor | Hourly log sweep for tracebacks and swallowed "non-fatal" errors. | `job_failure_monitor.py` | VM cron :40 hourly | — | **nothing watches it** |
| 7.9 | Canary | GREEN, ORANGE, or RED system health. | `canary_agent.py` | VM cron every 5 min in market hours, 30 min otherwise | Reads a cleared alert as live (`active` vs `level`) | — |
| 7.10 | Parameter audit / slow gate | Nightly parameter audit; slow model gate. | `parameter_audit.py`, `slow_gate.py` | VM cron 03:40; 03:30 | — | NONE |
| 7.11 | Short-interest monitor | — | `short_interest_monitor.py` | VM cron 12:35 weekdays | — | NONE |
| 7.12 | Deployed = repo | VM code matches the repo; the allowlist matches. | coherence laws `deployed_set_matches_repo`, `shipped_config_matches_repo` | coherence | **VIOLATED now: 5 files differ** ([LOOP] chairman item) | coherence |

## 8. ML / research

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 8.1 | Model metrics | One source of truth. "N/A" when untrained. Never a hard-coded AUC. | `model_metrics.py`, `data/model_meta.json` | readers | A number shown with no model behind it | metric_sanity |
| 8.2 | Test harness | Walk-forward, 20-day embargo, against a momentum baseline, plus Brier. Honest AUC ~0.58. | `model_test_harness.py` | VM cron Sun 07:00 | SUSPICIOUS_HIGH (leakage); report stale | NONE |
| 8.3 | Live test runner / overnight | Forward-validates the live model. | `ml_live_test_runner.py`, `overnight_ml_runner.py` | VM cron 06:00; 03:00 Tue–Sat | — | NONE |
| 8.4 | Long-term models | Label checker, validation, improvement agent, experiment runners. | `ml_longterm_*`, `ml_shortterm_experiment_runner.py`, `ml_improvement_loop.py` | VM cron weekly / daily | Experiments run, nothing gets adopted | NONE |
| 8.5 | Feature drift | Daily drift monitor. | `ml_feature_drift_monitor.py` | VM cron 12:00 weekdays | — | NONE |
| 8.6 | EDGAR scraper / transcripts | Backfills labels; earnings transcripts. | `predictive_model/edgar_historical_scraper.py`, `edgar_transcript_agent.py` | VM cron Mon 11:00; 23:20 | Label count flat | NONE |
| 8.7 | Institution tracker | 13F performance tracking (the original project). | `institution_performance_tracker.py` | VM cron Sun 07:40 | — | NONE |

## 9. Reports, email, ops

| # | Feature | Intended behaviour | Lives in | Trigger | Broken looks like | Alarm today |
|---|---|---|---|---|---|---|
| 9.1 | Morning briefing | Daily brief to Jack before the open. | `morning_briefing.py`, `chief_of_staff_agent.py` | VM cron 13:15 weekdays | No email | NONE |
| 9.2 | Email buffer | Batches non-urgent mail. Sends at 22:00. | `email_buffer.py` | VM cron 22:00 | Flood, or nothing | NONE |
| 9.3 | mail_guard | All mail goes through one guard (dedupe, rate limit). | `config.py` / `email_sender.py` | every send | Raw smtplib from Claude routines bypasses it (the July flood) | NONE for bypass |
| 9.4 | Dashboard | Local dashboard on port 5050. | `dashboard_server.py` | manual (Mac) | — | n/a |
| 9.5 | Backups | Daily data backup. | `backup_data.py` | VM cron 09:00 | No backups; restore untested | NONE |
| 9.6 | Disk janitor / log retention | Keep disk below the alarm level (79% today; was 89%). | `disk_janitor.py`, `log_retention.py` | VM cron 08:40, 19:40; 04:00 | Disk full → every job fails | canary? verify |
| 9.7 | Wakeup recovery | After a reboot or every 6 h, restart what is needed. | `wakeup_recovery.py` | VM `@reboot`, every 6 h | — | NONE |
| 9.8 | Crontab snapshot | Commit-able snapshot of the VM crontab. | `scripts/refresh_crontab_snapshot.sh` | VM cron 11:10 | Snapshot stale, so the audit compares against fiction | NONE |
| 9.9 | VM state mirror | The Mac pulls VM state for the dashboard. | Mac launchd `com.apexcapital.vmstate` | Mac launchd | — | NONE |
| 9.10 | Deploy | rsync to VM + stamp. Ships only tracked files. | `institutional-tracker/gcp_setup/deploy.sh` | manual (Jack-approved) | VM runs stale code (June-3 code for a month) | coherence `deployed_set_matches_repo` |
| 9.11 | Pre-push gate | Full suite twice (local zone + UTC) plus wiring audit. | `scripts/git-hooks/pre-push` | git push | Gate not installed on a fresh checkout | TestThePrePushGateIsTheOneWeShip |
| 9.12 | Podcast / UChicago plan | Personal deliverables. **Out of scope** for the audit. | ~/Desktop | n/a | n/a | n/a |

## 10. Dead crontab lines found while building this register

- `0 9 22 6 *`: a one-shot that rewrites `config/llm_config.json` to opus-4-8 and sends an
  email. It fires again **every June 22**. It also uses bare `python3`, which has no deps
  on the VM, so it will probably just fail. It should be removed (needs Jack's OK, since it
  is a prod crontab edit).
- `0 2 13-21 6 *  pregenerate_lessons.py`: a June-only batch. It is harmless, but it is dead.

## 11. Things that are built but scheduled nowhere (verify in M-gov / M-ops)

- `cit_implementation_agent.py`: confirmed unscheduled. Owned by the board-execution thread.
- `deputy_board.py`: no cron line. Confirm whether board_meeting.py calls it.
- `internal_audit_agent.py`: no cron line. Confirm whether something calls it.
- `chief_of_staff_agent.py`, `research_team.py`, `manager_agent.py`: expected to be called
  by main.py. Confirm.
