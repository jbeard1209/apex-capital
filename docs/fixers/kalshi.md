# Kalshi fixer

Owns: `kalshi_*.py`, coherence law `nested_probabilities_are_monotone`.

Live rows: 5. Problems: the build-or-drop verdict rows left open after being superseded
(3 loop-sig + the investigator row), nested ladder pairs that are not monotone.

Never touches: the arm gate. Arming or disarming is Chairman-only.

Know first: the mirror edge is proven only in the 98-100c x 1-7 day slice; the fee cliff
kills everything else. A market with no book has no price; zero is not a price.

## Rules every fixer follows
- Branch `claude/fix-<area>-<slug>`, PR via `tools/ship.sh`. Never push to main, never deploy.
- A test that fails without the fix (revert the fix, watch it fail, restore). Suite in both zones, `wiring_audit.py` exit 0, `logic_team.py` clean.
- Never edit live `data/` without a timestamped backup and a record file.
- Close every queue row of the problem in one PR (rows are grouped by area + normalised title).
- Cannot prove it in this run: set the rows to `needs_human` with what you tried. Never leave them `claimed`.
- Chairman-only (capital, risk limits, Kalshi arm gate, terminations): refuse and route to the Chairman queue.
