#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — POD HEALTH MONITOR
# =============================================================================
# One agile, uniform health check across EVERY pod (RD, ML, Macro/ETF, FI, FE).
#
# Why this exists: the FI pod ran for weeks drowning in yfinance 404 errors and
# the FE pod produced an empty slate for days — and the first time anyone noticed
# was a manual email to the Chairman. Freshness checks alone miss this: a pipeline
# can be perfectly "fresh" while producing garbage or nothing. This monitor checks
# three distinct failure modes per pod and, when it finds one, AUTOMATICALLY:
#   1. files a board proposal (cit_pending_proposals.json) with a specific fix, and
#   2. raises a Chairman action item (chairman_action_required.json) with a plan, and
#   3. posts to the firm channel.
# So a "pipeline isn't running / isn't producing" issue contacts the board and the
# Chairman with a remediation plan the moment it happens — no manual discovery.
#
# Failure modes detected per pod:
#   STALE        — pipeline report older than max_age_h (or missing)
#   ZERO_PICKS   — pipeline ran recently but produced 0 qualified picks
#   ERROR_FLOOD  — today's log has more than ERROR_FLOOD_THRESHOLD ERROR lines
#
# Usage:
#   python3 pod_health_monitor.py            # check + auto-escalate, print report
#   python3 pod_health_monitor.py --quiet    # no firm-channel post
# Exits nonzero if any CRITICAL problem is found (for cron alerting).
#
# Schedule: nightly, after the pod pipelines (see new_crontab.txt).
# =============================================================================

import argparse
import glob
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("pod_health")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
CIT_PENDING = DATA_DIR / "cit_pending_proposals.json"
CHAIRMAN_ACTIONS = DATA_DIR / "chairman_action_required.json"
FIRM_CHANNEL = DATA_DIR / "firm_channel.json"

ERROR_FLOOD_THRESHOLD = 50   # ERROR lines/day above this = a flood worth fixing

# Per-pod spec: where its freshness/quality signals live and how to fix it.
# picks_key/picks_path point at the file + JSON path holding the qualified picks.
POD_SPECS = {
    "FI": {
        "name": "Fixed Income",
        "report": "fi_pipeline_report.json", "max_age_h": 30,
        "picks_file": "fi_picks_queue.json",
        "log_glob": "logs/fi_*.log",
        "fix": "python3 fi_pipeline.py",
        "owner": "FI Pod Board + Chairman",
    },
    "FE": {
        "name": "First Eagle Value",
        "report": "fe_pipeline_log.json", "max_age_h": 30,
        "picks_file": "fe_picks_queue.json",
        "log_glob": "logs/fe_*.log",
        "fix": "python3 fe_pipeline.py  (then widen FE_VALUE_UNIVERSE if still 0 — keep the 25% MoS bar)",
        "owner": "FE Pod Board + Chairman",
    },
    "ETF": {
        "name": "Macro/ETF",
        "report": "etf_research_brief.json", "max_age_h": 30,
        "picks_file": "etf_pod_orders.json",
        "log_glob": "logs/*macro*.log",
        "fix": "python3 pod3_macro.py",
        "owner": "Macro Pod Board",
    },
}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def _age_hours(path: Path):
    if not path.exists():
        return None
    return (datetime.now().timestamp() - path.stat().st_mtime) / 3600.0


def _count_picks(path: Path) -> int:
    """Count qualified picks in a pod's picks file, tolerant of shape."""
    d = _load(path, [])
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        for k in ("picks", "qualified", "slate", "orders", "queue"):
            v = d.get(k)
            if isinstance(v, list):
                return len(v)
    return 0


def _todays_error_count(log_glob: str) -> int:
    files = sorted(glob.glob(str(BASE_DIR / log_glob)))
    if not files:
        return 0
    newest = max(files, key=lambda f: Path(f).stat().st_mtime)
    # only count if the log is from today (stale logs aren't a live flood)
    if (datetime.now().timestamp() - Path(newest).stat().st_mtime) > 30 * 3600:
        return 0
    try:
        with open(newest, errors="ignore") as f:
            return sum(1 for line in f if "ERROR" in line)
    except Exception:
        return 0


def _pod_positions(pod: str) -> int:
    """Held positions for a pod from the ledger. An order QUEUE that's empty because
    main.py executed+consumed the orders is healthy — held positions prove it.
    (The ETF pod triggered a false ZERO_PICKS: queue empty, but XLK/XLV/XLF held.)"""
    pa = _load(DATA_DIR / "pod_allocations.json", {})
    key = {"ETF": "pod3_macro", "FI": "pod_fi", "FE": "pod_fe"}.get(pod, "")
    p = (pa.get("pods", {}) or {}).get(key, {})
    return len(p.get("positions", {}) or {})


