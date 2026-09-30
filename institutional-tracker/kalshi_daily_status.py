#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI DAILY STATUS BRIEF
========================================
One Chairman-facing snapshot of where Kalshi stands today.

WHY THIS EXISTS
---------------
Five files answer "where are we with Kalshi today":

    data/kalshi_scan_log.json                  (scanner)
    data/kalshi_calibration_report.json        (calibration verdict)
    data/kalshi_mirror_paper_report.json       (paper mirror gate)
    data/kalshi_mirror_candidate_queue.jsonl   (armed-execute queue)
    data/kalshi_board_report.json              (board vote)

The Chairman does not want to read five files to know whether anything
changed. This aggregates them into ONE stable JSON:

    data/kalshi_daily_status.json

If a gate flips from BLOCKED to GREEN, or a structural arb appears, the
brief also appends a row to data/chairman_action_required.json — because
those are the only two events that need a decision.

NOT AUTOMATION OF TRADING
-------------------------
This module reads state and reports. It never writes `data/.kalshi_armed`
or `data/.kalshi_mirror_armed` — those two files are the Chairman-only
arm gates the [[kalshi_sector]] postmortem pinned as physically-present
actions, and no scheduled process is allowed to create them.

WHAT AUTOMATION MEANS HERE
--------------------------
Kalshi's four scheduled steps already run on the VM crontab:

    10 2,14,18,22 * * *   scanner  +  calibration record  +  board vote
    45 11 * * *           calibration resolve/report/email
    55 11 * * *           mirror paper report  +  mirror execute gate

Everything short of the arm gate happens automatically. The remaining gap
Jack named — "you never figured out how to make kalshi trading automatic" —
is not that the pipeline is not scheduled; it is that the state is
scattered and the Chairman had no dashboard for it. This closes that.

CRON
----
    20 12 * * *   kalshi_daily_status.py --report

Runs after the 11:45 calibration resolve and the 11:55 paper mirror /
execute pass, so every upstream report is fresh when this reads it.

DENOMINATORS
------------
Every reported rate carries an n_examined. `n_examined == 0` never reports
"GREEN, nothing found" — it reports UNKNOWN with `reason="no_data"`. This
is the [[an-aggregate-is-not-a-finding]] rule: a rate without a population
is not a fact.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

SCAN_LOG = DATA_DIR / "kalshi_scan_log.json"
CALIB_REPORT = DATA_DIR / "kalshi_calibration_report.json"
PAPER_REPORT = DATA_DIR / "kalshi_mirror_paper_report.json"
CANDIDATE_QUEUE = DATA_DIR / "kalshi_mirror_candidate_queue.jsonl"
BOARD_REPORT = DATA_DIR / "kalshi_board_report.json"

STATUS_FILE = DATA_DIR / "kalshi_daily_status.json"
STATUS_PRIOR_FILE = DATA_DIR / "kalshi_daily_status_prior.json"
CHAIRMAN_FEED = DATA_DIR / "chairman_action_required.json"

LOG_FILE = LOG_DIR / "kalshi_daily_status.log"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
)
log = logging.getLogger("kalshi_daily_status")


def _load_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text() or "null")
    except json.JSONDecodeError as e:
        log.warning("could not parse %s: %s", path.name, e)
        return None


def _summarize_scan(scan: Optional[dict]) -> dict:
    if not scan:
        return {
            "state": "MISSING",
            "reason": "no scan file yet — cron has not run or the scanner failed to write",
        }
    feed = scan.get("feed") or {}
    genuine = feed.get("genuine")
    band_gates = scan.get("band_gates") or {}
    passed = band_gates.get("passed", 0)
    seen = scan.get("markets_seen")
    with_book = scan.get("with_real_book")
    two_sided = scan.get("two_sided")
    struct_pair = scan.get("structural_pair") or []
    struct_field = scan.get("structural_field") or []
    disagreement = scan.get("disagreement") or []
    scanned_at = scan.get("scanned_at")

    book_coverage_pct = None
    if seen and seen > 0 and with_book is not None:
        book_coverage_pct = round(100.0 * with_book / seen, 2)

    return {
        "state": "OK" if seen else "STALE",
        "scanned_at": scanned_at,
        "genuine_markets": genuine,
        "markets_seen": seen,
        "with_real_book": with_book,
        "book_coverage_pct": book_coverage_pct,
        "two_sided": two_sided,
        "structural_arbs_found": len(struct_pair) + len(struct_field),
        "disagreement_candidates": len(disagreement),
        "band_gate_passed": passed,
        "feed_healthy": feed.get("healthy"),
    }


