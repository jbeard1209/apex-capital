#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI LEDGER BACKUP  (Mac launchd, daily)
=========================================================
The copier's order ledger (VM: data/kalshi_copytrade_ledger.jsonl) is the only
record of every real-money Kalshi order. It lives on one disk, on one host,
and kalshi_copytrade.py only ever APPENDS to it (`LEDGER_FILE.open("a")`). So
two things are true of a healthy ledger, day over day:

  1. it never gets shorter, and
  2. yesterday's copy is a byte-for-byte PREFIX of today's.

This job reads the ledger over ssh (`cat`, read-only — it never writes to the
VM), saves a dated copy under ~/apex_backups/kalshi/, and checks both laws
against the newest earlier copy. A copy is never deleted or overwritten with
something shorter, so a shrink on the VM cannot propagate into the backups.

  SHRANK      fewer rows than the last backup       -> Chairman queue (HIGH) + email
  REWRITTEN   same-or-more rows, but the old rows   -> Chairman queue (HIGH) + email
              are no longer a prefix (history edited)
  STALE       no successful fetch for > 48h         -> Chairman queue (MEDIUM)
  OK          grew or unchanged, prefix intact

A failed fetch is CANNOT_TELL, never OK: it does not refresh last_ok, so it
turns into STALE if it keeps failing.

    /usr/bin/python3 tools/kalshi_ledger_backup.py            # backup + check + alert
    /usr/bin/python3 tools/kalshi_ledger_backup.py --no-alert # backup + check only
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACKER = ROOT / "institutional-tracker"
BACKUP_DIR = Path(os.environ.get("APEX_KALSHI_BACKUP_DIR",
                                 str(Path.home() / "apex_backups" / "kalshi")))
VM_PATH = "~/institutional-tracker/data/kalshi_copytrade_ledger.jsonl"
PREFIX = "kalshi_copytrade_ledger."
SOURCE = "kalshi_ledger_backup"
STALE_HOURS = 48


def fetch_vm_ledger(host="apex-capital", timeout=120):
    """Read the VM ledger. Returns bytes, or raises. Read-only: `cat`."""
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host,
         "cat " + VM_PATH], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError("ssh cat exited %d: %s" % (
            out.returncode, out.stderr.decode(errors="replace").strip()[:300]))
    if not out.stdout:
        # An empty read is not "the ledger is empty": the file has had rows
        # since 2026-11-04. Treat as a failed read, which the checker below
        # would otherwise score as SHRANK-to-zero with no way to tell apart.
        raise RuntimeError("ssh cat returned 0 bytes")
    return out.stdout


def backups(d):
    return sorted(p for p in Path(d).glob(PREFIX + "*.jsonl"))


def check(prev_bytes, new_bytes):
    """Compare the newest earlier backup with today's read."""
    prev_n = prev_bytes.count(b"\n")
    new_n = new_bytes.count(b"\n")
    if new_n < prev_n:
        verdict = "SHRANK"
    elif not new_bytes.startswith(prev_bytes):
        verdict = "REWRITTEN"
    else:
        verdict = "OK"
    return {"verdict": verdict, "prev_rows": prev_n, "new_rows": new_n,
            "added_rows": new_n - prev_n}


def save(d, data, now):
    """Write today's copy without ever replacing a longer one."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s%s.jsonl" % (PREFIX, now.strftime("%Y%m%d")))
    if path.exists():
        old = path.read_bytes()
        if old == data:
            return path
        # Same day, different content: keep both rather than overwrite.
        path = d / ("%s%s.jsonl" % (PREFIX, now.strftime("%Y%m%dT%H%M%S")))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))
    return path


def _status_path(d):
    return Path(d) / "backup_status.json"


def load_status(d):
    try:
        return json.loads(_status_path(d).read_text())
    except Exception:
        return {}


def run(fetch=fetch_vm_ledger, d=BACKUP_DIR, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    status = load_status(d)
    prior = backups(d)
    result = {"checked_at": now.isoformat(timespec="seconds"),
              "last_ok": status.get("last_ok")}
    try:
        data = fetch()
    except Exception as e:
        result.update(verdict="CANNOT_TELL", error="%s: %s" % (type(e).__name__, e))
        last_ok = status.get("last_ok")
        age_h = None
        if last_ok:
            age_h = (now - dt.datetime.fromisoformat(last_ok)).total_seconds() / 3600
        if age_h is None or age_h > STALE_HOURS:
            result["verdict"] = "STALE"
            result["age_h"] = age_h
    else:
        if prior:
            result.update(check(prior[-1].read_bytes(), data))
            result["compared_to"] = prior[-1].name
        else:
            result.update(verdict="OK", prev_rows=None, new_rows=data.count(b"\n"))
        result["saved"] = str(save(d, data, now))
        if result["verdict"] == "OK":
            result["last_ok"] = result["checked_at"]
    _status_path(d).parent.mkdir(parents=True, exist_ok=True)
    _status_path(d).write_text(json.dumps(result, indent=2))
    return result


def alert(result, submit=None, email=None):
    """Raise SHRANK / REWRITTEN / STALE on the existing Chairman path."""
    v = result["verdict"]
    if v not in ("SHRANK", "REWRITTEN", "STALE"):
        return None
    sys.path.insert(0, str(TRACKER))
    if submit is None:
        import chairman_queue
        submit = chairman_queue.submit
    desc = {
        "SHRANK": "The VM's Kalshi order ledger got SHORTER: %s rows yesterday, %s now. "
                  "Orders history was lost on the VM. Backups are intact in %s (%s)."
                  % (result.get("prev_rows"), result.get("new_rows"), BACKUP_DIR,
                     result.get("compared_to")),
        "REWRITTEN": "The VM's Kalshi order ledger was EDITED: its earlier rows no longer "
                     "match the last backup (%s). The ledger is append-only, so something "
                     "other than the copier changed history. Backups are in %s."
                     % (result.get("compared_to"), BACKUP_DIR),
        "STALE": "The daily Kalshi ledger backup has not succeeded for over %dh "
                 "(last_ok=%s, error=%s). The ledger has no off-VM copy since then."
                 % (STALE_HOURS, result.get("last_ok"), result.get("error")),
    }[v]
    now = result["checked_at"][:10].replace("-", "")
    entry = {"id": "kalshi-ledger-%s-%s" % (now, v.lower()),
             "dedup_key": "kalshi_ledger_backup:%s" % v,
             "severity": "MEDIUM" if v == "STALE" else "HIGH",
             "category": "INFRASTRUCTURE", "status": "awaiting_chairman",
             "raised_at": result["checked_at"],
             "description": desc + "\n\nRaised by tools/kalshi_ledger_backup.py (Mac launchd, daily). "
                                   "Do NOT reset the copier's error counter or edit the ledger."}
    added, _ = submit([entry], SOURCE,
                      path=str(TRACKER / "data" / "chairman_action_required.json"))
    if added and v != "STALE":
        cwd = os.getcwd()
        try:
            if email is None:
                os.chdir(str(TRACKER))
                from email_sender import send_alert_email as email
            email("Kalshi ledger %s" % v, "<p>%s</p>" % desc)
        except Exception as e:
            print("email failed (queue entry still raised): %s" % e, file=sys.stderr)
        finally:
            os.chdir(cwd)
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-alert", action="store_true")
    a = ap.parse_args()
    r = run()
    print(json.dumps(r))
    if not a.no_alert:
        alert(r)
    return 0 if r["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
