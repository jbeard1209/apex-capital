#!/usr/bin/env python3
"""
APEX CAPITAL — SYNC DRIFT CHECK  (Mac launchd, hourly)
======================================================
Three copies of the code exist: the Mac checkout, GitHub `origin/main`, and the
files the VM is actually running. Nothing kept them equal except a session
remembering to push, open a PR, merge, and deploy — and sessions kept handing
those steps to Jack instead. This check measures all three and says, per pair,
whether they agree.

    MAC  vs MAIN   the Mac working tree against origin/main, tracked files only,
                   data/ and .env excluded. Split into
                     mac_uncommitted  tracked code edited in the Mac tree, not committed
                     mac_unpushed     commits on a local branch that no remote has
                     mac_behind       the Mac checkout is an older commit of main
                                      (healable: fast-forward, see --heal)
                     mac_off_main     the checkout is on a pushed branch that main
                                      does not contain (an open PR) — info only
    MAIN vs VM     md5 of every origin/main file under institutional-tracker/
                   (data/ excluded) against `md5sum` of the same paths on the VM,
                   plus docs/INFRASTRUCTURE.md, which deploy.sh copies there.
                     vm_differs       a file the VM runs differs from main
                     vm_missing       a file in main is absent on the VM

Each issue must PERSIST past a grace period before it alerts, because a session
mid-task legitimately has uncommitted edits and a fresh merge legitimately waits
for Jack's deploy OK. The first time an issue is seen is kept in a state file;
an issue that disappears resets.

When an issue is past grace it goes to the existing alert path: one
`chairman_queue.submit()` entry per issue kind (dedup'd, so it is raised once)
and one `send_alert_email()` (not forced, so it lands in the daily digest — the
Chairman's one-digest rule). When the comparison that produced an issue runs
again and the issue is gone, the queue entry is resolved. An issue whose
comparison could NOT run (no network, VM unreachable) is never resolved on that
silence: it reports CANNOT_TELL for that half instead.

--publish pushes NEEDS_JACK.md (docs/NEEDS_JACK.md plus a generated sync
section) and sync_status.json to the private apex-reports repo, so the single
"needs Jack" list is readable from anywhere, the same way the Kalshi report is.

Runs on /usr/bin/python3 (3.9). The Mac's shell python3 is 3.15 with no
packages; keep this file 3.9-clean (no `X | Y` types).

Usage:
    /usr/bin/python3 tools/sync_drift_check.py              # check, print, alert
    /usr/bin/python3 tools/sync_drift_check.py --heal       # + fast-forward the Mac if safe
    /usr/bin/python3 tools/sync_drift_check.py --publish    # + push NEEDS_JACK to apex-reports
    /usr/bin/python3 tools/sync_drift_check.py --no-alert --json   # read-only probe
Exit: 0 in sync, 1 drift, 2 cannot tell.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path(os.environ.get("APEX_MAC_ROOT", str(HERE.parent)))
VM_ALIAS = "apex-capital"
VM_DIR = "~/institutional-tracker"
TRACKER = "institutional-tracker/"
SOURCE = "sync_drift_check"
REPORTS_REMOTE = "git@github.com:jbeard1209/apex-reports.git"

# Paths that are not code, or that are deliberately per-host.
EXCLUDE_PREFIXES = ("institutional-tracker/data/", "institutional-tracker/logs/")
# Edited by sessions as a scratch list; it is published live from the Mac tree,
# so an uncommitted append is the normal state, not drift.
NOT_DRIFT = {"docs/NEEDS_JACK.md"}
# Files deploy.sh puts on the VM from OUTSIDE institutional-tracker/.
EXTRA_DEPLOYED = {"docs/INFRASTRUCTURE.md": "INFRASTRUCTURE.md"}

HOUR = 3600.0
GRACE_S = {
    "mac_uncommitted": 24 * HOUR,  # a session's work in progress
    "mac_unpushed": 2 * HOUR,
    "mac_behind": 2 * HOUR,        # --heal normally clears this first
    "vm_differs": 6 * HOUR,        # merged, waiting for Jack's deploy OK
    "vm_missing": 6 * HOUR,
}
INFO_ONLY = {"mac_off_main"}

WHAT_TO_DO = {
    "mac_uncommitted": "A session left code edited in the Mac checkout. Ask a session to "
                       "ship it (tools/ship.sh) or throw it away.",
    "mac_unpushed": "Commits on a Mac branch never reached GitHub. Ask a session to ship "
                    "the branch (tools/ship.sh) or delete it.",
    "mac_behind": "The Mac checkout is an older commit of main and could not be "
                  "fast-forwarded automatically (see reason). A session can run "
                  "tools/sync_drift_check.py --heal once the blocker is gone.",
    "vm_differs": "Merged code has not been deployed: the VM is running different "
                  "files. Tell a session 'deploy' (tools/ship.sh --deploy-only).",
    "vm_missing": "Files in main are absent on the VM. Tell a session 'deploy' "
                  "(tools/ship.sh --deploy-only).",
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def run(args, cwd=None, timeout=120, stdin=None):
    return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True,
                          timeout=timeout)


def git(root, *args, **kw):
    return run(["git", "-C", str(root), *args], **kw)


def in_scope(path):
    if path in NOT_DRIFT or path.startswith(EXCLUDE_PREFIXES):
        return False
    base = path.rsplit("/", 1)[-1]
    if base == ".env" or base.startswith(".env.") or "__pycache__/" in path or path.endswith(".pyc"):
        return False
    return True


def _lines(text):
    return [l for l in (text or "").splitlines() if l.strip()]


# ── MAC vs MAIN ──────────────────────────────────────────────────────────────
def compare_mac(root, ref="origin/main"):
    """Return (result_dict, error_or_None)."""
    out = {"ref": ref}
    head = git(root, "rev-parse", "HEAD")
    main = git(root, "rev-parse", ref)
    if head.returncode or main.returncode:
        return out, "git rev-parse failed: " + (head.stderr or main.stderr).strip()[:200]
    out["head"] = head.stdout.strip()[:12]
    out["main"] = main.stdout.strip()[:12]
    br = git(root, "branch", "--show-current")
    out["branch"] = br.stdout.strip() or "(detached)"

    unc = git(root, "diff", "--name-only", "HEAD", "--", ".")
    if unc.returncode:
        return out, "git diff HEAD failed: " + unc.stderr.strip()[:200]
    out["uncommitted"] = sorted(p for p in _lines(unc.stdout) if in_scope(p))

    committed = git(root, "diff", "--name-only", "HEAD", ref, "--", ".")
    out["head_vs_main"] = sorted(p for p in _lines(committed.stdout) if in_scope(p))

    unp = git(root, "log", "--branches", "--not", "--remotes",
              "--format=%h|%cI|%D|%s")
    commits = []
    for l in _lines(unp.stdout):
        parts = l.split("|", 3)
        if len(parts) == 4:
            commits.append({"sha": parts[0], "date": parts[1], "refs": parts[2],
                            "subject": parts[3][:100]})
    out["unpushed"] = commits

    anc = git(root, "merge-base", "--is-ancestor", "HEAD", ref)
    out["head_is_ancestor_of_main"] = (anc.returncode == 0)
    return out, None


def mac_issues(mac):
    issues = {}
    if mac.get("uncommitted"):
        issues["mac_uncommitted"] = mac["uncommitted"]
    if mac.get("unpushed"):
        issues["mac_unpushed"] = ["%s %s (%s)" % (c["sha"], c["subject"], c["refs"] or "no ref")
                                  for c in mac["unpushed"]]
    if mac.get("head_vs_main"):
        if mac.get("head_is_ancestor_of_main"):
            issues["mac_behind"] = mac["head_vs_main"]
        elif not mac.get("unpushed"):
            issues["mac_off_main"] = ["checkout on %s @ %s, not contained in main"
                                      % (mac.get("branch"), mac.get("head"))]
    return issues


def heal_mac(root, mac, ref="origin/main"):
    """Fast-forward the Mac checkout to main when that cannot lose anything.

    Only when: HEAD is an ancestor of main (nothing of ours is dropped), and no
    tracked CODE file is edited (a session may be mid-change; data/ runtime
    files are always dirty and git refuses the checkout itself if a data file
    it needs to change is dirty, which is the safe outcome). Returns
    (healed, reason)."""
    if not mac.get("head_vs_main"):
        return False, "already at main"
    if not mac.get("head_is_ancestor_of_main"):
        return False, "HEAD is not an ancestor of main (unmerged commits on %s)" % mac.get("branch")
    if mac.get("uncommitted"):
        return False, "%d code file(s) edited in the Mac tree: %s" % (
            len(mac["uncommitted"]), ", ".join(mac["uncommitted"][:5]))
    kept = []
    if mac.get("branch") != "main":
        r, k = _keeping_host_data(root, "checkout", "-q", "main")
        kept += k
        if r.returncode:
            return False, "git checkout main refused: " + r.stderr.strip()[:300]
    r, k = _keeping_host_data(root, "merge", "-q", "--ff-only", ref)
    kept += k
    if r.returncode:
        return False, "git merge --ff-only refused: " + r.stderr.strip()[:300]
    return True, "fast-forwarded to %s%s" % (
        mac.get("main"), " (kept this host's %s)" % ", ".join(sorted(set(kept))) if kept else "")


def _blocking_paths(stderr):
    """Paths git lists under 'would be overwritten by checkout/merge:'."""
    out, on = [], False
    for l in (stderr or "").splitlines():
        if "would be overwritten by" in l:
            on = True
        elif on and l.startswith("\t"):
            out.append(l.strip())
        elif on:
            on = False
    return out


def _keeping_host_data(root, *args):
    """Run a checkout/fast-forward that git refuses ONLY because a tracked data/
    or logs/ runtime file is dirty here: keep this host's bytes, let git move,
    then put them back. Until 2026-11-12 any such file (mac_launchd_snapshot.txt,
    vm_crontab_snapshot.txt are rewritten on the Mac and also committed) refused
    the heal, so the Mac sat behind main until a session did this by hand. The
    host's copy wins because data/ is per-host by design (not drift, not
    deployed). A dirty CODE file is never touched: heal_mac refuses before this,
    and a blocking list with anything outside data/ or logs/ is returned as-is.
    Returns (CompletedProcess, [kept paths])."""
    r = git(root, *args)
    if not r.returncode:
        return r, []
    paths = _blocking_paths(r.stderr)
    if not paths or not all(p.startswith(EXCLUDE_PREFIXES) and not
                            p.rsplit("/", 1)[-1].startswith(".env") for p in paths):
        return r, []
    saved = {p: (Path(root) / p).read_bytes() for p in paths}
    try:
        reset = git(root, "checkout", "-q", "--", *paths)
        if reset.returncode:
            return reset, []
        r = git(root, *args)
    finally:
        for p, b in saved.items():
            (Path(root) / p).parent.mkdir(parents=True, exist_ok=True)
            (Path(root) / p).write_bytes(b)
    return r, (paths if not r.returncode else [])


# ── MAIN vs VM ───────────────────────────────────────────────────────────────
def main_deployed_md5(root, ref="origin/main"):
    """{vm_relative_path: md5} for every file main says the VM should run."""
    ls = git(root, "ls-tree", "-r", "--format=%(objectname) %(path)", ref)
    if ls.returncode:  # git < 2.36 has no --format; fall back to the plain listing
        ls = git(root, "ls-tree", "-r", ref)
        rows = []
        for l in _lines(ls.stdout):
            meta, _, path = l.partition("\t")
            rows.append((meta.split()[2], path))
    else:
        rows = [tuple(l.split(" ", 1)) for l in _lines(ls.stdout)]
    want = {}
    for sha, path in rows:
        if path.startswith(TRACKER) and in_scope(path):
            want[path[len(TRACKER):]] = sha
        elif path in EXTRA_DEPLOYED:
            want[EXTRA_DEPLOYED[path]] = sha
    if not want:
        return {}
    shas = "\n".join(want.values()) + "\n"
    cat = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                         input=shas.encode(), capture_output=True, timeout=120)
    buf, i, blobs = cat.stdout, 0, {}
    while i < len(buf):
        nl = buf.index(b"\n", i)
        header = buf[i:nl].decode().split()
        size = int(header[2])
        blobs[header[0]] = hashlib.md5(buf[nl + 1: nl + 1 + size]).hexdigest()
        i = nl + 1 + size + 1
    return {p: blobs[s] for p, s in want.items() if s in blobs}


def parse_md5sum(output):
    """{path: md5} and [missing paths] from GNU md5sum output (stderr merged)."""
    got, missing = {}, []
    for line in (output or "").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("md5sum: ") and (": No such file" in line or "cannot open" in line):
            path = line[len("md5sum: "):].rsplit(": ", 1)[0]
            missing.append(path)
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 32:
            got[parts[1].lstrip("*")] = parts[0]
    return got, missing


def compare_vm(want, vm_local_dir=None, runner=None):
    """Return (result_dict, error_or_None). `vm_local_dir` compares against a
    local directory instead of ssh (used by the gate's deliberate-divergence
    probe and by tests)."""
    if not want:
        return {}, "no deployable files found in main — refusing to call an empty set in sync"
    stdin = "\0".join(sorted(want)) + "\0"
    if vm_local_dir:
        args = ["bash", "-c", "cd %s && xargs -0 md5sum 2>&1; true" % _sh(str(vm_local_dir))]
    else:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", VM_ALIAS,
                "cd %s 2>/dev/null || exit 9; xargs -0 md5sum 2>&1; true" % VM_DIR]
    try:
        r = (runner or run)(args, timeout=180, stdin=stdin)
    except Exception as e:  # timeout, no ssh
        return {}, "cannot reach the VM (%s: %s)" % (type(e).__name__, e)
    if r.returncode == 9:
        return {}, "%s does not exist on the VM" % VM_DIR
    got, missing = parse_md5sum(r.stdout)
    if not got and not missing:
        return {}, "VM returned nothing (%s)" % (r.stderr or "").strip()[:200]
    differs = sorted(p for p, h in want.items() if p in got and got[p] != h)
    absent = sorted(set(missing) | (set(want) - set(got) - set(missing)))
    return {"n_examined": len(want), "differs": differs, "missing": absent}, None


def vm_issues(vm):
    issues = {}
    if vm.get("differs"):
        issues["vm_differs"] = vm["differs"]
    if vm.get("missing"):
        issues["vm_missing"] = vm["missing"]
    return issues


def _sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


# ── persistence / grace ─────────────────────────────────────────────────────
def update_state(state, issues, checked_kinds, now):
    """Carry each issue's first_seen forward; forget issues whose comparison
    ran and found nothing. Kinds whose comparison did not run keep their state."""
    new = {}
    for kind, prev in (state or {}).items():
        if kind not in checked_kinds and kind not in issues:
            new[kind] = prev
    for kind, items in issues.items():
        first = (state or {}).get(kind, {}).get("first_seen") or _iso(now)
        new[kind] = {"first_seen": first, "n": len(items)}
    return new


def due(kind, state, now):
    if kind in INFO_ONLY or kind not in state:
        return False
    first = datetime.fromisoformat(state[kind]["first_seen"])
    return (now - first).total_seconds() >= GRACE_S.get(kind, 6 * HOUR)


KIND_OF_CHECK = {
    "mac": ("mac_uncommitted", "mac_unpushed", "mac_behind", "mac_off_main"),
    "vm": ("vm_differs", "vm_missing"),
}


def build_report(root, heal=False, vm_local_dir=None, ref="origin/main", fetch=True,
                 state=None, now=None):
    now = now or _now()
    report = {"generated_at": _iso(now), "root": str(root), "ref": ref, "errors": {}}
    if fetch:
        f = git(root, "fetch", "-q", "origin", "main", timeout=90)
        if f.returncode:
            report["errors"]["fetch"] = "git fetch failed (comparing against the last " \
                                        "fetched main): " + f.stderr.strip()[:200]
    issues, checked = {}, set()

    mac, err = compare_mac(root, ref)
    if err:
        report["errors"]["mac"] = err
    else:
        if heal:
            healed, why = heal_mac(root, mac, ref)
            report["heal"] = {"healed": healed, "reason": why}
            if healed:
                mac, err = compare_mac(root, ref)
        if not err:
            checked.update(KIND_OF_CHECK["mac"])
            issues.update(mac_issues(mac))
    report["mac"] = mac

    vm, err = compare_vm(main_deployed_md5(root, ref), vm_local_dir=vm_local_dir)
    if err:
        report["errors"]["vm"] = err
    else:
        checked.update(KIND_OF_CHECK["vm"])
        issues.update(vm_issues(vm))
    report["vm"] = vm

    new_state = update_state(state or {}, issues, checked, now)
    report["issues"] = []
    for kind in sorted(issues):
        first = new_state[kind]["first_seen"]
        age = (now - datetime.fromisoformat(first)).total_seconds() / HOUR
        entry = {"kind": kind, "n": len(issues[kind]), "items": issues[kind][:25],
                 "first_seen": first, "age_h": round(age, 1),
                 "grace_h": GRACE_S.get(kind, 0) / HOUR, "due": due(kind, new_state, now),
                 "info_only": kind in INFO_ONLY}
        if kind == "mac_behind" and report.get("heal"):
            entry["heal_reason"] = report["heal"]["reason"]
        report["issues"].append(entry)
    report["checked"] = sorted(checked)
    real = [i for i in report["issues"] if not i["info_only"]]
    if real:
        report["verdict"] = "DRIFT"
    elif report["errors"].get("mac") or report["errors"].get("vm"):
        report["verdict"] = "CANNOT_TELL"
    else:
        report["verdict"] = "IN_SYNC"
    return report, new_state


# ── alerting (existing Chairman queue + digest email) ──────────────────────
def _queue_entry(issue, now):
    items = "\n".join("  - " + str(x) for x in issue["items"][:15])
    more = "" if issue["n"] <= 15 else "\n  ... and %d more" % (issue["n"] - 15)
    return {
        "id": "sync-drift-%s-%s" % (now.strftime("%Y%m%d"), issue["kind"]),
        "dedup_key": "sync_drift:%s" % issue["kind"],
        "severity": "MEDIUM",
        "category": "INFRASTRUCTURE",
        "status": "awaiting_chairman",
        "raised_at": _iso(now),
        "description": ("Mac / GitHub / VM out of sync: %s (%d item(s), for %.0fh)\n\n"
                        "WHAT TO DO: %s\n\n%s%s\n\nRaised by tools/sync_drift_check.py; it "
                        "resolves itself once the check sees this cleared."
                        % (issue["kind"], issue["n"], issue["age_h"],
                           WHAT_TO_DO.get(issue["kind"], ""), items, more)),
    }


def alert(report, root, now=None, submit=None, resolve=None, email=None):
    """Raise due issues, resolve cleared ones. Returns a summary dict."""
    now = now or _now()
    tracker = Path(root) / "institutional-tracker"
    qpath = str(tracker / "data" / "chairman_action_required.json")
    if submit is None or resolve is None or email is None:
        sys.path.insert(0, str(tracker))
        if submit is None or resolve is None:
            import chairman_queue
            submit = submit or chairman_queue.submit
            resolve = resolve or chairman_queue.resolve
    raised, resolved = [], []
    for issue in report["issues"]:
        if not issue["due"]:
            continue
        added, _ = submit([_queue_entry(issue, now)], SOURCE, path=qpath)
        if added:
            raised.append(issue["kind"])
    present = {i["kind"] for i in report["issues"] if i["due"]}
    cleared = [k for k in report.get("checked", []) if k not in present and k not in INFO_ONLY]
    # A kind still present but not yet due stays open too: an issue that came
    # back inside its grace period is not a fix.
    still = {i["kind"] for i in report["issues"]}
    cleared = [k for k in cleared if k not in still]
    if cleared:
        done = resolve(["sync_drift:%s" % k for k in cleared], SOURCE, path=qpath,
                       note="sync_drift_check: comparison ran and the drift is gone")
        resolved = [e.get("dedup_key") for e in (done or [])]
    email_status = None
    if raised:
        body = "<h3>Mac / GitHub / VM drift</h3>" + "".join(
            "<p><b>%s</b> (%d): %s</p>" % (i["kind"], i["n"], WHAT_TO_DO.get(i["kind"], ""))
            for i in report["issues"] if i["kind"] in raised)
        subject = "Sync drift: %s" % ", ".join(raised)
        cwd = os.getcwd()
        try:
            # email_sender prints; keep stdout clean for --json
            with contextlib.redirect_stdout(sys.stderr):
                if email is None:
                    os.chdir(str(tracker))  # config/.env and the digest buffer are relative
                    from email_sender import send_alert_email as email
                email(subject, body)
            email_status = "sent/buffered"
        except Exception as e:
            email_status = "FAILED (queue entry still raised): %s: %s" % (type(e).__name__, e)
            print("  email " + email_status, file=sys.stderr)
        finally:
            os.chdir(cwd)
    return {"raised": raised, "resolved": resolved, "email": email_status}


# ── NEEDS_JACK publish ──────────────────────────────────────────────────────
def render_sync_section(report):
    lines = ["", "## Sync status (generated hourly by tools/sync_drift_check.py)", "",
             "Checked: %s" % report["generated_at"],
             "Verdict: **%s**  (Mac %s on %s, main %s)" % (
                 report["verdict"], report.get("mac", {}).get("head", "?"),
                 report.get("mac", {}).get("branch", "?"), report.get("mac", {}).get("main", "?")),
             ""]
    for k, v in sorted(report.get("errors", {}).items()):
        lines.append("- cannot tell (%s): %s" % (k, v))
    for i in report["issues"]:
        tag = "info" if i["info_only"] else ("NEEDS JACK" if i["due"] else "within grace")
        lines.append("- **%s** (%d, %s, seen %.0fh): %s" % (
            i["kind"], i["n"], tag, i["age_h"], WHAT_TO_DO.get(i["kind"], "")))
        for x in i["items"][:8]:
            lines.append("    - %s" % x)
    if not report["issues"] and not report.get("errors"):
        lines.append("- Mac, GitHub main and the VM agree.")
    return "\n".join(lines) + "\n"


def _stable(name, text):
    if name.endswith(".md"):
        return "\n".join(l for l in text.splitlines() if not l.startswith("Checked:")
                         and " seen " not in l)
    try:
        d = json.loads(text)
        d.pop("generated_at", None)
        for i in d.get("issues", []):
            i.pop("age_h", None)
            i.pop("due", None)
        return json.dumps(d, sort_keys=True)
    except ValueError:
        return text


def publish(report, root, remote=REPORTS_REMOTE):
    tracker = Path(root) / "institutional-tracker"
    sys.path.insert(0, str(tracker))
    from kalshi_report_publish import visibility_verdict
    ok, why = visibility_verdict(remote)
    if not ok:
        return "REFUSED: %s" % why
    nj = Path(root) / "docs" / "NEEDS_JACK.md"
    head = nj.read_text() if nj.exists() else "# Needs Jack\n\n(docs/NEEDS_JACK.md missing)\n"
    files = {"NEEDS_JACK.md": head.rstrip("\n") + "\n" + render_sync_section(report),
             "sync_status.json": json.dumps(report, indent=1, default=str) + "\n"}
    tmp = tempfile.mkdtemp(prefix="needs-jack-")
    try:
        r = run(["git", "clone", "--quiet", "--depth", "1", remote, tmp], timeout=120)
        if r.returncode:
            return "FAILED: clone: %s" % r.stderr.strip()[:200]
        changed = []
        for name, body in files.items():
            p = Path(tmp) / name
            if p.exists() and _stable(name, p.read_text()) == _stable(name, body):
                continue
            p.write_text(body)
            changed.append(name)
        if not changed:
            return "unchanged"
        for args in (["git", "add", *changed],
                     ["git", "-c", "user.name=Apex Sync Check", "-c",
                      "user.email=noreply@localhost", "commit", "-qm", "needs-jack + sync status"]):
            r = run(args, cwd=tmp)
            if r.returncode:
                return "FAILED: %s: %s" % (args[1], r.stderr.strip()[:200])
        for attempt in range(2):  # the VM publisher pushes at :55; rebase once on a race
            r = run(["git", "push", "--quiet", "origin", "HEAD"], cwd=tmp)
            if r.returncode == 0:
                return "published (%s)" % ", ".join(changed)
            run(["git", "pull", "--quiet", "--rebase", "origin", "main"], cwd=tmp)
        return "FAILED: push: %s" % r.stderr.strip()[:200]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--heal", action="store_true", help="fast-forward the Mac checkout when safe")
    ap.add_argument("--publish", action="store_true", help="push NEEDS_JACK.md to apex-reports")
    ap.add_argument("--no-alert", action="store_true", help="do not touch the Chairman queue/email")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--vm-local-dir", help="compare main against this local dir, not the VM")
    ap.add_argument("--state", help="state file (default <root>/institutional-tracker/logs/sync_drift_state.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root)
    logs = root / "institutional-tracker" / "logs"
    state_path = Path(a.state) if a.state else logs / "sync_drift_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    report, new_state = build_report(root, heal=a.heal, vm_local_dir=a.vm_local_dir,
                                     ref=a.ref, fetch=not a.no_fetch, state=state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(new_state, indent=1))
    if not a.state:
        (logs / "sync_drift_report.json").write_text(json.dumps(report, indent=1, default=str))
    if not a.no_alert:
        try:
            report["alert"] = alert(report, root)
        except Exception as e:
            report["alert"] = {"error": "%s: %s" % (type(e).__name__, e)}
    if a.publish:
        try:
            report["publish"] = publish(report, root)
        except Exception as e:
            report["publish"] = "FAILED: %s: %s" % (type(e).__name__, e)
    if a.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print("%s sync: %s" % (report["generated_at"], report["verdict"]))
        print(render_sync_section(report).strip())
        for k in ("heal", "alert", "publish"):
            if k in report:
                print("%s: %s" % (k, report[k]))
    return {"IN_SYNC": 0, "DRIFT": 1}.get(report["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
