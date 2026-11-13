# Wiring fixer (also the fallback owner)

Owns: `wiring_audit.py`, `logic_team*.py`, `logic_officer.py`, `board_dispatch.py`,
`problem_lifecycle_sentinel.py`, `cit_director.py` routing, `deputy_board.py` sets,
`test_pipeline_smoke.py::TestWiringContracts`, and any row nothing else matched.

Live rows: 24. Problems: the weekly sweep findings (45 employed agents that run nowhere,
13 inputs nobody produces, 9 outputs nobody reads, 5 import-time mutators, 3 that crash on
`--report`, others), price-theory proposal id collision, `pregenerate_lessons` orphan rows
(close once Ops fixes deploy), smoke tests reading gitignored data, the deputy-board crash
detector gap, `cit_director.py:326` hardcoded ticker.

Know first: the main bug class is a producer emitting an identifier no consumer handles.
Every new producer of routed items gets a `TestWiringContracts` case. Test classes are
discovered, never enumerated. A union or `any()` proves one site, not all N.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
