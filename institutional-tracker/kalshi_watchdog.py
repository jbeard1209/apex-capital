#!/usr/bin/env python3
"""
APEX CAPITAL -- KALSHI WATCHDOG
================================
Supervises the Kalshi measurement campaign between now and the day the verdict
gate can open. It does not scan, price, or bet; it answers one question every
day — **is the evidence still being collected, and can the verdict still be
reached?**

Why this exists
---------------
On 2026-08-19 the Chairman asked why the Kalshi plan "wasn't running". It was:
the VM crontab has scanned 4x/day and resolved daily without a miss. What had
stopped was the *supervision* — that was a session-scoped `/loop`, and it died
with the session that created it. The work was durable and the watching was not.

That is the dangerous half to lose. `kalshi_calibration.is_edge_proven()` is
deliberately patient: it answers "NOT YET" until the sample can carry a verdict.
A patient detector and a dead collector produce the identical output — "NOT YET",
forever — so silence cannot distinguish "still gathering" from "stopped
gathering three weeks ago". Compare `reader-outlived-its-producer`, where a job
graded itself against a log it never wrote, and `verdict-with-no-consequence`:
"cannot tell" has to defer to a named owner and eventually escalate, or it is
just a way of never being wrong.

The four checks below are each the failure that would otherwise be discovered on
2026-09-21, at the moment the verdict was expected and could not be produced.

  1. COLLECTION_STALLED  — nothing recorded in >26h. Cron's longest normal gap
                           is 12h (02:10 -> 14:10), so 26h is two missed cycles.
  2. RESOLUTION_STALLED  — rows whose close_time has passed and that never
                           settled, so the sample grows and never matures. The
                           CAUSE is measured, not assumed: this check used to
                           state flatly that "--record is running and --resolve
                           is not", which on 2026-08-22 was false — --resolve had
                           run 2.8h earlier and settled 31 rows, while 43 of the
                           44 overdue rows had simply never entered its 400-row
                           lookup window. An alarm that names a cause it never
                           measured sends the reader to restart a healthy cron
                           job instead of to the real defect.
  3. STRATUM_UNREACHABLE — a horizon stratum needs more settled rows than it has
                           pending, and no new inventory is arriving for it. The
                           gate cannot open from here; waiting is not a plan.
  4. GATE_SLIPPED        — the projected opening date moved materially later
                           than the last run's. Drift is legitimate; unreported
                           drift is how a 34-day wait quietly becomes a 6-month
                           one.

Every threshold and the selection rule are IMPORTED from kalshi_calibration, not
restated. A watchdog carrying its own copy of the rule it supervises is the
`sample-outlived-its-rule` bug with extra steps — the numbers would stay right
while the sentence around them went stale.

Output: data/kalshi_watchdog_state.json  (projection history, for drift)
Escalates: data/chairman_action_required.json, data/firm_channel.json

Run:  python3 kalshi_watchdog.py --report
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

PREDICTIONS       = DATA_DIR / "kalshi_predictions.json"
WATCHDOG_STATE    = DATA_DIR / "kalshi_watchdog_state.json"
CHAIRMAN_ACTIONS  = DATA_DIR / "chairman_action_required.json"
FIRM_CHANNEL      = DATA_DIR / "firm_channel.json"
VM_CRONTAB_SNAP   = DATA_DIR / "vm_crontab_snapshot.txt"

# The string whose presence in a crontab means "this host collects Kalshi data".
SCANNER_MARKER = "kalshi_scanner.py"
# A crontab snapshot older than this cannot be used to claim the other host is
# still collecting — it only proves it was, once.
SNAPSHOT_STALE_HOURS = 72.0

# Cron records at 02:10, 14:10, 18:10, 22:10 UTC — longest normal gap is 12h.
STALL_HOURS          = 26.0
# A contract past its close that has not settled after this long means --resolve
# is not running. Kalshi settles most markets within hours of close.
OVERDUE_HOURS        = 48.0
OVERDUE_COUNT_ALARM  = 20
# `--resolve` runs once a day (45 11 * * *) and writes `settled_at` only when it
# actually settles something. A RECENT settled_at therefore proves the job is
# alive; its ABSENCE proves nothing on its own, because a run that found nothing
# ready to settle leaves exactly the same trace as a run that never happened.
# So this threshold is only ever used to CONFIRM liveness, never to conclude
# death — the asymmetry is the point, and ignoring it is how the old wording got
# to "--resolve is not running" without having looked.
RESOLVER_ALIVE_HOURS = 30.0
# Intake is measured over this window to decide whether a short stratum can
# still fill from new scanning, or is structurally stuck.
INTAKE_WINDOW_DAYS   = 7
# Slippage beyond this is reported rather than absorbed silently.
GATE_SLIP_ALARM_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kalshi_watchdog")


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _load(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    tmp.replace(path)


def _parse_ts(value) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware UTC datetime, or None.

    Returns None rather than a sentinel date on purpose. A watchdog that
    substitutes 'now' or epoch for an unparseable timestamp would report a
    healthy age from corrupt data — `fallback-that-means-act`.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sev_rank(s: str) -> int:
    return {"OK": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}.get(s, 0)


def _is_settled(row: dict) -> bool:
    return bool(row.get("settled")) or row.get("outcome") is not None


# ---------------------------------------------------------------------------
# Which host is supposed to be collecting?
# ---------------------------------------------------------------------------

def collector_role(now: Optional[datetime] = None) -> dict:
    """Am I the collector, a reader, or is nobody collecting?

    This has to be answered before staleness means anything. The Kalshi cron
    runs on the VM only; the Mac holds a copy of kalshi_predictions.json left
    behind by the last deploy, and there is no state sync between the hosts.
    Run naively on the Mac, this watchdog reads "22.5h since the last record"
    and files a CRITICAL about a stall that is not happening — and an alarm that
    cries wolf daily is worse than no alarm, because it teaches the Chairman to
    scroll past the one day it is real. `host_divergence_is_a_bug_class`, in the
    direction people forget: red on the Mac is not red either.

    The role is DERIVED, never a hostname list — a list would have to be edited
    the day the cron moves, and would be wrong silently until someone noticed
    (`untracked-code-reached-production`). Two sources answer it:

      my own `crontab -l`      -> I schedule the scanner, so I am the collector
      data/vm_crontab_snapshot -> the other host does (deploy-allowlisted, so
                                  the Mac genuinely can see the VM's schedule)

    The third case is the one worth building for. If NEITHER crontab schedules
    the scanner, the campaign has no collector at all — that is the disaster
    Jack described as losing track of an agent, and it must be loud rather than
    inferred from a quiet dashboard. A snapshot older than
    SNAPSHOT_STALE_HOURS proves the VM *was* collecting, not that it *is*, so it
    cannot be used to stay silent — `presence-is-not-currency`.
    """
    import subprocess

    now = now or datetime.now(timezone.utc)

    mine = ""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if proc.returncode == 0:
            mine = proc.stdout
    except Exception:
        mine = ""
    i_collect = any(SCANNER_MARKER in ln and not ln.lstrip().startswith("#")
                    for ln in mine.splitlines())

    snap_has, snap_age = False, None
    if VM_CRONTAB_SNAP.exists():
        try:
            text = VM_CRONTAB_SNAP.read_text()
            snap_has = any(SCANNER_MARKER in ln and not ln.lstrip().startswith("#")
                           for ln in text.splitlines())
            mtime = datetime.fromtimestamp(VM_CRONTAB_SNAP.stat().st_mtime,
                                           tz=timezone.utc)
            snap_age = round((now - mtime).total_seconds() / 3600.0, 1)
        except Exception:
            pass

    snap_trusted = snap_has and snap_age is not None and snap_age <= SNAPSHOT_STALE_HOURS

    if i_collect:
        role, why = "collector", "this host's crontab schedules " + SCANNER_MARKER
    elif snap_trusted:
        role, why = "reader", (f"another host collects (vm_crontab_snapshot, "
                               f"{snap_age}h old)")
    elif snap_has:
        role, why = "reader_unverified", (
            f"vm_crontab_snapshot still lists the scanner but is {snap_age}h old "
            f"(>{SNAPSHOT_STALE_HOURS:.0f}h); it proves the other host WAS "
            f"collecting, not that it still is")
    else:
        role, why = "orphaned", (
            "no crontab visible from this host schedules " + SCANNER_MARKER +
            " — not this host's, and not the VM snapshot")

    return {"role": role, "evidence": why, "i_collect": i_collect,
            "snapshot_lists_scanner": snap_has, "snapshot_age_hours": snap_age}


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _stratum_of(days: object, strata) -> Optional[str]:
    if not isinstance(days, (int, float)):
        return None
    for name, lo, hi in strata:
        if lo <= days < hi:
            return name
    return None


def project(now: Optional[datetime] = None) -> dict:
    """Measure where the campaign actually is. No opinions, just counts + dates."""
    import kalshi_calibration as kc

    now = now or datetime.now(timezone.utc)
    raw = _load(PREDICTIONS, [])
    rows = raw if isinstance(raw, list) else raw.get("predictions", [])

    rule = kc.SELECTION_RULE
    current = [r for r in rows if r.get("selection_rule") == rule]

    # Freshness: newest recorded_at across the CURRENT rule's rows.
    recorded = [t for t in (_parse_ts(r.get("recorded_at")) for r in current) if t]
    newest = max(recorded) if recorded else None
    stall_hours = (now - newest).total_seconds() / 3600.0 if newest else None

    intake_cutoff = now - timedelta(days=INTAKE_WINDOW_DAYS)

    strata: Dict[str, dict] = {}
    for name, lo, hi in kc.HORIZON_STRATA:
        strata[name] = {"settled": 0, "pending_dates": [], "intake": 0}

    overdue: List[Tuple[datetime, str]] = []
    overdue_rows: List[dict] = []
    outside = 0

    for r in current:
        s = _stratum_of(r.get("days_to_close"), kc.HORIZON_STRATA)
        if s is None:
            outside += 1
            continue
        rec = _parse_ts(r.get("recorded_at"))
        if rec and rec >= intake_cutoff:
            strata[s]["intake"] += 1

        if _is_settled(r):
            strata[s]["settled"] += 1
            continue

        close = _parse_ts(r.get("close_time"))
        strata[s]["pending_dates"].append(close)
        if close and (now - close).total_seconds() / 3600.0 > OVERDUE_HOURS:
            overdue.append((close, r.get("ticker", "?")))
            overdue_rows.append(r)

    per_horizon = kc.MIN_SETTLED_PER_HORIZON
    total_settled = sum(v["settled"] for v in strata.values())

    report: Dict[str, dict] = {}
    # Every gate that is still SHUT, with the date it opens. A gate that is MET
    # contributes nothing, which is the whole reason this used to read
    # `max(gate_dates or [])` and why that expression is a ValueError on
    # exactly one day: the day the last sample gate is met, when the list is
    # empty. The watchdog would have died uncaught, from cron, on the morning
    # the campaign it supervises finally succeeded — reproduced at aedecd8
    # with 70 settled rows in each of the three strata.
    open_gates: Dict[str, str] = {}
    for name in strata:
        v = strata[name]
        dates = sorted(d for d in v["pending_dates"] if d)
        need = max(0, per_horizon - v["settled"])
        if need == 0:
            eta, reachable = "MET", True
        elif len(dates) >= need:
            eta, reachable = dates[need - 1].date().isoformat(), True
            open_gates[name] = eta
        else:
            eta, reachable = None, False
        report[name] = {
            "settled": v["settled"], "pending": len(dates), "need": need,
            "intake_7d": v["intake"], "eta": eta, "reachable": reachable,
        }

    # The 200-total gate, from the same pending inventory pooled across strata.
    all_dates = sorted(d for v in strata.values() for d in v["pending_dates"] if d)
    need_total = max(0, kc.MIN_SETTLED_FOR_VERDICT - total_settled)
    if need_total == 0:
        total_eta = "MET"
    elif len(all_dates) >= need_total:
        total_eta = all_dates[need_total - 1].date().isoformat()
        open_gates["total"] = total_eta
    else:
        total_eta = None

    # The estimator's gate is a gate. It is a DIFFERENT blocker from sample
    # size — discharged by walk-forward validation clearing, not by more
    # settlements — but it is dated, and this number is the one the firm reads
    # as "when a verdict becomes possible". It was absent from the max, and on
    # 2026-08-28 the two disagreed by 13 days: the estimator said 09-08, the
    # strata said 09-21, and only the accident that 09-21 is the later of the
    # two kept the reported date honest. Sourced from `estimator_status()`, the
    # module that owns the definition, never re-derived here.
    #
    # An UNDATED blocker outranks a dated one. If the estimator is absent or
    # unfitted there is no date at which it opens, so reporting the strata date
    # would be a constant that reads as "on track" for a gate that is not
    # ([[fallback-that-means-act]]).
    est_gate = None
    try:
        est = kc.estimator_status()
        if est.get("state") == kc.EST_NOT_SERVING:
            est_gate = est.get("earliest")
            if est_gate:
                open_gates["estimator"] = est_gate
            else:
                est_gate = "UNDATED"
        elif est.get("state") in (kc.EST_ABSENT, kc.EST_UNFITTED):
            est_gate = "UNDATED"
    except Exception:                                   # noqa: BLE001
        est_gate = "UNKNOWN"

    unreachable = [n for n, v in report.items() if not v["reachable"]]
    if unreachable:
        binding, binding_gate_name = None, "strata_without_inventory"
    elif total_eta is None:
        binding, binding_gate_name = None, "total_without_inventory"
    elif est_gate in ("UNDATED", "UNKNOWN"):
        binding, binding_gate_name = None, "estimator"
    elif not open_gates:
        binding, binding_gate_name = "MET", "none"
    else:
        binding_gate_name = max(open_gates, key=lambda k: open_gates[k])
        binding = open_gates[binding_gate_name]

    # Is --resolve actually running? `settled_at` is written by resolve_settlements
    # and by nothing else, so it is the one honest liveness signal for that job.
    # Measured across ALL rows, not just the current rule: the resolver is
    # rule-agnostic, and asking a rule-scoped question about a global job is the
    # `both-sides-of-a-comparison` mistake.
    settled_ats = sorted(t for t in (_parse_ts(r.get("settled_at")) for r in rows) if t)
    resolver_hours = ((now - settled_ats[-1]).total_seconds() / 3600.0
                      if settled_ats else None)
    settled_24h = sum(1 for t in settled_ats
                      if (now - t).total_seconds() <= 86400)

    # And if it IS running, is it reaching these rows? The budget and the
    # ordering are imported from the module that owns them, never restated here.
    try:
        window, n_pending = kc.resolve_candidates(rows)
        in_window = {id(r) for r in window}
        beyond = sum(1 for r in overdue_rows if id(r) not in in_window)
        budget = kc.RESOLVE_MAX_LOOKUPS
    except Exception:
        beyond, n_pending, budget = None, None, None

    return {
        "measured_at":   now.isoformat(),
        "collector":     collector_role(now),
        "selection_rule": rule,
        "regime_start":  getattr(kc, "REGIME_START", None),
        "n_examined":    len(current),
        "n_all_rules":   len(rows),
        "outside_strata": outside,
        "newest_record": newest.isoformat() if newest else None,
        "stall_hours":   None if stall_hours is None else round(stall_hours, 2),
        "total_settled": total_settled,
        "need_total":    need_total,
        "total_eta":     total_eta,
        "strata":        report,
        "overdue_unsettled": len(overdue),
        "oldest_overdue": min(overdue)[0].isoformat() if overdue else None,
        "resolver_last_settled_hours": (None if resolver_hours is None
                                        else round(resolver_hours, 2)),
        "resolver_settled_24h": settled_24h,
        "overdue_beyond_lookup_window": beyond,
        "resolve_pending": n_pending,
        "resolve_budget": budget,
        "binding_gate":  binding,
        # WHICH gate binds decides what you would DO about it. "2026-09-21"
        # alone reads as "wait for settlements" whoever wrote it, and that is
        # the wrong instruction when the estimator is the one holding the date.
        "binding_gate_name": binding_gate_name,
        "open_gates":    open_gates,
        "estimator_gate": est_gate,
        "unreachable":   unreachable,
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _resolution_cause(proj: dict) -> str:
    """Why are closed rows not settling? Attribute from evidence, or defer.

    The previous version of this sentence asserted "--record is running and
    --resolve is not". On 2026-08-22 that was simply false — --resolve had run
    2.8h earlier and settled 31 rows — and the reader it misdirects goes off to
    restart a healthy cron job while the real defect stands. `verdict-with-no-
    consequence`: a check that cannot tell must say so and name what to look at,
    not pick the tidier story.
    """
    beyond = proj.get("overdue_beyond_lookup_window")
    pending = proj.get("resolve_pending")
    budget = proj.get("resolve_budget")
    hours = proj.get("resolver_last_settled_hours")

    alive = (f"--resolve IS running: it settled {proj.get('resolver_settled_24h', 0)} "
             f"row(s) in the last 24h, most recently {hours:.1f}h ago"
             if hours is not None and hours <= RESOLVER_ALIVE_HOURS else None)

    # The starvation case outranks liveness, because it explains the symptom even
    # when the job is perfectly healthy — and it is the case a liveness-only
    # check is structurally unable to see.
    if beyond:
        who = alive or "the resolver's liveness is unconfirmed"
        extra = ""
        if pending and budget and pending > budget:
            extra = (f" It holds {pending} pending row(s) against a "
                     f"{budget}-lookup budget, so {pending - budget} go "
                     f"unexamined every run.")
        return (f"{beyond} of those row(s) sit BEYOND the resolver's lookup "
                f"window and have never been looked up, so this is a starved "
                f"queue, not a dead job ({who}).{extra}")

    if alive:
        return (f"{alive}, and every overdue row IS inside its lookup window — "
                f"so these markets are being asked about and Kalshi is not "
                f"returning a settled result for them. Check whether the "
                f"tickers still exist upstream.")

    if hours is None:
        return ("No row carries a settled_at, so --resolve has never completed a "
                "settlement here. Confirm it is scheduled before assuming the "
                "markets are at fault.")

    return (f"Nothing has settled for {hours:.1f}h. That is CONSISTENT with "
            f"--resolve not running, but does not prove it — a run that found "
            f"nothing ready leaves the same trace. Check the crontab and the "
            f"job's own log before concluding.")


def evaluate(proj: dict, prev: Optional[dict]) -> List[dict]:
    """Turn the projection into findings. Each carries its own denominator."""
    findings: List[dict] = []
    role = proj.get("collector", {}).get("role", "collector")
    evidence = proj.get("collector", {}).get("evidence", "")

    # 0. Is there a collector at all? This outranks every other check: if nobody
    #    is scheduled to scan, the freshness of the local file is beside the
    #    point and the campaign is over without anyone having decided to end it.
    if role == "orphaned":
        findings.append({
            "kind": "NO_COLLECTOR", "severity": "CRITICAL",
            "detail": (f"No host visible from here schedules {SCANNER_MARKER} "
                       f"({evidence}). The Kalshi measurement campaign has no "
                       f"collector, so the verdict gate can never open. This is "
                       f"an agent lost without a failure — nothing errored."),
        })
    elif role == "reader_unverified":
        findings.append({
            "kind": "COLLECTOR_UNVERIFIED", "severity": "MEDIUM",
            "detail": (f"Cannot confirm the collecting host is still scheduled: "
                       f"{evidence}. Refresh the crontab snapshot, or run this "
                       f"watchdog on the collector."),
        })

    # Every check below reads THIS host's kalshi_predictions.json. On a reader
    # that file is a deploy artifact — its age, its counts and its projected
    # dates all describe whatever the collector happened to have at the last
    # rsync, not the campaign. So a reader measures nothing and says so, rather
    # than reporting a confident number about another machine's state.
    if role != "collector":
        return findings

    # 1. Is anything still being collected?
    sh = proj["stall_hours"]
    if sh is None:
        findings.append({
            "kind": "COLLECTION_STALLED", "severity": "CRITICAL",
            "detail": (f"No row under the current rule carries a readable "
                       f"recorded_at ({proj['n_examined']} row(s) examined). "
                       f"The scanner may be writing, but nothing can be aged."),
        })
    elif sh > STALL_HOURS:
        findings.append({
            "kind": "COLLECTION_STALLED", "severity": "CRITICAL",
            "detail": (f"Nothing recorded for {sh:.1f}h (alarm >{STALL_HOURS}h; "
                       f"longest normal cron gap is 12h). Newest row "
                       f"{proj['newest_record']}. The measurement campaign has "
                       f"stopped and the verdict date is no longer meaningful."),
        })

    # 2. Is what was collected maturing?
    if proj["overdue_unsettled"] >= OVERDUE_COUNT_ALARM:
        findings.append({
            "kind": "RESOLUTION_STALLED", "severity": "HIGH",
            "detail": (f"{proj['overdue_unsettled']} of {proj['n_examined']} "
                       f"row(s) closed >{OVERDUE_HOURS:.0f}h ago and never "
                       f"settled (oldest {proj['oldest_overdue']}). "
                       + _resolution_cause(proj)),
        })

    # 3. Can the gate still be reached from current inventory?
    for name in proj["unreachable"]:
        s = proj["strata"][name]
        sev = "HIGH" if s["intake_7d"] == 0 else "MEDIUM"
        findings.append({
            "kind": "STRATUM_UNREACHABLE", "severity": sev,
            "detail": (f"Horizon {name} needs {s['need']} more settled row(s) "
                       f"but holds only {s['pending']} pending; {s['intake_7d']} "
                       f"new row(s) arrived in the last {INTAKE_WINDOW_DAYS}d. "
                       + ("No inventory is arriving, so this gate cannot open by "
                          "waiting — the selection rule admits too little in this "
                          "horizon."
                          if s["intake_7d"] == 0 else
                          "Inventory is still arriving; reachable if intake holds.")),
        })

    # 3b. Which gate is the last one shut? Every check above measures intake,
    #     so a clean run reads as "collection is on track" — true, and beside
    #     the point when the thing holding the verdict is not collection at
    #     all. The estimator's gate is discharged by validation clearing, and
    #     no amount of settling discharges it, so it needs saying out loud or
    #     the firm keeps waiting for the wrong event.
    name = proj.get("binding_gate_name")
    if name == "estimator":
        eg = proj.get("estimator_gate")
        findings.append({
            "kind": "GATE_IS_NOT_SAMPLE_SIZE", "severity": "MEDIUM",
            "detail": (f"Every sample gate this watchdog measures is on track, "
                       f"but the verdict is held by the ESTIMATOR, which is "
                       f"{'undated' if eg in ('UNDATED', 'UNKNOWN') else eg}. "
                       f"More settlements do not discharge it — walk-forward "
                       f"validation clearing does. Reading the date above as "
                       f"'collect until then' waits for the wrong event."),
        })
    elif name and name not in ("none",) and proj.get("open_gates", {}).get("estimator"):
        # Both gates dated and the estimator is NOT the later one: fine today,
        # worth stating, because the pair is exactly what made this invisible.
        findings.append({
            "kind": "GATE_BEHIND_ANOTHER_GATE", "severity": "LOW",
            "detail": (f"Binding gate is `{name}` at {proj.get('binding_gate')}; "
                       f"the estimator opens earlier, at "
                       f"{proj['open_gates']['estimator']}. Both must clear, so "
                       f"the earlier one is not the date to plan against."),
        })

    # 4. Is the date drifting?
    if prev and prev.get("binding_gate") and proj.get("binding_gate"):
        try:
            was = datetime.fromisoformat(prev["binding_gate"]).date()
            now_ = datetime.fromisoformat(proj["binding_gate"]).date()
            slip = (now_ - was).days
            if slip >= GATE_SLIP_ALARM_DAYS:
                findings.append({
                    "kind": "GATE_SLIPPED", "severity": "MEDIUM",
                    "detail": (f"Projected verdict date moved {slip}d later "
                               f"({was} -> {now_}) since the previous run at "
                               f"{prev.get('measured_at', '?')[:16]}."),
                })
        except ValueError:
            pass

    return findings


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def _escalate(findings: List[dict], proj: dict) -> int:
    """HIGH/CRITICAL reach the Chairman; everything reaches the firm channel."""
    filed = 0
    now = datetime.now(timezone.utc).isoformat()

    actions = _load(CHAIRMAN_ACTIONS, [])
    if not isinstance(actions, list):
        actions = []

    for f in findings:
        title = f"[Kalshi] {f['kind']}"
        if _sev_rank(f["severity"]) >= 2:
            # Dedup on the kind, not the wording: the numbers inside the detail
            # change every run, so matching on the full text would file a fresh
            # duplicate daily and bury the queue.
            open_same = any(
                a.get("source") == "Kalshi Watchdog"
                and a.get("description", "").startswith(title)
                and a.get("status") not in ("resolved", "dismissed", "closed")
                for a in actions
            )
            if not open_same:
                actions.append({
                    "id": f"kalshi-wd-{f['kind'].lower()}-"
                          f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                    "source": "Kalshi Watchdog",
                    "severity": f["severity"],
                    "description": f"{title} — {f['detail']}",
                    "status": "open",
                    "resolution_note": "",
                    "ts": now,
                    "times_seen": 1,
                })
                filed += 1

    if filed:
        _save(CHAIRMAN_ACTIONS, actions)

    fc = _load(FIRM_CHANNEL, [])
    if isinstance(fc, list) and findings:
        for f in findings:
            fc.append({
                "ts": now, "date": now[:10],
                "agent": "Kalshi Watchdog", "category": "KALSHI_HEALTH",
                "priority": f["severity"],
                "content": f"[{f['kind']}] {f['detail']}",
            })
        _save(FIRM_CHANNEL, fc[-500:])

    return filed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(write: bool = True) -> dict:
    proj = project()
    hist = _load(WATCHDOG_STATE, {})
    if not isinstance(hist, dict):
        hist = {}
    prev = hist.get("last")

    findings = evaluate(proj, prev)
    filed = _escalate(findings, proj) if write else 0

    worst = "OK"
    for f in findings:
        if _sev_rank(f["severity"]) > _sev_rank(worst):
            worst = f["severity"]

    result = {**proj, "status": worst, "findings": findings, "escalated": filed}

    if write:
        history = hist.get("history", [])
        history.append({k: proj[k] for k in
                        ("measured_at", "total_settled", "binding_gate", "stall_hours")})
        _save(WATCHDOG_STATE, {"last": proj, "history": history[-90:]})

    return result


def _print_report(res: dict) -> None:
    print("=" * 66)
    print("  KALSHI WATCHDOG")
    print("=" * 66)
    c = res.get("collector", {})
    print(f"  role          : {c.get('role','?')} — {c.get('evidence','')}")
    print(f"  rule          : {res['selection_rule']}")
    print(f"  regime start  : {res['regime_start']}")
    print(f"  rows (rule)   : {res['n_examined']} of {res['n_all_rules']} lifetime")
    sh = res["stall_hours"]
    print(f"  last recorded : {res['newest_record']} "
          f"({'unknown' if sh is None else f'{sh:.1f}h ago'})")
    print()
    print(f"  {'horizon':<9}{'settled':>8}{'pending':>9}{'need':>6}{'intake7d':>10}   opens")
    for name, s in res["strata"].items():
        print(f"  {name:<9}{s['settled']:>8}{s['pending']:>9}{s['need']:>6}"
              f"{s['intake_7d']:>10}   {s['eta'] or 'UNREACHABLE'}")
    print(f"\n  total settled : {res['total_settled']} "
          f"(need {res['need_total']} more -> {res['total_eta'] or 'UNREACHABLE'})")
    print(f"  overdue unsettled: {res['overdue_unsettled']}")
    # The gate's NAME travels with its date. A bare date is read as an
    # instruction ("collect until then") and only one of the gate names makes
    # that instruction the right one.
    print(f"\n  BINDING GATE  : {res['binding_gate'] or 'UNREACHABLE'}"
          f"  (gate: {res.get('binding_gate_name', '?')})")
    if res.get("open_gates"):
        print("  gates still shut: "
              + ", ".join(f"{k}={v}" for k, v in sorted(res["open_gates"].items(),
                                                        key=lambda kv: kv[1])))
    print(f"  STATUS        : {res['status']}")
    for f in res["findings"]:
        print(f"\n  [{f['severity']}] {f['kind']}\n    {f['detail']}")
    if not res["findings"]:
        # "No findings" on a reader would be a verdict about a machine this
        # process never looked at. The counts above came from a deploy artifact.
        if c.get("role") == "collector":
            print("\n  No findings — collection advancing, gate reachable.")
        else:
            print(f"\n  NOT MEASURED HERE — this host is a {c.get('role')}, so the "
                  f"\n  figures above are last-deploy leftovers, not the campaign."
                  f"\n  Run on the collector for a real reading.")
    print("=" * 66)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Supervise the Kalshi measurement campaign")
    ap.add_argument("--report", action="store_true", help="print the projection table")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate without filing escalations or writing state")
    args = ap.parse_args()

    res = run(write=not args.dry_run)
    if args.report:
        _print_report(res)
    else:
        logger.info("Kalshi watchdog: %s — gate %s, %d finding(s), %d escalated",
                    res["status"], res["binding_gate"] or "UNREACHABLE",
                    len(res["findings"]), res["escalated"])
    return 2 if res["status"] == "CRITICAL" else 0


if __name__ == "__main__":
    sys.exit(main())
