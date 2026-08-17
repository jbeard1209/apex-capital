#!/usr/bin/env bash
#
# Restore the VM's full crontab from data/vm_crontab_snapshot.txt.
#
# WHY THIS EXISTS
# ---------------
# vm_bootstrap.sh carries a 21-job seed crontab, written when the firm had 21
# jobs. The firm now runs 84. Nothing reconciled the two, so a rebuild would
# have produced a VM that booted cleanly, reported success, and quietly ran a
# quarter of the schedule — with no error anywhere, because a job that is not
# scheduled cannot fail. The snapshot is the real schedule; this restores it.
#
# The snapshot is refreshed by scripts/refresh_crontab_snapshot.sh and is
# committed, so it survives the VM. Run this ON THE VM, after deploy.sh has
# rsynced the repo across.
#
# Times in the snapshot are UTC — the VM's timezone. Do not run this on a host
# set to America/New_York; every job would move 4-5 hours.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/institutional-tracker}"
SNAPSHOT="$PROJECT_DIR/data/vm_crontab_snapshot.txt"

count_jobs() { grep -v "^#" | grep -v "^$" | grep -v "^SHELL\|^PATH" | wc -l | tr -d " "; }

echo "=============================================="
echo "  RESTORE CRONTAB FROM SNAPSHOT"
echo "=============================================="

if [ ! -f "$SNAPSHOT" ]; then
    echo "  ✗ No snapshot at $SNAPSHOT"
    echo "    Run deploy.sh from the Mac first — the snapshot ships with the repo."
    exit 1
fi

tz=$(date +%Z)
if [ "$tz" != "UTC" ]; then
    echo "  ✗ This host's timezone is $tz, not UTC."
    echo "    The snapshot's times are UTC; installing them here would shift"
    echo "    every job. Fix the host first:  sudo timedatectl set-timezone Etc/UTC"
    exit 1
fi

new=$(count_jobs < "$SNAPSHOT")
old=$(crontab -l 2>/dev/null | count_jobs || echo 0)

if [ "$new" -lt 1 ]; then
    echo "  ✗ Snapshot parses to $new jobs — refusing to install an empty schedule."
    exit 1
fi

echo "  Current: ${old:-0} job(s)"
echo "  Snapshot: $new job(s)"

if [ "${old:-0}" -gt "$new" ] && [ "${FORCE_CRONTAB:-0}" != "1" ]; then
    echo "  ⚠ The snapshot is SMALLER than what this host already runs."
    echo "    That means the snapshot is stale, not that jobs should be deleted."
    echo "    Refresh it from the live host instead:"
    echo "      ssh apex-capital 'crontab -l' > data/vm_crontab_snapshot.txt"
    echo "    To install anyway: FORCE_CRONTAB=1"
    exit 1
fi

backup="$HOME/crontab.before-restore.$(date +%Y%m%d%H%M%S).txt"
crontab -l > "$backup" 2>/dev/null || echo "" > "$backup"
echo "  Backed up current crontab -> $backup"

crontab "$SNAPSHOT"
echo "  ✓ Installed. Crontab now holds $(crontab -l | count_jobs) job(s)."
