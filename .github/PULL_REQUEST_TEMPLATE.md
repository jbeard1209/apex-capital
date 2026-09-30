<!--
Thanks for opening a PR against Apex Capital. Fill this in; a PR
without a filled template will be closed with a pointer back to
CONTRIBUTING.md.
-->

## What

<!-- One paragraph. What does this change do? -->

## Why

<!--
The incident, the postmortem, or the design pressure that forced
this change. If this is scratching a hypothetical itch, say so.
"Would be nice" is a valid answer; pretending there was an
incident when there wasn't is not.
-->

## Test plan

- [ ] `python3 test_pipeline_smoke.py` passes locally.
- [ ] Pre-push gate ran clean in both zones (`America/New_York`
      **and** `TZ=UTC`).
- [ ] If this PR adds a routed item (a new rule name, pod name,
      category, or dispatch key), a case was added to
      `test_pipeline_smoke.py::TestWiringContracts`.
- [ ] If this PR adds a coherence law, a paired **poison pill**
      was added (a law without a pill fails the internal-audit
      suite).

## Risk

<!-- Check any that apply. -->

- [ ] Touches a **protected file** (`alpaca_trader.py`, `main.py`,
      `risk_manager_agent.py`, `irm_review_agent.py`,
      `board_meeting.py`, `manager_agent.py`). If yes, link the
      Chairman decision row in `data/chairman_decision_queue.json`.
- [ ] Changes a wiring contract (adds / removes / renames a
      dispatch handler).
- [ ] Widens a guardrail (raises a limit, relaxes a check,
      shortens a cool-down). Risk-relaxing changes go through the
      sign-off ledger's asymmetric gate — do NOT merge without it.
- [ ] Would need a data migration on the VM as well as the Mac
      (there is no state sync — each host runs it independently).

## Rollout

- [ ] Paper-only (the default; no arm-gate touched).
- [ ] Requires a new arm-gate file (`data/.<name>_armed`). If yes,
      arming is Chairman-only and must be a separate decision.

## Related

<!-- Issue #, previous PR, postmortem file, memory index entry. -->