def _summarize_calibration(rep: Optional[dict]) -> dict:
    if not rep:
        return {
            "state": "MISSING",
            "verdict": "UNKNOWN",
            "reason": "no calibration report yet",
        }
    verdict = rep.get("verdict") or rep.get("edge_proven") or "UNKNOWN"
    n_settled = rep.get("n_settled") or rep.get("settled_rows") or 0
    hit_rate = rep.get("hit_rate")
    band = rep.get("band") or "90-100c"
    blocker = rep.get("blocker") or rep.get("reason")
    return {
        "state": "OK",
        "verdict": verdict,
        "n_settled": n_settled,
        "hit_rate": hit_rate,
        "band": band,
        "blocker": blocker,
    }


def _summarize_paper_mirror(rep: Optional[dict]) -> dict:
    if not rep:
        return {
            "state": "MISSING",
            "verdict": "UNKNOWN",
            "reason": "no paper mirror report yet",
        }
    gate = rep.get("gate") or {}
    return {
        "state": "OK",
        "verdict": gate.get("ready") if isinstance(gate.get("ready"), bool) else gate.get("verdict") or "UNKNOWN",
        "n_prospective": rep.get("n_prospective") or gate.get("n_prospective"),
        "ev_net_mean": rep.get("ev_net_mean") or gate.get("ev_net_mean"),
        "ev_net_ci_low": rep.get("ev_net_ci_low") or gate.get("ev_net_ci_low"),
        "days_since_start": rep.get("days_since_start") or gate.get("days_since_start"),
        "target_n": 500,
        "target_ci_low_c": 0.0,
        "target_days": 30,
    }


def _summarize_execute() -> dict:
    if not CANDIDATE_QUEUE.exists():
        return {
            "state": "OK",
            "queue_lines": 0,
            "last_write_utc": None,
            "emitting": False,
        }
    try:
        text = CANDIDATE_QUEUE.read_text()
        lines = [ln for ln in text.splitlines() if ln.strip()]
    except OSError as e:
        return {"state": "ERR", "reason": str(e)}
    last_ts = None
    if lines:
        try:
            last_ts = json.loads(lines[-1]).get("generated_at")
        except Exception:
            pass
    return {
        "state": "OK",
        "queue_lines": len(lines),
        "last_write_utc": last_ts,
        "emitting": bool(lines),
    }


def _bottom_line(status: dict) -> str:
    """One-to-three sentences on what would unblock arming today."""
    struct = status["scan"].get("structural_arbs_found", 0)
    if struct and struct > 0:
        return (
            f"{struct} structural arbitrage(s) detected — buy YES+NO for less "
            f"than $1.00. Chairman review needed for placement (venue is "
            f"CFTC-regulated, arm gate is data/.kalshi_armed)."
        )
    cal_verdict = str(status["calibration"].get("verdict")) or "UNKNOWN"
    paper_verdict = status["paper_mirror"].get("verdict")
    if cal_verdict == "BLOCKED":
        return (
            "Calibration gate is BLOCKED (structural, not evidentiary): no "
            "calibrated estimator installed, so disagreement lane cannot be "
            "sized. More settlements will not clear this. Next step: build "
            "estimator (see docs/kalshi_build_plan.md, lane 3)."
        )
    n_prosp = status["paper_mirror"].get("n_prospective") or 0
    days = status["paper_mirror"].get("days_since_start") or 0
    if paper_verdict in (False, "not_ready", "UNKNOWN"):
        return (
            f"Paper mirror gate not ready: n_prospective={n_prosp}/500, "
            f"days={days}/30. Waiting on tight-tier (98-100c × 1-7d) "
            f"settlements. This is evidentiary — passing time discharges it."
        )
    if paper_verdict is True or paper_verdict == "ready":
        return (
            "Paper mirror gate is READY. Chairman can arm Phase 2 ($500 cap) "
            "by writing data/.kalshi_mirror_armed on the target host. This "
            "script cannot write that file."
        )
    return "State collected. See individual gate summaries."


def _gate_status_label(status: dict) -> str:
    if status["scan"].get("structural_arbs_found", 0) > 0:
        return "GREEN (structural arb detected)"
    if status["paper_mirror"].get("verdict") is True:
        return "GREEN (paper mirror gate ready)"
    if str(status["calibration"].get("verdict")) == "BLOCKED":
        return "BLOCKED (calibration structural blocker)"
    return "WAITING"