def _deploy_drift_check() -> dict:
    """PRODUCTION SELF-CHECK: is this host running stale code?

    deploy.sh ships deploy_stamp.json (repo SHA + date). If the GitHub repo's HEAD
    has moved past our stamp AND the stamp is >3 days old, production is drifting
    behind the codebase — exactly how the VM silently ran June-3 code for a month
    (the "two brains" incident). Absent stamp = dev machine, check skipped."""
    stamp_p = BASE_DIR / "deploy_stamp.json"
    if not stamp_p.exists():
        return {"status": "SKIPPED (no stamp — dev host)"}
    stamp = _load(stamp_p, {})
    try:
        import subprocess
        out = subprocess.run(
            ["git", "ls-remote", "git@github.com:jbeard1209/apex-capital.git", "HEAD"],
            capture_output=True, text=True, timeout=20)
        remote_sha = out.stdout.split()[0] if out.returncode == 0 and out.stdout else None
    except Exception:
        remote_sha = None
    if not remote_sha:
        return {"status": "UNKNOWN (cannot reach repo)", "stamp": stamp}
    local_sha = str(stamp.get("sha", ""))
    age_days = None
    try:
        dep = datetime.fromisoformat(str(stamp.get("deployed_at", "")).replace("Z", ""))
        age_days = (datetime.utcnow() - dep).days
    except Exception:
        pass
    drifted = local_sha and not remote_sha.startswith(local_sha) and (age_days or 0) > 3
    if drifted:
        detail = (f"Production code is STALE: deployed {local_sha} {age_days}d ago but the "
                  f"repo HEAD has moved ({remote_sha[:8]}). Run `bash gcp_setup/ship.sh` "
                  "from the Mac. (Prevention layer from the June/July stale-prod incident.)")
        _escalate("PROD", {"name": "Deploy drift", "fix": "bash gcp_setup/ship.sh (from Mac)",
                           "owner": "Chairman"}, "STALE_CODE", "CRITICAL", detail)
        return {"status": "DRIFTED", "deployed": local_sha, "repo": remote_sha[:8],
                "age_days": age_days}
    return {"status": "CURRENT", "deployed": local_sha, "age_days": age_days}


def check_pod(pod: str, spec: dict) -> dict:
    report = DATA_DIR / spec["report"]
    age = _age_hours(report)
    picks = _count_picks(DATA_DIR / spec["picks_file"])
    if picks == 0:
        picks = _pod_positions(pod)   # executed-and-held counts as deployed
    errors = _todays_error_count(spec["log_glob"])

    problems = []
    severity = "OK"
    if age is None or age > spec["max_age_h"]:
        problems.append(("STALE", "CRITICAL",
                         f"{spec['name']} pipeline report is "
                         f"{'missing' if age is None else f'{age:.0f}h old'} "
                         f"(max {spec['max_age_h']}h) — the pipeline may not be running."))
        severity = "CRITICAL"
    elif picks == 0:
        problems.append(("ZERO_PICKS", "HIGH",
                         f"{spec['name']} pipeline ran (report {age:.0f}h old) but produced "
                         f"0 qualified picks — the pod is deploying no capital."))
        severity = max(severity, "HIGH", key=_sev_rank)
    if errors > ERROR_FLOOD_THRESHOLD:
        problems.append(("ERROR_FLOOD", "HIGH",
                         f"{spec['name']} emitted {errors} ERROR lines in today's log "
                         f"(>{ERROR_FLOOD_THRESHOLD}) — likely a data/integration bug spamming failures."))
        severity = max(severity, "HIGH", key=_sev_rank)

    return {"pod": pod, "name": spec["name"], "age_h": age, "picks": picks,
            "errors": errors, "problems": problems,
            "severity": severity if problems else "OK"}


def _sev_rank(s: str) -> int:
    return {"OK": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}.get(s, 0)


