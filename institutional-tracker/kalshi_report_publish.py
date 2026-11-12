#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI + POD REPORT PUBLISHER (hourly; VM cron or Mac launchd)
============================================================================
Chairman, 2026-11-09: wants Claude in the project to read the copy-trade report
without relaying it by hand. Only the Mac can ssh to the VM, so this runs the
VM's read-only `--report` and pushes the output to a GitHub repo.

THE REPORT IS REAL-MONEY TRADING ACTIVITY, AND apex-capital IS PUBLIC (checked
2026-11-09). So the destination must be a repo that is PRIVATE, and this asks
GitHub on every run rather than trusting a config line written once: a repo
flipped to public later must stop receiving the report that same hour.

    anonymous GET /repos/OWNER/REPO    200 -> public     -> REFUSE
                                       404 -> not public -> publish
                                       else -> cannot tell -> REFUSE

Failing closed costs one stale hour. Failing open cannot be undone: a public
commit stays in forks and caches after it is deleted.

A failed ssh publishes nothing, so the last good report stays up, and its
`generated_at` line shows how old it is. Exit codes: 0 published or unchanged,
1 refused or failed, 2 not configured.

Setup (Chairman): create a private repo, then put its ssh URL in the gitignored
data/kalshi_report_publish.json as {"remote": "git@github.com:OWNER/REPO.git"}.
Optional "mirror_dir": a local data/ folder that also receives
kalshi_pnl_history.json, so the Mac's dashboard can show the VM-only desk.

Since 2026-11-11 each push also carries the desk's daily P&L history
(kalshi_pnl_history.json + .csv), read from the VM's recorder output.

Since 2026-11-12 it also carries every pod's daily P&L (pod_pnl_history.json,
pod_pnl_history.csv, firm_pnl_history.csv), and it can run ON THE VM, so the
report no longer stops whenever the Mac sleeps. VM config:
    {"remote": "git@github.com:OWNER/REPO.git", "source": "local",
     "ssh_key": "/home/jackbeard/.ssh/apex_reports_deploy"}
"source": "local" runs the VM commands directly instead of over ssh;
"ssh_key" is a deploy key with write access to that one repo (the VM's other
key belongs to apex-capital, and GitHub ties a deploy key to a single repo).

Usage:
    python3 kalshi_report_publish.py            # fetch + check + push
    python3 kalshi_report_publish.py --dry-run  # fetch + check, print, push nothing
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "data" / "kalshi_report_publish.json"
REPORT_NAME = "kalshi_copytrade_report.txt"
REMOTE_COMMAND = ("cd ~/institutional-tracker && "
                  "~/venv/bin/python3 kalshi_copytrade_manager.py --report")
# The daily P&L history (kalshi_pnl_history.py, recorded by VM cron). Read-only:
# --json prints the file the VM's recorder wrote, and never writes it.
HISTORY_NAME = "kalshi_pnl_history.json"
HISTORY_CSV = "kalshi_pnl_history.csv"
HISTORY_COMMAND = ("cd ~/institutional-tracker && "
                   "~/venv/bin/python3 kalshi_pnl_history.py --json")
# Every pod's daily P&L (pod_pnl_history.py), published beside the Kalshi desk
# so apex-reports carries the whole firm, not one desk of it.
POD_HISTORY_NAME = "pod_pnl_history.json"
POD_HISTORY_CSV = "pod_pnl_history.csv"
FIRM_HISTORY_CSV = "firm_pnl_history.csv"
POD_HISTORY_COMMAND = ("cd ~/institutional-tracker && "
                       "~/venv/bin/python3 pod_pnl_history.py --json")
CSV_COLS = ("date", "orders_filled", "orders_errored", "orders_paper", "spent_usd",
            "realised_usd", "cum_spent_usd", "cum_realised_usd", "marked",
            "unrealised_usd", "total_usd", "balance_total_usd")
