#!/usr/bin/env python3
"""
Job Failure Monitor  (Department: Operations / health)
======================================================
WHY THIS EXISTS
---------------
2026-11-09 POSTMORTEM. The board's CIT vote crashed in every meeting from about
2026-08-18 to 2026-11-09 (a label confidence "HIGH" met `conf >= 75`). The
error line was in the logs the whole time: in board_*.log, cron_*.log, run_*.log
and 23 times in the canary's own log. No monitor acted on it, because every
monitor we had asks a different question:
  • freshness_sentinel  — "did the file get written recently?"  The meeting
    report was rewritten every day, so it read as fresh.
  • canary / catch_up   — "did the job run and exit 0?"  It did: the vote's
    except logged "CIT vote error (non-fatal)" and the meeting carried on.
  • coherence laws      — "do two measurements agree?"  Nothing measured the vote.
A job that runs, exits 0, writes its report, and fails inside is invisible to
all three. The Chairman's words: "when something isn't running as smoothly as
possible, I'd like it to be fixed — that's why I have so many health monitors."

WHAT IT DOES (hourly, VM)
  1. Reads every log in logs/ dated within WINDOW_DAYS (plus undated logs
     modified in that window) and extracts failure SIGNATURES:
       - the final exception line of every Python Traceback;
       - swallowed errors: WARNING/ERROR lines that say "non-fatal", or that
         name an exception class (TypeError, KeyError, ... Exception).
     A signature is the message with numbers, hex and quoted values collapsed,
     so the same crash on different days/rows/tickers is one signature.
  2. Reads structured status files that jobs write about themselves
     (STATUS_FILE_NAMES; first: data/board_vote_status.json), where ok=false is a
     failure even when the log is quiet.
  3. A signature is REPEATED when it is seen on >= REPEAT_DAYS distinct days or
     >= REPEAT_COUNT times in the window. Repeated failures are escalated:
       - one email to the Chairman listing all of them (force-sent, because a
         digest is where the board-vote line already sat unread). A signature
         re-alerts only when it shows up on a NEW day (and EMAIL_COOLDOWN_H has
         passed); until then it is KNOWN and the report stays green;
       - one BUG_FIX proposal per signature to the CIT queue via
         cit_director.submit_to_pod_queue (title-deduped there), so the firm's
         fix machinery owns it, not just an inbox.
  4. Writes data/job_failure_report.json (freshness_sentinel watches it).

CAN'T TELL IS NOT OK: zero logs examined → status CANNOT_TELL and exit 2.
No LLM calls. Usage:
  python3 job_failure_monitor.py               # scan, escalate, write report
  python3 job_failure_monitor.py --report      # scan + print, no email/proposal/state
  python3 job_failure_monitor.py --baseline    # at deploy: mark today's failures known
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
DATA = BASE / "data"
REPORT_PATH = DATA / "job_failure_report.json"
STATE_PATH = DATA / "job_failure_state.json"

WINDOW_DAYS = 4          # Fri + Mon of a weekday job are 2 runs inside 4 days
REPEAT_DAYS = 2
REPEAT_COUNT = 3
EMAIL_COOLDOWN_H = 12   # and only for a failure seen on a NEW day since its last alert
MAX_LOG_BYTES = 20_000_000     # read the tail of anything bigger

# Jobs that report on themselves. ok=false (or a fatal_error) is a failure.
# Names, not Paths: a module-level container of DATA-derived paths would not
# follow a test (or a redirected run) that rebinds DATA.
STATUS_FILE_NAMES = {
    "board_vote": "board_vote_status.json",
}


def status_files() -> dict:
    return {job: DATA / name for job, name in STATUS_FILE_NAMES.items()}

_DATED = re.compile(r"^(?P<job>.+?)_(?P<d>\d{8})\.log$")
_TS_PREFIX = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}(,\d+)?\s*")
_LEVEL = re.compile(r"\b(WARNING|ERROR|CRITICAL)\b")
_EXC_NAME = re.compile(r"\b[A-Z][A-Za-z]*(Error|Exception)\b")
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning))(:|$)")


def signature_of(text: str) -> str:
    """Collapse the volatile parts of an error message so recurrences match."""
    t = _TS_PREFIX.sub("", text.strip())
    t = re.sub(r"0x[0-9a-fA-F]+", "0x#", t)
    t = re.sub(r"'[^']{0,80}'", "'…'", t) if "not supported between" not in t else t
    t = re.sub(r"\d+(\.\d+)?", "#", t)
    t = re.sub(r"\s+", " ", t)
    return t[:200]


def _sig_id(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def _read(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > MAX_LOG_BYTES:
                f.seek(size - MAX_LOG_BYTES)
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def extract_failures(lines: list[str]) -> list[str]:
    """Failure messages in one log: traceback final lines + swallowed errors."""
    out = []
    in_tb = False
    for line in lines:
        s = line.rstrip()
        if "Traceback (most recent call last):" in s:
            in_tb = True
            continue
        if in_tb:
            if s.startswith(" ") or not s:
                continue
            m = _EXC_LINE.match(s)
            if m:
                out.append(s)
                in_tb = False
                continue
            if s.startswith("During handling") or s.startswith("The above exception"):
                continue
            in_tb = False      # something else; fall through and scan it
        lvl = _LEVEL.search(s)
        if lvl and ("non-fatal" in s.lower() or _EXC_NAME.search(s[lvl.end():])):
            out.append(s[lvl.end():].strip(" :-"))
    return out


def _log_day(path: Path, now: datetime):
    m = _DATED.match(path.name)
    if m:
        try:
            return m.group("job"), datetime.strptime(m.group("d"), "%Y%m%d").date()
        except ValueError:
            pass
    try:
        mt = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None, None
    return path.stem, mt.date()


def scan_logs(log_dir: Path = LOG_DIR, now: datetime | None = None,
              window_days: int = WINDOW_DAYS) -> tuple[dict, int]:
    """→ ({sig: {"jobs", "days", "count", "sample"}}, n_logs_examined)."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=window_days - 1)).date()
    found: dict = {}
    n = 0
    if not log_dir.is_dir():
        return found, 0
    for path in sorted(log_dir.glob("*.log")):
        job, day = _log_day(path, now)
        if day is None or day < cutoff:
            continue
        n += 1
        for msg in extract_failures(_read(path)):
            sig = signature_of(msg)
            rec = found.setdefault(sig, {"jobs": set(), "days": set(),
                                         "count": 0, "sample": msg[:300]})
            rec["jobs"].add(job)
            rec["days"].add(day.isoformat())
            rec["count"] += 1
    return found, n