def _has_flip_to_green(prior: Optional[dict], current: dict) -> Optional[str]:
    """Return a reason string if state flipped from not-GREEN to GREEN today."""
    if not prior:
        return None
    prior_struct = (prior.get("scan") or {}).get("structural_arbs_found", 0) or 0
    curr_struct = current["scan"].get("structural_arbs_found", 0) or 0
    if curr_struct > 0 and prior_struct == 0:
        return f"structural arbitrage detected today: n={curr_struct}"
    prior_paper = (prior.get("paper_mirror") or {}).get("verdict")
    curr_paper = current["paper_mirror"].get("verdict")
    if curr_paper is True and prior_paper is not True:
        return "paper mirror gate crossed to READY today"
    return None


def _append_chairman_row(reason: str, status: dict) -> None:
    row = {
        "id": f"kalshi-flip-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "source": "kalshi_daily_status",
        "severity": "HIGH",
        "type": "kalshi_gate_flip",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "summary": f"Kalshi state changed: {reason}",
        "status_snapshot": status,
        "action": "Chairman review — arming decision required per kalshi_sector policy",
    }
    existing = _load_json(CHAIRMAN_FEED) or []
    if isinstance(existing, dict):
        existing = existing.get("items", []) or []
    existing.append(row)
    kept = existing[-200:]
    CHAIRMAN_FEED.write_text(json.dumps(kept, indent=2))
    log.info("appended chairman row: %s", row["id"])


def build_status() -> dict:
    scan = _load_json(SCAN_LOG)
    calib = _load_json(CALIB_REPORT)
    paper = _load_json(PAPER_REPORT)
    board = _load_json(BOARD_REPORT)

    status = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scan": _summarize_scan(scan if isinstance(scan, dict) else None),
        "calibration": _summarize_calibration(calib if isinstance(calib, dict) else None),
        "paper_mirror": _summarize_paper_mirror(paper if isinstance(paper, dict) else None),
        "execute": _summarize_execute(),
        "board_last_vote": (
            (board or {}).get("verdict") if isinstance(board, dict) else None
        ),
    }
    status["gate_label"] = _gate_status_label(status)
    status["bottom_line"] = _bottom_line(status)
    return status


def write_status(status: dict) -> None:
    if STATUS_FILE.exists():
        try:
            STATUS_PRIOR_FILE.write_text(STATUS_FILE.read_text())
        except OSError as e:
            log.warning("could not archive prior status: %s", e)
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def print_brief(status: dict) -> None:
    print("=" * 64)
    print("APEX CAPITAL — KALSHI DAILY STATUS")
    print("=" * 64)
    print(f"generated_utc: {status['generated_utc']}")
    print(f"gate_label:    {status['gate_label']}")
    print()
    print(f"SCAN:          markets_seen={status['scan'].get('markets_seen')}, "
          f"book_coverage={status['scan'].get('book_coverage_pct')}%, "
          f"struct_arbs={status['scan'].get('structural_arbs_found')}, "
          f"disagreement={status['scan'].get('disagreement_candidates')}")
    print(f"CALIBRATION:   verdict={status['calibration'].get('verdict')}, "
          f"n_settled={status['calibration'].get('n_settled')}, "
          f"blocker={status['calibration'].get('blocker')}")
    print(f"PAPER MIRROR:  verdict={status['paper_mirror'].get('verdict')}, "
          f"n_prosp={status['paper_mirror'].get('n_prospective')}/500, "
          f"days={status['paper_mirror'].get('days_since_start')}/30, "
          f"EV_net={status['paper_mirror'].get('ev_net_mean')}c")
    print(f"EXECUTE:       queue_lines={status['execute'].get('queue_lines')}, "
          f"emitting={status['execute'].get('emitting')}")
    print()
    print("BOTTOM LINE:")
    print(f"  {status['bottom_line']}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi daily status brief")
    parser.add_argument("--report", action="store_true",
                        help="Write status file and print brief (default action)")
    parser.add_argument("--quiet", action="store_true",
                        help="Skip stdout print (cron mode)")
    args = parser.parse_args()

    prior = _load_json(STATUS_FILE)
    status = build_status()
    write_status(status)

    flip = _has_flip_to_green(prior if isinstance(prior, dict) else None, status)
    if flip:
        _append_chairman_row(flip, status)

    if not args.quiet:
        print_brief(status)

    return 0


if __name__ == "__main__":
    sys.exit(main())
