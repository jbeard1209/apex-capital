# First Eagle fixer

Owns: `fe_*.py`, `fe_watchlist.json` producer, `data/company_research/`, the
`fund-sanity-*` quarantine path (`fundamentals_sanity`, `fe_intrinsic_value.calculate_iv`).

Live rows: 12. Problems: `fe_watchlist.json` missing, `data/company_research/` missing,
CIT-2026-037 (`fe_board.py:24` silent `except: pass`) is already fixed on main: it now logs
the exception. Close as `superseded`.
WING quarantine needs_human, 4 standing quarantines (BBIO/FICO/MSCI/TDG, handled by
`board_dispatch`; leave them).

Know first: the FE standard is deep value (EPV-anchored, debt-verified, 25-50% margin of
safety). Never loosen it to produce picks. NaN passes every `is not None` guard; bad data
must quarantine, not become a silent zero.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
