# Audit Plan — modules, gates, proving commands

The modules are ordered by **risk**. Code that can move money or block money comes first.
Next is code that decides things (governance). Code that only reports comes last.

Every module has the same three kinds of gate:
- **F**: function tests. These are unit or contract tests run locally. If a test doesn't
  exist yet, the run writes it.
- **B**: backtests or replays. Run the logic on historical inputs and compare the result
  with what should have happened.
- **L**: live/forward checks on the VM. These are **read-only**: logs, data files, broker
  read endpoints, crontab.

A gate **passes** only when its proving command was run *after the last change*, its
output is quoted in `modules/<module>.md`, and it examined a non-zero number of items.

Common prefixes used below:
```bash
# Mac (py3.9 with deps — NOT the shell's python3)
cd "/Users/jackbeard/Claude investment strategy/institutional-tracker"
PY=/usr/bin/python3
# VM (read-only)
VM='ssh apex-capital'; VMPY='cd ~/institutional-tracker && ~/venv/bin/python3'
```

Each module run always starts with **gate 0, freshness**: the VM must run the code
being audited.
```bash
$VM 'cat ~/institutional-tracker/deploy_stamp.json'; git -C .. rev-parse --short origin/main
for f in <module files>; do $VM "md5sum ~/institutional-tracker/$f"; md5 -q $f; done
```
If they differ, the finding is "VM stale for <files>", and the live gates are graded
against the VM's copy, not the repo's.

---

## Tier 1: money-moving (do first)

### M01: Order path & choke point
Files: `alpaca_trader.py`, `risk_gate.py`, `single_writer.py`, every call site of
`submit_order` / `submit_buy` / `submit_sell`.
- F1: every call site that reaches the broker goes through `risk_gate`, checked **per site**,
  not as a union. Proof: `grep -rn "submit_order\|api.submit\|/v2/orders" *.py pods/ predictive_model/`.
  Assert the set equals the gated set. Run `$PY test_pipeline_smoke.py -k choke`.
- F2: the gate refuses a buy that would exceed the 7% position cap, breach the cash floor,
  or run under a HIGH/CRITICAL CRO bulletin. Write a parametrised test with fake broker
  numbers.
- F3: `submit_sell` accepts a short cover (qty sign) and a full exit. This is a regression
  test for short_book_could_not_close.
- F4: single_writer refuses on the Mac. Simulate the host id.
- B1: replay the last 30 trading days of `logs/run_*.log` decisions through the gate.
  Count how many orders the gate *would* refuse today but the broker filled. The expected
  count is 0.
- L1: broker orders over the last 30 days. Every `client_order_id` must carry the VM
  prefix. Proof: VM read of the orders endpoint via the existing client, `--report` only.
- L2: `reconciliation_check.py` last 10 reports. Drift must be 0 or explained.

### M02: Positions, entry dates, max-hold
Files: `alpaca_trader.py` (entry_dates, max-hold), ledger, coherence `positions_conserve`.
- F1: a position closed and re-bought inside the 500-order window is dated from the
  **re-buy** fill. This is the open HIGH chairman item; write a failing test first.
- F2: a TRIM keeps the original entry date.
- B1: for every position held today, recompute the entry date from the broker fill history
  and diff it against the ledger.
- L1: `coherence_laws.py` `positions_conserve` must HOLD with n_examined = position count.
  It is VIOLATED today (1 ledger entry).

### M03: Manager, picks, CRO
Files: `manager_agent.py`, `research_team.py`, the CRO block in `main.py`, `risk_manager_agent.py`.
- F1: every pick verdict (APPROVE, REJECT, DEFER, any unknown verb) hits a branch. An
  unknown verb raises; it is never silently skipped.
- F2: a cleared CRO alert (`active=false`) does not block buys, whatever its `level`.
- B1: over the last 30 days, every APPROVED pick produced an order or a logged reason. The
  count of approved-and-silent must be 0.
- L1: on the VM, the days in the last 30 on which buys were blocked, with the reason. Flag
  any block lasting more than 3 consecutive days.

### M04: Kalshi copy-trader + manager + scout
Files: `kalshi_copytrade.py`, `kalshi_copytrade_manager.py`, `kalshi_copytrade_scout.py`,
`kalshi_client.py`, the arm and halt files.
- F1: caps of $1 per trade, $3 per day, and the 15–85¢ band are each enforced, and each has
  a test at the boundary (85¢ in, 86¢ out).
- F2: the halt file stops trading within one cycle; the −$6 breaker writes it.
- F3: no trade when unarmed. The arm file is only ever written by a human.
- B1: replay the scout's chosen traders over the last 30 days of their fills. Would
  copying them have been positive after fees?
- L1: VM logs: in the last 24 h, the number of cycles run (the expected count is about
  1,440), the orders placed, and the orders refused, with reasons. The balance must be in
  the shard the bot trades from.
- L2: is the halt file present, and if so why and since when?

### M05: Kalshi scanner, calibration, mirror
Files: `kalshi_scanner.py`, `kalshi_calibration.py`, `kalshi_mirror_paper.py`, `kalshi_watchdog.py`.
- F1: a book-less market is excluded, never priced at 0.
- F2: the mirror only acts in the 98–100c × 1–7d slice.
- B1: recompute the calibration table from the raw resolved set and diff it against the
  report.