def _escalate(pod: str, spec: dict, kind: str, severity: str, detail: str):
    """File a board proposal AND a Chairman action item with a concrete fix plan."""
    title = f"[{pod}] {kind}: {spec['name']} pod health"
    plan = (f"{detail}\n\nRecommended fix: {spec['fix']}\n"
            f"Owner: {spec['owner']}. Auto-raised by pod_health_monitor.")

    # 1) Board proposal (dedup by title)
    pending = _load(CIT_PENDING, [])
    if isinstance(pending, list) and title not in {p.get("title", "") for p in pending}:
        pending.append({
            "id": f"PHM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{pod}",
            "source": "Pod Health Monitor", "category": "INTEGRATION",
            "severity": severity, "confidence": 90, "title": title,
            "description": detail, "proposed_fix": spec["fix"], "owner": spec["owner"],
            "created_at": datetime.now().isoformat(), "status": "pending",
        })
        _save(CIT_PENDING, pending)

    # 2) Chairman action item (dedup by description prefix, only for CRITICAL/HIGH)
    if _sev_rank(severity) >= 2:
        actions = _load(CHAIRMAN_ACTIONS, [])
        if isinstance(actions, list) and not any(
            a.get("description", "").startswith(title) for a in actions
            if a.get("status") not in ("resolved", "dismissed")
        ):
            actions.append({
                "id": f"PHM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{pod}",
                "source": "Pod Health Monitor", "severity": severity,
                "description": f"{title} — {plan}", "status": "open",
                "resolution_note": "", "ts": datetime.now().isoformat(),
            })
            _save(CHAIRMAN_ACTIONS, actions)


def _post_channel(content: str, priority: str):
    fc = _load(FIRM_CHANNEL, [])
    if not isinstance(fc, list):
        return
    fc.append({"ts": datetime.now().isoformat(), "date": datetime.now().strftime("%Y-%m-%d"),
               "agent": "Pod Health Monitor", "category": "POD_HEALTH",
               "priority": priority, "content": content})
    _save(FIRM_CHANNEL, fc[-500:])


def run(quiet: bool = False) -> dict:
    logger.info("=" * 55)
    logger.info("  POD HEALTH MONITOR — all-pod uniform check")
    logger.info("=" * 55)

    results = {}
    any_critical = False
    escalated = 0
    for pod, spec in POD_SPECS.items():
        r = check_pod(pod, spec)
        results[pod] = r
        if r["problems"]:
            for kind, sev, detail in r["problems"]:
                logger.warning(f"  [{pod}] {kind} ({sev}): {detail}")
                _escalate(pod, spec, kind, sev, detail)
                escalated += 1
            if r["severity"] == "CRITICAL":
                any_critical = True
        else:
            logger.info(f"  [{pod}] OK — {r['picks']} picks, "
                        f"{r['age_h']:.0f}h old, {r['errors']} errors")

    if escalated and not quiet:
        bad = [f"{r['name']}: " + ", ".join(k for k, _, _ in r["problems"])
               for r in results.values() if r["problems"]]
        _post_channel("🩺 Pod health: " + " | ".join(bad) +
                      " — board proposals + Chairman actions auto-filed.",
                      "HIGH" if any_critical else "NORMAL")

    # Deploy-drift self-check (prod only): are we running stale code vs the repo?
    try:
        drift = _deploy_drift_check()
        results["_deploy_drift"] = drift
        if drift.get("status") == "DRIFTED":
            any_critical = True
            logger.warning(f"  [DeployDrift] {drift}")
    except Exception as _de:
        logger.debug(f"  deploy-drift check skipped: {_de}")

    # Shared-resource check — the one detector that survives common-mode failure:
    # unexplained orders on the Alpaca account mean ANOTHER HOST is trading it
    # (that's how the forgotten GCP VM stayed invisible to 40 local protections).
    try:
        from foreign_order_detector import run_check as _fod
        fod = _fod()
        results["_foreign_orders"] = {"status": fod.get("status"),
                                      "unexplained": len(fod.get("foreign", []))}
        if fod.get("status") == "FOREIGN_ORDERS":
            any_critical = True
    except Exception as _fe:
        logger.debug(f"  foreign-order check skipped: {_fe}")

    # Verified-closed hygiene (Chairman-ratified 2026-07-09): flip tracker items
    # covered by the resolution registry; break >=3-approval stale loops.
    try:
        from tracker_hygiene import run as _th
        results["_tracker_hygiene"] = _th()
    except Exception as _the:
        logger.debug(f"  tracker hygiene skipped: {_the}")

    logger.info(f"  {escalated} problem(s) escalated to board + Chairman")
    return {"results": results, "escalated": escalated, "critical": any_critical}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    out = run(quiet=args.quiet)
    sys.exit(1 if out["critical"] else 0)
