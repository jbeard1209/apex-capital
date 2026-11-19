# M01 — Order path & choke point (pass 1)

Run 2026-11-16, host `MacBook-Air-173.local` (MAC), worktree `apex-audit-2026-11-16`, branch
`audit/M01-order-path-2026-11-16`. VM reachable (deploy_stamp sha 153958b, up 93 days; md5 of
alpaca_trader.py / order_ledger.py / risk_gate.py identical to the Mac tree).

## How the choke point works (what was actually read)
`config.py:227` calls `order_ledger.install()`, which replaces `TradingClient.submit_order` with
`_submit_order`. That wrapper runs, in order: `single_writer.check` (shadow by default) →
`risk_gate.check` (position-count cap + cash floor; exits never gated) → stamp `client_order_id`
→ real submit → reserve slot/cash → `pod_ledger.claim` → ledger record. Every guard fails OPEN on
its own error (by design, logged).

## Gates

| Gate | Result | n |
|---|---|---|
| F1 every `submit_order` site is behind the installer | **PASS** | 15 sites |
| F2 gate refuses cap / cash floor, never exits | **PASS** (with a scope finding, below) | 6 cases |
| F3 `submit_sell` closes shorts | **PASS** (existing `TestShortBookCanClose`) | in suite |
| F4 single_writer refuses entries on non-writer host | **PASS** | 5 cases |
| B1 replay 30 trading days of run logs through the gate | **BLOCKED** — run logs are not retained in the worktree; replaced by the live `risk_gate_log.json` read below | — |
| L1 broker orders last 30 days carry the VM prefix | **FAIL → finding 2** | 114 |
| L2 last 10 reconciliation reports | **PARTIAL** — only the latest report is retained (`reconciliation_report.json`), so "last 10" cannot be read; latest = WARNING, 1 HIGH | 1 of 10 |
| Full `test_pipeline_smoke.py` | see bottom | |

### F1 — command and output
AST walk of every non-test `*.py` for `.submit_order(` calls, requiring a `config` /
`alpaca_trader` / `manager_agent` import earlier in the same file (those import `config`, which
installs the patch). Pinned as `TestM01OrderPathAudit.test_every_submit_order_site_is_preceded_by_the_gate_installer`.
```
alpaca_trader.py ×6, board_meeting.py:1895, canary_agent.py:710, deputy_monitor.py:466,
fi_executor.py:430, head_agent.py:161, main.py:385/1510/1603, manager_agent.py:1138
15 sites; 0 without prior import
```
Dynamic cross-check: `import config; import fi_executor` → `TradingClient.submit_order.__name__ == "_submit_order"`.
Note: a bare `import main` (no function call) shows UNPATCHED because every site imports lazily;
the patch is in place by the time the call line runs. The static check is line-order, which is
weaker than path-domination; accepted for 15 straight-line sites.
No raw REST order path exists: the only `/v2/orders` uses are GET (`single_writer.py:542`,
`cit_pipeline_integrity_agent.py:713`). No `close_position`, `cancel_order` or `replace_order`
call exists outside tests.