def scan_status_files(state: dict, files: dict = None) -> dict:
    """Self-reported failures. Repeated = the last REPEAT_DAYS distinct runs failed."""
    files = status_files() if files is None else files
    found = {}
    hist = state.setdefault("status_history", {})
    for job, path in files.items():
        try:
            st = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        run_id = st.get("finished_at") or st.get("started_at")
        if not run_id:
            continue
        ok = bool(st.get("ok")) and not st.get("fatal_error")
        h = hist.setdefault(job, [])
        if not h or h[-1]["run"] != run_id:
            h.append({"run": run_id, "ok": ok})
            del h[:-10]
        if ok:
            continue
        why = st.get("fatal_error") or (
            f"{len(st.get('row_errors') or [])} row error(s) at stage {st.get('stage')}")
        sig = signature_of(f"[{job}] {why}")
        recent = h[-REPEAT_DAYS:]
        found[sig] = {"jobs": {job}, "days": {r["run"][:10] for r in recent if not r["ok"]},
                      "count": sum(1 for r in recent if not r["ok"]),
                      "sample": f"{path.name}: {why}"[:300],
                      "consecutive_failed_runs": len(recent) == REPEAT_DAYS
                                                 and not any(r["ok"] for r in recent)}
    return found


def is_repeated(rec: dict) -> bool:
    if "consecutive_failed_runs" in rec:
        return rec["consecutive_failed_runs"]
    return len(rec["days"]) >= REPEAT_DAYS or rec["count"] >= REPEAT_COUNT


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _email(repeated: list[dict]) -> bool:
    rows = "".join(
        f"<li><b>{r['jobs']}</b>: {r['sample']}<br>"
        f"<span style='color:#8b949e'>seen {r['count']}× on {len(r['days'])} day(s): "
        f"{', '.join(sorted(r['days']))}</span></li>" for r in repeated)
    body = (f"<p>{len(repeated)} job failure(s) repeated in the last {WINDOW_DAYS} days. "
            f"Each one has been filed to CIT as a BUG_FIX proposal. Full list: "
            f"data/job_failure_report.json on the VM.</p><ul>{rows}</ul>")
    try:
        from email_sender import send_alert_email
        return send_alert_email(
            f"Apex: {len(repeated)} job(s) failing repeatedly", body, force=True)
    except Exception as e:
        print(f"  email failed: {e!r}")
        return False


def _file_proposal(r: dict) -> None:
    """Hand the failure to the firm's fix machinery (main board via CIT)."""
    try:
        from cit_director import submit_to_pod_queue
        submit_to_pod_queue({
            "id": f"job-failure-{r['sig_id']}",
            "source": "job_failure_monitor",
            "category": "BUG_FIX",
            "severity": "HIGH",
            "confidence": 90,
            "title": f"[JOB FAILURE] {r['jobs'][0]} failing repeatedly: {r['signature'][:80]}",
            "description": (f"Seen {r['count']}x on {len(r['days'])} day(s) "
                            f"({', '.join(sorted(r['days']))}) in jobs {r['jobs']}. "
                            f"Sample: {r['sample']}"),
            "evidence": f"data/job_failure_report.json sig_id={r['sig_id']}",
            "proposed_fix": "Reproduce from the log, fix the root cause with a failing-first "
                            "test, deploy, and confirm the signature stops appearing.",
            "auto_implementable": False,
            "ts": datetime.now().isoformat(),
        })
    except Exception as e:
        print(f"  proposal for {r['sig_id']} failed: {e!r}")