_REMOTE_RE = re.compile(r"^(?:git@github\.com:|https://github\.com/)"
                        r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def _run(args, timeout=180, cwd=None, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          env=env)


def parse_remote(remote):
    """(owner, repo) for a github.com URL, else None."""
    m = _REMOTE_RE.match(str(remote or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _http_status(url):
    """Anonymous HTTP status via curl (the Mac's python3 fails TLS verification
    against api.github.com; curl uses the system trust store). 0 = no answer."""
    r = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20",
              "-H", "User-Agent: apex-kalshi-report", url], timeout=30)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def visibility_verdict(remote, status_of=_http_status):
    """(ok_to_publish, why). Only a 404 to an anonymous caller permits it."""
    parsed = parse_remote(remote)
    if parsed is None:
        return False, f"not a github.com repo URL: {remote!r}"
    code = status_of(f"https://api.github.com/repos/{parsed[0]}/{parsed[1]}")
    if code == 404:
        return True, "private (anonymous lookup 404)"
    if code == 200:
        return False, f"{parsed[0]}/{parsed[1]} is PUBLIC"
    return False, f"cannot tell if {parsed[0]}/{parsed[1]} is private (HTTP {code})"


def _vm(command, local=False):
    """argv that runs `command` on the VM: over ssh from the Mac, or directly
    when this copy IS on the VM (config "source": "local"). The VM runs it
    hourly so the report no longer stops whenever the Mac sleeps."""
    if local:
        return ["bash", "-c", command]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "apex-capital",
            command]


def fetch_report(run=_run, local=False):
    """The report text. Raises on failure: an error must never be published
    over the last good report."""
    r = run(_vm(REMOTE_COMMAND, local))
    if r.returncode != 0:
        raise RuntimeError(f"ssh exit {r.returncode}: {r.stderr.strip()[:200]}")
    if "KALSHI COPY-TRADE MANAGER" not in r.stdout:
        raise RuntimeError("output is not the copy-trade report")
    return r.stdout


def fetch_history(run=_run, local=False, command=HISTORY_COMMAND, sector="kalshi"):
    """A VM P&L history as a dict, or None. A failure here never blocks the
    text report: the histories are the optional half of the push."""
    try:
        r = run(_vm(command, local))
        if r.returncode != 0:
            return None
        doc = json.loads(r.stdout)
        return doc if isinstance(doc, dict) and doc.get("sector") == sector \
            and isinstance(doc.get("snapshots"), list) else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def fetch_pod_history(run=_run, local=False):
    return fetch_history(run, local, POD_HISTORY_COMMAND, "pods")


def history_csv(doc):
    """One line per day; an unmeasured value is an empty cell, never 0."""
    def cell(v):
        return "" if v is None else str(v)
    lines = [",".join(CSV_COLS)]
    for r in doc.get("snapshots") or []:
        lines.append(",".join(cell(r.get(c)) for c in CSV_COLS))
    return "\n".join(lines) + "\n"


def history_files(doc, pods=None):
    """{filename: text} to publish alongside the report; a history that could
    not be read contributes nothing, so its last good copy stays up."""
    out = {}
    if doc:
        out[HISTORY_NAME] = json.dumps(doc, indent=1, default=str) + "\n"
        out[HISTORY_CSV] = history_csv(doc)
    if pods:
        out[POD_HISTORY_NAME] = json.dumps(pods, indent=1, default=str) + "\n"
        try:
            import pod_pnl_history as ph
        except ImportError:
            # The Mac runs a lone copy of this file; the JSON still goes up.
            return out
        out[POD_HISTORY_CSV] = ph.history_csv(pods)
        out[FIRM_HISTORY_CSV] = ph.firm_csv(pods)
    return out


def _volatile(name, text):
    """Text with the per-run stamps removed, so an unchanged book is not a new
    commit every hour."""
    if name == REPORT_NAME:
        return _strip_stamp(text)
    if name in (HISTORY_NAME, POD_HISTORY_NAME):
        try:
            d = json.loads(text)
            for k in ("generated_at", "last_written"):
                d.pop(k, None)
            for r in d.get("snapshots") or []:
                r.pop("recorded_at", None)
            return json.dumps(d, sort_keys=True, default=str)
        except ValueError:
            return text
    return text


