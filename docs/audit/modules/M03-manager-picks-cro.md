# M03 — Manager, picks, CRO (pass 1)

Run 2026-11-25, host `Mac.lan` (MAC), worktree `/Users/jackbeard/apex-audit-2026-11-25-b`, branch
`audit/M03-manager-picks-cro-2026-11-25`, base `581479c`. VM reachable (deploy_stamp sha `5c0ce93`,
2026-11-24T19:37Z; md5 of main.py, manager_agent.py, research_team.py, risk_manager_agent.py,
alpaca_trader.py, trade_review_team.py, liveness.py, config/liveness.json and risk_gate.py identical
to `origin/main`, checked 2026-11-25 20:05 UTC). Status: **BLOCKED** (F1 fails as written, B1 has 6 of
30 days of logs).

## In plain English

- **Nothing new has been bought since the board pause kept being re-set.** Every emergency board
  session votes to pause new buys, even with a risk score of 0 and no concerns. Buy refusals from the
  pause appear on 11-20, 11-23, 11-24 and 11-25. Changing that loosens a safety control, so it is
  **Jack's decision**; nothing here changes it.
- **The 13F strategy has never bought anything.** The order is built without a price and is refused.
  The fix turns on a strategy that has never run, so it is a separate draft PR.
- **A 15% position-limit trim was approved and then never sent.** Fixed.
- **The pacing limit on new names did not apply to the scheduled run** (30 buys approved in one run on
  11-24). Fixed, with a large practical effect: see finding 2.
- A related incident (28 stacked SH hedge buys, 81% of the account) is fixed separately in PR #88.

## What was read
`manager_agent.py` (verdict, sizing, execution, `run_manager`), `research_team.py`, `main.py` steps
3–4c (13F path, risk bulletin, manager loop), `risk_manager_agent.py`, `trade_review_team.py`,
`canary_agent.py` (alert loader, defensive hedge), `board_meeting.py` (emergency positions,
consensus, actuator), `risk_gate.py` (pause), `liveness.py`.

## Gates

| Gate | Result | n |
|---|---|---|
| F1 every verdict hits a branch; an unknown verb raises | **FAIL as written** (intent met: refused loudly, never silent; it does not raise) | 21 |
| F2 a cleared alert does not block buys | **PARTIAL** — canary alert and board pause pass; the CRO bulletin has no `active` field to clear | 7 |
| B1 30 days: every approved pick has an order or a logged reason | **BLOCKED** — logs go back to 11-20 only; 0 silent in what exists | 61 (+28 canary) |
| L1 days buys were blocked, with reason; flag > 3 consecutive | **PASS** (measured), **flag raised** | 500 log entries |
| Full `test_pipeline_smoke.py` + `test_liveness.py` | **PASS** | 2038 + 24 |

