#!/usr/bin/env python3
"""
APEX CAPITAL — KALSHI REPORT PUBLISHER (Mac launchd, hourly)
============================================================
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

Usage:
    python3 kalshi_report_publish.py            # fetch + check + push
    python3 kalshi_report_publish.py --dry-run  # fetch + check, print, push nothing
"""

import json
import re
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
_REMOTE_RE = re.compile(r"^(?:git@github\.com:|https://github\.com/)"
                        r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def _run(args, timeout=180, cwd=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)


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


def fetch_report(run=_run):
    """The report text. Raises on failure: an error must never be published
    over the last good report."""
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "apex-capital",
             REMOTE_COMMAND])
    if r.returncode != 0:
        raise RuntimeError(f"ssh exit {r.returncode}: {r.stderr.strip()[:200]}")
    if "KALSHI COPY-TRADE MANAGER" not in r.stdout:
        raise RuntimeError("output is not the copy-trade report")
    return r.stdout


def render(report, now):
    return f"generated_at: {now.isoformat(timespec='seconds')}\n\n{report}"


def _strip_stamp(text):
    return text.split("\n\n", 1)[-1]


def push(remote, text):
    """Commit `text` to a throwaway clone and push. False if nothing changed.
    A fresh clone, so the Mac's checkout and its pre-push gate are never touched."""
    tmp = tempfile.mkdtemp(prefix="kalshi-report-")
    try:
        r = _run(["git", "clone", "--quiet", "--depth", "1", remote, tmp])
        if r.returncode != 0:
            raise RuntimeError(f"clone failed: {r.stderr.strip()[:200]}")
        path = Path(tmp) / REPORT_NAME
        if path.exists() and _strip_stamp(path.read_text()) == _strip_stamp(text):
            return False
        path.write_text(text)
        for args in (["git", "add", REPORT_NAME],
                     ["git", "-c", "user.name=Apex Kalshi Report",
                      "-c", "user.email=noreply@localhost", "commit", "-qm",
                      "kalshi copy-trade report"],
                     ["git", "push", "--quiet", "origin", "HEAD"]):
            r = _run(args, cwd=tmp)
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
        text = render(fetch_report(), datetime.now(timezone.utc))
        if "--dry-run" in argv:
            print(text)
            return 0
        print(f"{stamp} {'published' if push(remote, text) else 'unchanged'} ({why})")
        return 0
    except Exception as e:
        print(f"{stamp} FAILED: {type(e).__name__}: {e}; nothing published")
        return 1


if __name__ == "__main__":
    sys.exit(main())
