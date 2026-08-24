#!/bin/bash
# =============================================================================
# refresh_crontab_snapshot.sh — mirror the VM's LIVE crontab into
#                               data/vm_crontab_snapshot.txt
#
# Why this exists
# ---------------
# wiring_audit.py's orphan-agent check and hr_agent.py's attendance/ratchet
# checks both need to know what is ACTUALLY scheduled in production. They used
# to read new_crontab.txt, a hand-maintained Mac-path template that had drifted
# 10 agents behind prod — so agents running every four hours in production were
# reported as "scheduled nowhere", and the audit produced confident, wrong
# findings. The snapshot replaced it as ground truth.
#
# A mirror that nothing refreshes is just a slower-moving stale document.
# wiring_audit.py:236 and test_pipeline_smoke.py both named this script as the
# thing that keeps it fresh, and the test hard-fails once the snapshot passes
# 14 days — but the script itself was never written. (Same shape as
# build_365d_labels.py: specified, referenced, never built. See the
# `plausible-is-not-validated` memory.)
#
# Where it runs
# -------------
# Either host, auto-detected:
#   * ON the VM      — `crontab -l` IS the live truth, no SSH needed. This is
#                      the scheduled path.
#   * ON the Mac     — SSHes the VM. gcp_setup/deploy.sh calls this before
#                      rsync so the copy it pushes is never staler than the
#                      copy already on the VM.
#
# TWO schedulers, not one
# -----------------------
# This header used to say the Mac's crontab is "intentionally empty (dev
# machine only, no agents on cron)". That is true of CRON and false of the
# MAC: the Mac schedules through launchd, and has done since the catch-up and
# vm-state jobs moved there — catch_up.py every 1200s and vm_state_monitor.py
# every 900s, neither of which appears in any crontab anywhere.
#
# So the schedule ground truth was one host's half of the firm, and every
# consumer named at the top treated it as the whole. Measured 2026-08-24, the
# blast radius is ZERO modules: both launchd targets are already reachable from
# the VM's cron roots, so no agent is currently mis-accused. That is the reason
# to fix it now rather than a reason not to — the hole costs nothing until
# someone schedules something on the Mac that runs nowhere else, and on that
# day it produces a confident "scheduled NOWHERE" about a job running every
# fifteen minutes (memory: `auditor_ground_truth`).
#
# The Mac capture is deliberately NOT fatal when it is unavailable, unlike the
# crontab. The two failures are not symmetric: with no crontab the walk knows
# only main.py and would brand the entire firm orphaned, whereas missing
# launchd roots can only ever cause a false ACCUSATION of a handful of jobs.
# A note is the proportionate response; wedging the whole audit is not. On the
# VM there is no launchd to read, so the existing Mac snapshot is left exactly
# as it is — writing an empty one there would erase the Mac's schedule from
# the VM's copy on every deploy.
#
# Fail-closed
# -----------
# A truncated or empty capture is far worse than a stale one: every agent would
# look unscheduled and the orphan ratchet would fire on the whole firm. So the
# capture goes to a temp file, is sanity-checked for a plausible job count, and
# only then replaces the real snapshot. Any failure leaves the existing file
# untouched and exits non-zero so cron surfaces it.
# =============================================================================

set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT="$BASE_DIR/data/vm_crontab_snapshot.txt"
VM_HOSTNAME="apex-capital-configuration"
SSH_ALIAS="apex-capital"

# The VM had 67 job lines when this was written. Demand a floor well under that
# but far above zero: enough that a truncated read cannot pass, loose enough
# that deliberately retiring a batch of jobs doesn't wedge the refresher.
MIN_JOB_LINES=20

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if [ "$(hostname)" = "$VM_HOSTNAME" ]; then
    SOURCE_DESC="local crontab on $VM_HOSTNAME"
    CAPTURE_CMD="crontab -l"
    crontab -l > "$tmp" 2>/dev/null
else
    SOURCE_DESC="$SSH_ALIAS via ssh"
    CAPTURE_CMD="ssh $SSH_ALIAS 'crontab -l'"
    ssh -o BatchMode=yes -o ConnectTimeout=20 "$SSH_ALIAS" 'crontab -l' > "$tmp" 2>/dev/null