### F1 — command and output
`evaluate_recommendation` for every known verb, every alias, `DEFER`, `APPROVE`, `REJECT`, `WAT`,
empty and missing; `execute_decision(..., dry_run=True)` for three raw verbs.
```
F1 eval 'BUY'          -> APPROVED  BUY_NEW  rationale=2
F1 eval 'EXIT'         -> APPROVED  SELL     rationale=2
F1 eval 'DEFER'        -> REJECTED  HOLD     rationale=1
F1 eval 'APPROVE'      -> REJECTED  HOLD     rationale=1
F1 eval 'REJECT'       -> REJECTED  HOLD     rationale=1
F1 eval 'WAT'          -> REJECTED  HOLD     rationale=1
F1 eval ''             -> APPROVED  BUY_NEW  rationale=1
F1 eval None           -> APPROVED  BUY_NEW  rationale=1
F1 exec 'WAT'    executed=False error="unhandled action 'WAT' — execute_decision has no branch for "
F1 n = 21  verdicts with no rationale: []
```
An empty or missing action becomes `BUY_NEW` on purpose (`manager_agent.py:258`, "research picks are
buy signals"). VM read: 0 of 2,405 logged decisions lacked an action. Left as is (finding 11).

### F2 — command and output
```
F2 canary active=False level=ORANGE -> loader None verdict APPROVED
F2 canary active=False level=RED -> loader None verdict APPROVED
F2 canary active=True level=ORANGE -> loader ORANGE verdict REJECTED
F2 canary active=True level=RED -> loader RED verdict REJECTED
F2 pause active=False expires=2099 -> open
F2 pause active=True expires=2000 -> open
F2 pause active=True expires=2099 -> BLOCKS
F2 n = 7  cleared records that still block: []
```

### B1 — VM command and output (read-only log counts)
```
B1 20261120 approved [0, 0, 1, 0] buyfail 0 size_fail 0  BLOCK_BOARD_PAUSE 6  13F_rej 136 13F_exec 0
B1 20261123 approved [0, 0, 0, 0] buyfail 0 size_fail 0  BLOCK_BOARD_PAUSE 7  13F_rej 136 13F_exec 0
B1 20261124 approved [0, 0, 30, 30] buyfail 58 size_fail 2 BLOCK_BOARD_PAUSE 68 13F_rej 92 13F_exec 0 podledger 44
B1 20261125 approved [0, 0, 0] buyfail 0 size_fail 0     BLOCK_BOARD_PAUSE 8  13F_rej 102 13F_exec 0
```
(`approved` = each "Manager approved N" line.) Manager path: 61 approved = 1 executed sell + 58 "Buy
order failed" (board pause) + 2 "Cannot size position [BRK-B]"; silent 0. Canary path,
`logs/canary_2026112[0-5].log`: 28 trade lines = 9 "✓ Executed" + 19 "MSFT ✗ Failed: submit_sell
returned None"; silent 0 (the reason in those 19 is wrong, finding 7). 13F approvals were logged as
failures with a misleading reason (findings 4 and 5). 30-day window available from about 12-20.

### L1 — VM command and output (`data/risk_gate_log.json`, `data/buy_pause.json`)
```
L1 gate_log n 500 block days 19     (since 10-26)
10-27 MAX_POSITIONS 24 | 10-28 MAX 3, CASH 1 | 10-29 CASH 4 | 10-30 CASH 1
11-02 MAX 32, CASH 3 | 11-03 CASH 4 | 11-04 CASH 3, MAX 3 | 11-05 MAX 2
11-11 MAX 7 | 11-12 MAX 13 | 11-13 MAX 3 | 11-16 MAX 7 | 11-17 MAX 3 | 11-18 MAX 24 | 11-19 MAX 3
11-20 BOARD_PAUSE 7 | 11-23 BOARD_PAUSE 7 | 11-24 BOARD_PAUSE 68 | 11-25 BOARD_PAUSE 9
PAUSE active=true set_at 2026-11-25T15:25Z expires 2026-11-26T15:25Z source emergency_board
```
**Flag:** board pause on 4 consecutive trading days (11-20 → 11-25), and it was re-set every 2–2.5
hours from 11-21 21:00Z to 11-25. Cash-floor / position-count refusals also ran 8 (10-27 → 11-05) and
7 (11-11 → 11-19) consecutive trading days; the log records refused orders only, so it does not show
whether any buy got through on those days.

Pause vote, reproduced (dry run, briefing with `risk_score` 0 and no concerns): all four
`_emergency_position_*` functions (`board_meeting.py:3404–3600`) propose `PAUSE_NEW_BUYS` STRONG;
`_synthesise_consensus` returns
```
{'id': 'PAUSE_NEW_BUYS', 'proceed': True, 'executor': 'Engineering', 'urgency': 'IMMEDIATE', 'classification': 'UNANIMOUS'}
```
Callers of the emergency session: `cit_director.py:4794`, `canary_agent.py:1155`,
`chief_of_staff_agent.py:3417`, `deputy_monitor.py:318`, `reconciliation_check.py`,
`risk_manager_agent.py`.

## Findings

| # | Finding | Sev | Disposition |
|---|---|---|---|
| 1 | The 15% hard-ceiling trim was labelled MODIFIED and never executed; a trim could also stack on a waiting order | A | **Fixed**. VM 11-25: no position above 15% (largest ORCL short, 7.83%). Still open: an ADD at or over the regime cap is REJECTED with exec TRIM and does not execute (Jack) |
| 2 | `main.py` step 4c had no new-position pacing (30 approvals in one run, 11-24) | A | **Fixed** (`NewPositionPacer`: 2 per cycle, 1 per pod, counts executions only). **Effect:** every production `source_agent` on the VM maps to `pod1_rd`, so the scheduled run now opens at most **one** new name per run (up to 4 a day). This applies the 2026-05-06 board mandate; Jack should confirm that is wanted |
| 3 | `cro_buy_block` liveness read only the CRO level and was never beaten from `main.py`, so a board pause looked healthy | A | **Fixed** (G07): the beat is withheld while any firm-wide block is active; alerts after 72 weekday hours. Cash-floor / position-count refusals are not covered |
| 4 | 13F buys can never execute: `main.py:843` builds the order with no `current_price`; pod ledger refuses "price must be > 0". 0 13F rows in the VM buy log (126 rows) | A | Separate **draft** PR: it turns on a never-run strategy. Jack decides |
| 5 | 13F log said "REJECTED: APPROVED:" with no reason when approved but not executed | C | **Fixed** |
| 6 | Win/loss streak tracker was dead (read keys that are never returned); VM streaks 0/0 despite 154 sell rows | B | **Fixed**. Turns on never-run behaviour: loss streak ≥3 / ≥5 tightens entry, win streak ≥5 loosens it by 3 points |
| 7 | The order layer's refusal reason was lost, so guard blocks were graded as broker failures (canary: "30 of 31 failed at the broker" was the board pause) | B | **Fixed** (`trader.last_refusal`). The unmerged M02 branch also edits `alpaca_trader.py` |
| 8 | Historical-context check judged the raw verb, not the executed one | C | **Fixed** |
| 9 | Research picks earned an RSI point with no data; one malformed pick lost the whole cycle | B | **Fixed** |
| 10 | S&P benchmark was always 0 on the VM, so every trade "beat the market" (11-20 report: 153 trades, SPY total 0.0, delta 68.04; measurable windows give SPY +6.95 over 44, delta 61.09) | B | **Fixed** (unmeasured is None). The sum-of-percentages metric itself is unchanged. `portfolio_risk_dashboard.py` has the same pattern and fails on the VM (`config.ML_SHORTTERM_CAPITAL` missing); not in cron |
| 11 | An unknown verb is refused, not raised; an empty action is treated as a buy by design | C | Accept-or-change: **Jack** (F1) |
| 12 | The CRO bulletin has no `active` field and no staleness check; HIGH only raises thresholds and only CRITICAL + "PRESERVE" blocks, which contradicts intent 1.5 ("HIGH or CRITICAL blocks all buys") | B | **Jack**: change the code or the intent |
| 13 | Every emergency board session pauses all buys for 24 h regardless of the input; repeated sessions keep it on | A | **Jack**. Not changed: it loosens a safety control |
| 14 | Canary RED re-buys the SH hedge whenever it is not held, while defensive mode sells it; weekend RED repeated a stale "S&P futures -3.64%, VIX 35.9"; an "urgent email" is logged as sent after being diverted to the digest | B | Open. PR #88 stops the stacking; the trigger is unchanged (M14) |
| 15 | Fail-open reads: high-water mark (`except: pass`), CVaR breaker, previous-bulletin read; `REGIME_MANDATES` has no LOW / MODERATE keys, so those levels fall to UNKNOWN; step 4c is one `try`, so one exception means zero trades; buying-power thresholds are nearly vacuous under margin | B | Open, code reading only; follow up in pass 2 |
| 16 | `_resolve_pod_id` (`manager_agent.py:1010`) matches "ml" inside "teamleader", sending `RDTeamLeader` to the ML pod; all VM energy / CoS / FE sources map to `pod1_rd` | C | Latent (0 occurrences on the VM). M06 |

## Tests
New: 33 tests in `test_pipeline_smoke.py` (`TestM03…`) and two patches in `test_liveness.py`. The same
33 against `origin/main` source: `Ran 33 tests in 4.268s FAILED (failures=11, errors=17)`.

Full suite after the last code change (2026-11-25):
```
test_pipeline_smoke.py: Ran 2038 tests in 222.852s  OK (skipped=10)
test_liveness.py:       Ran 24 tests in 4.628s  OK
wiring_audit.py:        exit 0
```

## Deploy
Not deployed. After merge it needs Jack's go-ahead (`tools/ship.sh --deploy-only`). Deploy PR #88
first: it is the urgent one.
