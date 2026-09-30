# Postmortems — a curated index

Apex Capital is instrumented well *because* almost everything
here has broken at least once. This is a curated index of the
incidents that mattered most — the ones whose guards are still
load-bearing.

Each entry lists the incident, the root cause, and the guard now
in place. Search the repo for the guard name to see how it is
wired.

---

## VM ran stale code for a month

**What happened.** The Mac dev host and the GCP VM
(`apex-capital-495523`) both call `main.py`. The Mac was
`git pull`'d daily; the VM was assumed to be updated by the same
process because "the deploy script runs there." The deploy script
did not run there — the VM had no cron for it — and for ~30 days
the VM was running June-3 code while the Mac ran June-30 code.
Consequences: phantom email alerts (an alert type deleted in the
Mac's tree still ran on the VM), and *divergent trades from two
brains against the same paper account*.

**Root cause.** The VM was `rsync --exclude=.git/`'d during initial
setup, and `deploy.sh` ships the same shape. The VM is not a git
checkout — `git log` on the VM fails with "not a git repository."
Every earlier version of the CLAUDE.md freshness check said "run
`git log -1` on the VM" and had silently errored out for the entire
time it was documented.

**Guard.** `deploy_stamp.json` written by `deploy.sh`; freshness
audit uses `md5sum` diff over the actual files (checksum ground
truth, not a self-reported timestamp). The VM's Python is
`~/venv/bin/python3`, not bare `python3` — bare Python has no
`dotenv` and every test ImportErrors. This is captured in the
memory index entry `vm_is_not_a_git_checkout`.

---

## Green on the Mac was not green

**What happened.** A push that passed the pre-push gate on the
Mac shipped to the VM and immediately failed the pipeline. The
tests were passing in the Mac's local zone
(`America/New_York`); on the VM (`TZ=UTC`) they failed on
timezone-boundary arithmetic (a `date.today()` call that returns
different dates in the two zones near midnight).

**Root cause.** The gate ran the suite once, in the Mac's local
zone. A wide class of bugs — clock arithmetic, `date.today()`
under UTC vs ET, weekday-of arithmetic across DST boundaries —
were invisible to it.

**Guard.** The pre-push gate now runs the suite **twice** — once
in `America/New_York`, once with `TZ=UTC`. It also runs longer
than an idle SSH connection to GitHub survives, so
`core.sshCommand` is set repo-locally to
`ServerAliveInterval=20 ServerAliveCountMax=30`. Both are in
`CLAUDE.md`. The class of bug is captured in
`host_divergence_is_a_bug_class`.

---

## Two brains, one account

**What happened.** For ~7 days, both the Mac and the VM
independently submitted paper orders. The Mac's alarm for "VM is
down" stayed silent because the VM was not down — it was up and
unreachable to the Mac. Each host read its own local
`state.json`, believed itself to be the only writer, and traded
accordingly.

**Root cause.** The single-writer check counted heartbeats
written by the host itself. A host that cannot see the other
sees only its own heartbeat and concludes "I am the only writer."
The check begged the question.

**Guard.** `single_writer_detector` now counts hosts from the
**broker's own order log** (Alpaca returns a `client_order_id`
suffix that encodes the host). Only the broker can count hosts.
Host identity is persisted, not derived from environment
variables — a host that renames itself does not become a new
host. See `two_brains_one_account`.

---

## The silent identifier mismatch

**What happened.** June 2026, three bugs in the same month:
`logic_team.py` emitted a rule name that
`logic_team_implementation_agent.py::RESOLUTION_HANDLERS` did not
handle; `cit_director.submit_to_pod_queue()` routed to `fi`,
`fe`, and `etf` pod boards that had **no consumer**; a proposal
category was emitted that was not in any of `deputy_board.py`'s
category sets. In each case output was produced, dispatched, and
never acted on. `.get()` and `try/except` swallowed the mismatch.

**Root cause.** Wiring was validated *at authoring time* (a
comment in one file naming the consumer in another) rather than
*at test time*. Comments rot; test failures do not.