- L1: resolved n grows week over week.

### M06: Pods: budgets & boards
Files: pod budget code, `rd_pod_board.py`, `ml_pod_board.py`, `etf_pod_board.py`,
`fi_pipeline.py`/fi_executor, `fe_pipeline.py`, `tech_short_pod.py`.
- F1: the budget formula. Cash minus short liability minus the 20% floor, then a 25% floor
  and 60% cap per pod. Test the edge cases: no cash, short-heavy.
- F2: every pod's order verb is in the executor's branch set.
- B1: per pod, the last 30 days of approved ideas against the orders placed.
- L1: for each pod, the last date it placed an order. If a pod has gone more than 10
  trading days without one, "idle" must be a *recorded decision*, not an accident.

## Tier 2: governance (decides what gets done)

### M07: Board meeting & deputy board
Files: `board_meeting.py`, `deputy_board.py`, `board_issue_ledger`.
- F1: a vote on a proposal with each confidence type (int, "HIGH", None, NaN) does not
  raise.
- F2: every proposal category maps to one of the deputy_board sets.
- L1: board log for each of the last 10 meetings, giving proposals seen, votes cast, and
  decisions written. Both counts must be above 0.
- L2: is `deputy_board.py` actually invoked? Check by log evidence, not by grep.

### M08: Board → execution (CIT implementation)
**Owned by the thread "Make board decisions actually get executed".** This audit only
re-verifies that thread's fix after it lands.
- L1: approved proposals in the last 14 days, against implemented ones, against those
  explained as not-implementable.
- L2: re-approvals of an already-approved finding must be 0.

### M09: CIT director & proposal routing
Files: `cit_director.py`, `cit_*`, `*_pod_pending.json`.
- F1: `TestWiringContracts` passes; every pod name has a consumer.
- L1: queue sizes over time. A queue that only grows is a finding.

### M10: Logic team & implementation agent
- F1: every `rule=` has a handler (existing contract test).
- L1: last 10 runs, giving findings emitted against findings acted on.

### M11: Chairman queue / inbox / SLA / compactor / sync
Files: `chairman_inbox.py`, `chairman_queue.py`, `chairman_decision_sla.py`,
`chairman_queue_compactor.py`, `reclose_stale_reopened.py`, router (PR #25).
- F1: router. A crucial item reaches Jack; a non-crucial one goes to the board fallback.
- F2: compactor. The [LOOP] duplicates collapse.
- L1: Mac inbox against VM inbox. Item ids and states must agree.
- L2: count of overdue items without an escalation record must be 0.

### M12: Sign-off ledger / risk-limit governor / strategy council
- F1: loosening a limit without sign-off is refused; tightening passes.
- L1: current SHADOW/ENFORCE state, and how long it has been in that state.

## Tier 3: people & oversight

### M13: HR (evaluation, PIP, KEEP veto, retirement, roster)
- F1: KEEP veto. One KEEP converts a termination into a PIP.
- F2: no status that HR sets is refused by the research_team loader.
- F3: the retirement-audit reader smoke test (currently **failing**).
- L1: `agent_roster.py --check` on the VM shows 155 agents with no shrink.

### M14: Monitors (canary, sentinels, job_failure_monitor, coherence, internal audit)
- F1: each monitor, given a planted failure (poison pill), reports it.
- F2: each monitor, when it fails itself, is noticed by something else
  (see LIVENESS_GAPS).
- L1: each monitor's last successful run on the VM, against its cron cadence.

### M15: IRM
- L1: last 10 IRM emails sent; tips consumed by the CRO and the board ledger.

## Tier 4: ML / research

### M16: Model metrics & harness
- F1: `get_model_metrics()` returns None/N/A when untrained.
- B1: rerun `model_test_harness.py` and diff it against `data/model_test_report.json`.
  The edge over the momentum baseline must be recorded honestly.
- L1: live-runner forward AUC against harness AUC.

### M17: ML long-term / experiments / drift / EDGAR backfill
- L1: labelled-sample count trend; experiments adopted against experiments run.

## Tier 5: reports, email, ops

### M18: Email (mail_guard, buffer, briefing, all senders)
- F1: `grep -rn "smtplib" --include=*.py` finds **only** the guarded sender.
- L1: last 7 days of sent mail count by sender; no duplicate floods.

### M19: Ops (deploy, backup, disk, log retention, wakeup, crontab snapshot, Mac launchd)
- F1: `deploy.sh` ships only tracked files, writes the stamp, and excludes runtime data.
- L1: the latest backup exists and a restore to a scratch dir works (read-only to prod).
- L2: crontab snapshot in the repo against the live `crontab -l`.
- L3: dead cron lines (the June 22 model switch, the June pregenerate). Remove them with
  Jack's OK.

### M20: Dashboard & reports
- L1: dashboard numbers against their source files (no hard-coded metrics).

---

## Line-by-line method (per module)

1. List every function in the module's files (`grep -n "^def \|^    def "`).
2. For each function: what does it promise (docstring or name), who calls it, and what
   does it do on bad input (None, NaN, empty, wrong type)? Mark each `try/except` that
   swallows an error, and each `.get(x, default)` whose default means "act".
3. For each swallowed error, ask whether a monitor would see it. If not, it is a
   liveness gap.
4. Write the missing tests, run the gates, record the results.
