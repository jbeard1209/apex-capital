# Risk fixer

Owns: `main.py` (CRO section), `alpaca_trader.py`, `single_writer.py`, `signoff_ledger.py`,
`signoff_promotion_gate.py`, `position_risk_monitor.py`, `position_aging_agent.py`,
`loss_attribution.py`, `portfolio_attribution_agent.py`, coherence laws `positions_conserve`
and `recorded_exits_are_executed`.

Live rows (2026-11-13): 9. Problems: stale loss attribution (4 `LO-*-unexplained_loss` rows +
1 loop-sig, one fix), `positions_conserve` 1 broker position with no ledger entry,
`recorded_exits_are_executed` 1 of 138 exits never executed, designated writer silent (2).

Never touches: risk limits, position caps, capital. Those are Chairman-only.

Know first: the broker is the only shared truth between Mac and VM (no state sync); host
identity is persisted, not derived; every order goes through the preflight choke point,
never a new direct broker call; a degraded read returns None and fails closed (999/350 read
as "go"). Entry dates are host-local; the broker wins.

Guard tests: `TestWiringContracts`, the signoff ledger and choke-point classes in
`test_pipeline_smoke.py`; `coherence_laws.py` poison pills via `internal_audit_agent.py`.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