def render(report, now):
    return f"generated_at: {now.isoformat(timespec='seconds')}\n\n{report}"


def _strip_stamp(text):
    return text.split("\n\n", 1)[-1]


def _git_env(ssh_key=None):
    """The VM already has a key, but it is apex-capital's deploy key and
    GitHub ties a deploy key to one repo, so apex-reports gets its own."""
    if not ssh_key:
        return None
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (f"ssh -i {shlex.quote(str(ssh_key))} -o IdentitiesOnly=yes "
                              "-o BatchMode=yes")
    return env


def push(remote, text, extra=None, ssh_key=None):
    """Commit `text` (plus any `extra` {filename: text}) to a throwaway clone
    and push. False if nothing changed. A fresh clone, so the Mac's checkout and
    its pre-push gate are never touched."""
    files = {REPORT_NAME: text, **(extra or {})}
    tmp = tempfile.mkdtemp(prefix="kalshi-report-")
    try:
        env = _git_env(ssh_key)
        r = _run(["git", "clone", "--quiet", "--depth", "1", remote, tmp], env=env)
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr.strip()[:200]}")
        changed = []
        for name, body in files.items():
            path = Path(tmp) / name
            if path.exists() and _volatile(name, path.read_text()) == _volatile(name, body):
                continue
            path.write_text(body)
            changed.append(name)
        if not changed:
            return False
        for args in (["git", "add", *changed],
                     ["git", "-c", "user.name=Apex Kalshi Report",
                      "-c", "user.email=noreply@localhost", "commit", "-qm",
                      "kalshi copy-trade report"],
                     ["git", "push", "--quiet", "origin", "HEAD"]):
            r = _run(args, cwd=tmp, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"{args[1]} failed: {r.stderr.strip()[:200]}")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        remote = cfg.get("remote") if isinstance(cfg, dict) else None
    except (OSError, ValueError):
        remote = None
    if not remote:
        print(f"{stamp} NOT CONFIGURED: no remote in {CONFIG_FILE.name}; nothing published")
        return 2
    ok, why = visibility_verdict(remote)
    if not ok:
        print(f"{stamp} REFUSED: {why}; nothing published")
        return 1
    try:
        local = isinstance(cfg, dict) and cfg.get("source") == "local"
        text = render(fetch_report(local=local), datetime.now(timezone.utc))
        extra = history_files(fetch_history(local=local), fetch_pod_history(local=local))
        if "--dry-run" in argv:
            print(text)
            for name, body in extra.items():
                print(f"--- {name}: {len(body)} bytes")
            return 0
        mirror = cfg.get("mirror_dir") if isinstance(cfg, dict) else None
        if mirror and Path(mirror).is_dir() and not local:
            # The Mac dashboard reads its own data/ (no state sync between
            # hosts), so the VM's history is copied there for it to show.
            # Never on the VM: there the mirror would overwrite the original.
            for name in (HISTORY_NAME, POD_HISTORY_NAME):
                if name in extra:
                    (Path(mirror) / name).write_text(extra[name])
        hist = ("with " + ", ".join(n for n in (HISTORY_NAME, POD_HISTORY_NAME) if n in extra)
                if any(n in extra for n in (HISTORY_NAME, POD_HISTORY_NAME))
                else "no history (VM file missing or unreadable)")
        key = cfg.get("ssh_key") if isinstance(cfg, dict) else None
        print(f"{stamp} {'published' if push(remote, text, extra, key) else 'unchanged'} "
              f"({why}; {hist})")
        return 0
    except Exception as e:
        print(f"{stamp} FAILED: {type(e).__name__}: {e}; nothing published")
        return 1


if __name__ == "__main__":
    sys.exit(main())