**Guard.** `test_pipeline_smoke.py::TestWiringContracts` — a test
class that iterates every producer's emitted identifier set and
asserts each item is in the consumer's handler table. Any new
routed item requires a new contract case, or the pre-push gate
blocks the push. `wiring_audit.py` runs the same sweep weekly.
See `wiring_contract_bugs`.

---

## NaN is not None

**What happened.** A guard read like `if x is not None and x >
threshold`. A data feed occasionally emitted `NaN`. `NaN is not
None` returns `True`; `NaN > threshold` returns `False`. So bad
data silently became "condition not met" — the guard reported
GREEN on inputs the alarm was designed to catch.

**Root cause.** Python treats `NaN` as a valid float, and
`math.nan is not None` is `True`. `x != x` is the only test that
distinguishes `NaN` from a value; every other comparison lies.

**Guard.** A three-part fix. First, `math.isnan(x)` (or
`pd.isna(x)`) explicitly in every threshold read. Second, a
`sanity_gate` layer at the ingestion boundary rejects `NaN` from
feeds before any consumer sees it. Third, a poison-pill test that
injects `NaN` into each guarded input and asserts the guard fires.
See `nan-is-not-none`.

---

## The fix that erased its detector

**What happened.** A remediation was drafted for a coherence
finding, and the drafter (correctly) removed the field that the
finding referenced. The field was also read by the detector for
that finding. After the fix, the detector could not distinguish
"this instance is fixed" from "this field no longer exists" —
every future instance passed.

**Root cause.** The remediation changed the schema. The detector's
field-reference was compile-time coupled to the old schema. The
test suite was not.

**Guard.** Detector refs are now **AST-resolved** against the
control they audit, not string-matched. A remediation that
removes a field the detector reads breaks the AST resolution and
fails the audit suite; the drafter has to name the new field
explicitly. Additionally, every retired detector must post a
`retirement_reason` before the audit accepts it as gone. See
`fix_that_erased_its_detector`.

---

## A union cannot prove all N

**What happened.** A guard test looked like
`assert any(guard_fired(site) for site in sites)`. The test
passed on the day it was written because *some* site fired. When
a code change quietly reduced the number of sites that fired
from all 15 to 1, the test still passed — `any()` was satisfied.
Two real guard-test bypasses shipped this way.

**Root cause.** `any()` is a union. A test that asserts a union
proves *one* case, not *all* cases.

**Guard.** Every guard test now asserts **per-site**, not `any`:
`for site in sites: assert guard_fired(site)`. Reviewers watch
for `any()` in guard tests and reject them. Additionally, the
suite asserts *domination* (this guard fires on every case) rather
than line order (this guard's line appears before that one). See
`a-union-cannot-prove-all-n`.

---

## A verdict with no consequence

**What happened.** A control returned `UNKNOWN` when a required
signal was absent. `UNKNOWN` had no downstream reader — nothing
opened a ticket, nothing paged, nothing timed out. So absent
signals turned into a permanent open state. Three real bugs sat
in `UNKNOWN` for weeks.

**Root cause.** `UNKNOWN` was a verdict shape but not a **status
transition**. "Cannot tell yet" is only useful if it eventually
becomes "must tell" or "downgraded to noise" — and only if
someone owns the transition.

**Guard.** Every deferring verdict now names (a) an owner and
(b) an expiry. If the owner does not resolve the deferral by the
expiry, the item auto-escalates. This is the same 15-day fallback
pattern the Chairman queue uses, generalized to any status field.
See `verdict-with-no-consequence`.

---

## Untracked code reached production

**What happened.** `deploy.sh` used `rsync -a --exclude=.git`.
Any file created on the Mac and not committed was still rsync'd
to the VM. For ~2 months, 362 uncommitted files — experimental
scripts, one-off debug helpers, a WIP agent — ran on the VM
alongside the production pipeline. Some of them wrote to shared
queues.

**Root cause.** Deploy was defined as "files on disk", not
"files in git." The two diverged silently.

**Guard.** `deploy.sh` now checks `git status --porcelain` before
rsync and refuses to deploy if the tree is dirty. A one-off
override exists (`--force-untracked`) but is announced in the
deploy log and requires the Chairman-only flag. See
`untracked-code-reached-production`.

---

For the full index of postmortems as memory entries, see the
project's memory index. The above are the load-bearing ones.