fi
rc=$?

if [ $rc -ne 0 ]; then
    echo "refresh_crontab_snapshot: capture FAILED (exit $rc) from $SOURCE_DESC." >&2
    echo "  Existing snapshot left untouched — a stale mirror beats an empty one." >&2
    exit 1
fi

# Job lines = non-blank, non-comment, and not a bare VAR=value assignment.
job_lines=$(grep -vE '^\s*(#|$)' "$tmp" | grep -vE '^\s*[A-Za-z_][A-Za-z0-9_]*=' | wc -l | tr -d ' ')

if [ "$job_lines" -lt "$MIN_JOB_LINES" ]; then
    echo "refresh_crontab_snapshot: capture looks truncated — only $job_lines job line(s)," >&2
    echo "  floor is $MIN_JOB_LINES. Refusing to overwrite the snapshot with this." >&2
    echo "  If prod really did shrink this far, lower MIN_JOB_LINES deliberately." >&2
    exit 1
fi

{
    echo "# CANONICAL PROD SCHEDULE — verbatim snapshot of the GCP VM's live crontab."
    echo "# Captured $(date -u +%Y-%m-%dT%H:%M:%SZ) from $SOURCE_DESC via: $CAPTURE_CMD"
    echo "# DO NOT hand-edit — it is a mirror, not a control surface."
    echo "# Refresh with: bash scripts/refresh_crontab_snapshot.sh"
    echo ""
    cat "$tmp"
} > "$SNAPSHOT"

echo "refresh_crontab_snapshot: $job_lines job line(s) mirrored -> data/vm_crontab_snapshot.txt"

# ── the Mac's other scheduler ────────────────────────────────────────────────
# launchd, not cron. Best-effort by design (see header): a failure here leaves
# the existing snapshot untouched and does not fail the script, because a
# missing Mac schedule can only over-accuse a couple of jobs while a missing
# crontab would condemn the firm.
MAC_SNAPSHOT="$BASE_DIR/data/mac_launchd_snapshot.txt"

if command -v launchctl >/dev/null 2>&1 && [ -d "$HOME/Library/LaunchAgents" ]; then
    mtmp="$(mktemp)"
    {
        echo "# MAC SCHEDULE — the firm's SECOND scheduler. launchd, not cron."
        echo "# Captured $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
        echo "# DO NOT hand-edit — it is a mirror, not a control surface."
        echo "# Refresh with: bash scripts/refresh_crontab_snapshot.sh"
        echo ""
        for plist in "$HOME"/Library/LaunchAgents/com.apexcapital.*.plist; do
            [ -e "$plist" ] || continue
            label="$(basename "$plist" .plist)"
            # The script BASENAME is what wiring_audit matches module names
            # against; the interval is what a cadence check would need. Reduce
            # to the basename with sed rather than `basename`/`xargs`: this
            # repo's own path contains spaces ("Claude investment strategy"),
            # and xargs would split it into three nonexistent arguments.
            args="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments' "$plist" 2>/dev/null \
                    | sed -e 's#^[[:space:]]*##' -e 's#[[:space:]]*$##' -e 's#^.*/##' \
                    | grep -E '^[A-Za-z_][A-Za-z0-9_]*\.py$' | tr '\n' ' ')"
            iv="$(/usr/libexec/PlistBuddy -c 'Print :StartInterval' "$plist" 2>/dev/null)"
            echo "launchd ${label} StartInterval=${iv:-none} ${args}"
        done
    } > "$mtmp" 2>/dev/null

    if grep -qE '^launchd ' "$mtmp"; then
        mv "$mtmp" "$MAC_SNAPSHOT"
        echo "refresh_crontab_snapshot: $(grep -cE '^launchd ' "$MAC_SNAPSHOT") launchd job(s)" \
             "mirrored -> data/mac_launchd_snapshot.txt"
    else
        rm -f "$mtmp"
        echo "refresh_crontab_snapshot: no com.apexcapital.* launchd jobs found —" \
             "existing Mac snapshot left untouched." >&2
    fi
else
    echo "refresh_crontab_snapshot: no launchd on this host (expected on the VM) —" \
         "Mac schedule snapshot left as-is." >&2
fi