### L1 — VM command and output
`ssh apex-capital` → `single_writer.fetch_broker_orders(limit=500)`, filter last 30 days, parse stamp:
```
n_30d 114 {'stamped': 114}
Counter({'apexcapitalc': 111, 'mac': 3})
2026-11-12T19:40:15Z ZION sell 84   mac manageragent
2026-11-12T19:40:15Z KEY  sell 211  mac manageragent
2026-11-12T19:40:15Z AMAT buy  11   mac manageragent   <-- an ENTRY from the non-writer host
```
All 114 are stamped (no foreign/unstamped order). 111 carry the VM tag, 3 carry `mac`. The AMAT
BUY is an entry placed by the Mac on 11-12, two days before commit 495f7e0 ("Mac exit guard:
non-writer host enforces stops only"). Nothing from the Mac since.

### Live gate behaviour (VM `data/risk_gate_log.json`, 4,502 lines)
Today the gate is actively refusing new names at the cap: `BLOCK_MAX_POSITIONS … 20 positions
open against a cap of 20; XLP would be a new one` (XLP, XLV, 13:54 UTC) and passing an exit
(`ALLOW_EXIT NEM sell 28`, 14:30 UTC). So the gate is live and consulted on the VM.

## Findings

1. **TRIM on a short sold more of the short (B).** `manager_agent.execute_decision` TRIM used
   `abs(qty)*0.40` but hard-coded `OrderSide.SELL`; `board_meeting` TRIM/REDUCE used
   `max(1, int(qty * pct))` on the SIGNED qty (a short of −100 at 50% → `max(1,−50)` = 1) and
   also hard-coded SELL. A SELL against a short is an entry that deepens the short, and the gate
   classifies it as non-reducing but allows it (no cash check on sells, no new slot). Same
   family as `short_book_could_not_close`. **Fixed**: `alpaca_trader.trim_order_plan(qty, pct)`
   returns (SELL, n) for a long and (BUY, n) for a short, capped at the position and floored at
   1; both sites use it. Tests: `test_trim_plan_buys_back_a_short_and_sells_a_long`,
   `test_both_trim_sites_use_the_signed_plan`. (No live short TRIM was observed in the last 30
   days of orders, so this was latent, not an incident.)
2. **A Mac ENTRY reached the shared account on 2026-11-12 (B).** `single_writer` is in shadow
   mode, so nothing blocked the Mac's AMAT buy. Mitigation already merged (#45, exit-only Mac
   guard); arming `single_writer` enforcement is the Chairman-only decision and stays open.
   Disposition: existing chairman item; no new item filed.
3. **The choke point does not enforce the 7% single-name cap or the CRO block (B).** The plan's
   F2 assumed it did. `risk_gate` enforces position COUNT and cash floor only. The 7% cap
   (`MAX_POSITION_SIZE_PCT`, regime-tightened) is applied only in `AlpacaTrader._size_position`
   → `submit_buy`; `head_agent` (no cap reference anywhere in the file) and the ETF-pod BUY in
   `main.py:1603` reach the broker without it; `fi_executor` applies its own target-size cap
   instead (fi_executor.py:13). The HIGH/CRITICAL buy block is caller-side
   (main.py / manager_agent; manager only hard-blocks CRITICAL+PRESERVE). Disposition: CIT
   proposal — "concentration cap at the choke point (`BLOCK_POSITION_PCT`) using the snapshot
   already fetched"; CRO block audited in M03. Not fixed here: a new refusal on a money path
   needs a design decision and a shadow period.
4. **Retention too short to audit (C).** Only the latest `reconciliation_report.json` is kept, so
   L2 ("last 10 reports") cannot be evaluated by anyone. Disposition: add a history file (same
   pattern as `pod_pnl_history`); CIT proposal.
5. **The Anthropic API returned "credit balance is too low" during the test suite (A).** Seen in
   `m01_final.log` (`claude-fable-5` and fallback `claude-opus-4-8`, HTTP 400). If this also
   holds for the production key, every gated reasoning agent is on its fallback or silent.
   Disposition: surfaced to Jack by push notification; outside M01.
6. **PROGRESS.md on origin/main contains unresolved merge-conflict markers (C).** A merge left
   `<<<<<<< HEAD … >>>>>>> origin/main` in the run log. Fixed in this PR (both rows kept).

## Pass-2 follow-ups
B1 needs run logs kept for 30 days (or the gate log joined to the broker order list; the gate
log already holds verdict per order). Add a liveness feature for `risk_gate_log.json` freshness
(gate consulted but no entry in N hours while orders exist) to the M00 registry.

## Full suite
`/usr/bin/python3 test_pipeline_smoke.py` → `Ran 1856 tests … FAILED (failures=2, errors=2, skipped=13)`.
All four were missing gitignored data in the fresh worktree (`data/hr_history.json`,
`data/historical_13f.db`). After copying them from the Mac checkout the three classes re-ran:
`Ran 33 tests … OK (skipped=1)`. New `TestM01OrderPathAudit` (5 tests) and `test_liveness` pass.
The `order_gate` heartbeat was added before the final run; no later code change.
