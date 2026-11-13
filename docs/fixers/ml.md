# ML fixer

Owns: `predictive_model/`, `ml_*.py`, `feature_engineer.py`, `model_metrics.py`,
`model_test_harness.py`.

Live rows: 0 directly. The 10 "ML cron stale" rows route to Ops (scheduling), and Ops hands
over only if the job itself crashes.

Never touches: pod capital (the idle $30k ML row is the Chairman queue).

Know first: never hardcode an AUC; `model_metrics.get_model_metrics()` is the only source.
Any model change must beat the prior `data/model_test_report.json` on embargoed walk-forward
AUC and edge over baseline. Commodity momentum and bridge features regressed; do not re-add.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