def run(write: bool = True, log_dir: Path = LOG_DIR, now: datetime | None = None,
        baseline: bool = False) -> dict:
    now = now or datetime.now()
    state = _load_state()
    found, n_logs = scan_logs(log_dir, now)
    found.update(scan_status_files(state))

    failures = []
    for sig, rec in found.items():
        failures.append({
            "sig_id": _sig_id(sig), "signature": sig,
            "jobs": sorted(rec["jobs"]), "days": sorted(rec["days"]),
            "count": rec["count"], "sample": rec["sample"],
            "repeated": is_repeated(rec),
        })
    failures.sort(key=lambda f: (not f["repeated"], -f["count"]))

    # ACTIVE vs KNOWN. A repeated failure that was already escalated and has not
    # appeared on any NEW day since is KNOWN: it was reported, and the evidence
    # left in older logs is history, not a fresh failure. Without this, a crash
    # fixed today would keep the report red (and re-email) until its last log
    # line aged out of the window. A known failure that shows up on a new day is
    # ACTIVE again and re-alerts, so "still crashing" is never mistaken for
    # "already reported".
    alerted = state.setdefault("alerted", {})
    for f in failures:
        prev = alerted.get(f["sig_id"])
        f["new_days"] = sorted(set(f["days"]) - set(prev["days"])) if prev else f["days"]
        f["active"] = f["repeated"] and bool(f["new_days"])
    repeated = [f for f in failures if f["repeated"]]
    active = [f for f in repeated if f["active"]]

    status = "CANNOT_TELL" if n_logs == 0 else ("FAILING" if active else "OK")
    report = {
        "generated_at": now.isoformat(), "status": status,
        "ok": status == "OK",          # freshness_sentinel verdict_key
        "window_days": WINDOW_DAYS, "n_logs_examined": n_logs,
        "n_status_files": len(STATUS_FILE_NAMES),
        "n_failure_signatures": len(failures), "n_repeated": len(repeated),
        "n_active": len(active), "n_known": len(repeated) - len(active),
        "failures": failures,
    }

    if write:
        due = [f for f in active
               if f["sig_id"] not in alerted
               or now - datetime.fromisoformat(alerted[f["sig_id"]]["at"])
               >= timedelta(hours=EMAIL_COOLDOWN_H)]
        if baseline:
            # Deploy-time only: record what is already failing as KNOWN without
            # emailing, so the first scheduled run alerts on failures that are
            # new or recur, not on the ones the deploy just fixed.
            for f in repeated:
                alerted[f["sig_id"]] = {"at": now.isoformat(), "days": f["days"],
                                        "baseline": True}
                f["active"], f["new_days"] = False, []
            due = []
            # The written report must say what the NEXT run will see. A baseline
            # that saved its pre-baseline FAILING made the sentinel go RED on the
            # 2026-11-09 deploy for a failure that had just been marked known.
            report.update(n_active=0, n_known=len(repeated),
                          status="CANNOT_TELL" if n_logs == 0 else "OK")
            report["ok"] = report["status"] == "OK"
        elif due:
            for f in due:
                _file_proposal(f)
            if _email(due):
                for f in due:
                    alerted[f["sig_id"]] = {"at": now.isoformat(), "days": f["days"]}
        report["escalated_this_run"] = [f["sig_id"] for f in due]
        import atomic_json as aj
        aj.write(REPORT_PATH, report)
        aj.write(STATE_PATH, state)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--report", action="store_true",
                    help="scan and print only: no email, no proposals, no files written")
    ap.add_argument("--baseline", action="store_true",
                    help="record failures repeating now as already reported (no email); "
                         "use once at deploy so fixed failures don't alert")
    args = ap.parse_args()
    rep = run(write=not args.report, baseline=args.baseline)
    print(f"Job failure monitor: {rep['status']} — {rep['n_logs_examined']} log(s) examined, "
          f"{rep['n_failure_signatures']} failure signature(s), {rep['n_repeated']} repeated "
          f"({rep['n_active']} active, {rep['n_known']} already reported)")
    for f in rep["failures"]:
        mark = "ACTIVE  " if f["active"] else ("known   " if f["repeated"] else "once    ")
        print(f"  {mark} x{f['count']:<4} {','.join(f['jobs'])[:40]:40s} {f['signature'][:110]}")
    if rep["status"] == "CANNOT_TELL":
        sys.exit(2)
    sys.exit(0)   # FAILING is the detector working; only CANNOT_TELL is an error


if __name__ == "__main__":
    main()
