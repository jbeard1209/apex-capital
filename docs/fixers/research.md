# Research fixer (added 2026-11-13; not in the original seven)

Owns: `research_team.py`, `etf_research_agent.py`, `pod_macro_agent.py`, `etf_pod_board.py`,
`rd_pod_board.py`, `pod_health_monitor.py`, the zero-pick rules in `logic_team.py`/
`logic_officer.py` (read only; changes go via Wiring).

Live rows: 21. Problems: 16 June zero-picks duplicates (premise check: picks exist since,
then `superseded`), Macro/ETF pod 0 qualified picks and below its output OKR, the R&D
event-triggered cadence proposal (awaiting Chairman, leave it), HR zero-pick streak memos.

Know first: the anti-chasing law (extension is a disqualifier). Never loosen a pick gate to
make a zero-picks alarm go quiet; find why nothing qualifies and report it.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
