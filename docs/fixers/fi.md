# Fixed Income fixer

Owns: `fi_*.py` (board, cit_agent, credit_monitor, data_layer, executor, pipeline,
position_monitor, risk_monitor).

Live rows: 2, both `LT-015346-fi_never_run` from June. Premise is false (FI ran 2026-11-12
14:30Z). Expected action: close as `superseded` with the run log as the recorded check.

Know first: FI once emitted a verb ("BUY") that matched no branch and had no `else`, so 66
days of orders were approved and never executed. Every dispatch on a string needs an
explicit unknown-value branch that raises or escalates.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
