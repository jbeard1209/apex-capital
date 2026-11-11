# Apex Capital — Full-System Audit (started 2026-11-11)

**Why this exists.** The board stopped voting around 2026-08-18. Nobody noticed for close to
three months. The vote crashed inside every meeting, but the meeting still exited 0 and
rewrote its report, so every monitor read it as healthy. This was not a one-off. Most of
the July–November failures share the same shape: **a feature silently stops doing its job,
and nothing is built to notice that it stopped.** This audit has two jobs. The first is to
check that every feature Jack asked for actually does what it was meant to. The second is
to make sure every feature *announces* when it stops.

**Chairman's mandate (2026-11-11, paraphrased).** Go line by line through the code, using
function tests, backtests, and forward/live tests. Go slowly, over about a month (target: first full pass by ~2026-12-11), at roughly
30% of the weekly token budget. Use agents where needed. Write a lot of large files so
nothing gets lost. Say so if there are better solutions.

**Shape agreed with Jack (2026-11-11).** The work is in two parts:
1. **One massive baseline audit.** These four documents are its plan, and the first
   modules get worked from them.
2. **A recurring audit every third day.** Each run reads [PROGRESS.md](PROGRESS.md), takes
   the next module, works its gates, writes findings, and updates PROGRESS.md before it
   finishes.

## Files

| File | What it is | Who updates it |
|---|---|---|
| [INTENT_REGISTER.md](INTENT_REGISTER.md) | Every feature Jack asked for or that was built. For each one: what it is *meant* to do, where it lives, what triggers it, and how we would know it is broken. | When a feature is added, changed, or found to differ from its intent |
| [AUDIT_PLAN.md](AUDIT_PLAN.md) | The codebase split into modules ordered by risk, money-moving code first. Each module has pass/fail **gates**, each gate has a **proving command**. | Rarely. Only when a module is split or added |
| [LIVENESS_GAPS.md](LIVENESS_GAPS.md) | Features with no automated alarm if they silently stop. This is the root-cause list. | When a gap is closed or a new one is found |
| [PROGRESS.md](PROGRESS.md) | The tracker. Module status, last run, the next module up, the findings log, and the token budget. | **Every recurring run, at the end** |
| `modules/<module>.md` (e.g. `modules/M01-order-path.md`) | One file per module. Each run appends a dated section with gate-by-gate evidence. | Each run |

## Rules every audit run follows

1. **Read-only on production.** No writes to the VM, no crontab edits, no deploys, no arm
   or disarm files, no broker orders. A fix goes on a branch and a PR. Deploying it needs
   Jack's go-ahead.
2. **Both hosts.** Green on the Mac is not green. Every gate that touches runtime behaviour
   runs on the VM too:
   `ssh apex-capital 'cd ~/institutional-tracker && ~/venv/bin/python3 …'`
   The VM is **not** a git checkout. Check freshness with `deploy_stamp.json` plus an
   `md5sum` diff.
3. **A pass needs a denominator.** "0 problems" must say how many items were examined.
   "Checked 0 of 0" is a FAIL (vacuous), not a PASS.
4. **Name the evidence.** Every PASS or FAIL in a findings file quotes the command and the
   relevant output lines.
5. **Unlazy gates.** Define the gates before looking. After the last change, rerun every
   gate. Never mark a module done while a gate fails. Mark it `BLOCKED` with the reason
   instead.
6. **Findings route somewhere.** Each finding gets one of three dispositions:
   (a) fixed in a PR, (b) filed as a CIT proposal or chairman item, or (c) recorded here as
   accepted risk with Jack's sign-off. "Noted" is not a disposition.
7. **Budget.** Aim for about 30% of the weekly token allowance across all runs in a week.
   One recurring run covers two modules (about 10 runs × 2 = all 21 modules in a month). Stop when the module is done,
   not when the context is full.
