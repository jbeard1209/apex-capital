#!/bin/bash
# =============================================================================
# APEX CAPITAL — GCP DEPLOY SCRIPT
# Run this from your Mac after vm_bootstrap.sh has finished on the VM.
# It rsyncs all project files and your .env credentials to the GCP instance.
#
# Usage:
#   bash gcp_setup/deploy.sh [VM_EXTERNAL_IP]
#
#   If no IP is passed, uses the SSH alias "apex-capital" from ~/.ssh/config.
#   When the GCP instance IP changes, run:
#     bash gcp_setup/update_ip.sh <NEW_IP>
#   That updates ~/.ssh/config and you're done — no other changes needed.
#
# Example (first deploy or after IP change):
#   bash gcp_setup/deploy.sh 34.85.151.111
#
# Example (routine redeploy, IP unchanged):
#   bash gcp_setup/deploy.sh
#
# ── STATIC IP (recommended) ──────────────────────────────────────────────────
# To lock the GCP instance to a permanent IP and never worry about this again,
# run these 3 commands in Google Cloud Shell (console.cloud.google.com → Shell):
#
#   gcloud compute addresses create apex-capital-static-ip \
#     --region=us-east4 --project=apex-capital-495523
#
#   # Note the IP printed, then:
#   gcloud compute instances delete-access-config apex-capital-configuration \
#     --access-config-name="External NAT" --zone=us-east4-a \
#     --project=apex-capital-495523
#
#   gcloud compute instances add-access-config apex-capital-configuration \
#     --access-config-name="External NAT" \
#     --address=<IP-FROM-FIRST-COMMAND> \
#     --zone=us-east4-a --project=apex-capital-495523
#
# After that, the IP never changes and you can delete update_ip.sh entirely.
# =============================================================================

set -e

VM_USER="jackbeard"
LOCAL_DIR="/Users/jackbeard/Claude investment strategy/institutional-tracker/"

# Use passed IP, or fall back to SSH alias "apex-capital"
if [ -n "$1" ]; then
    VM_IP="$1"
    REMOTE_DIR="$VM_USER@$VM_IP:~/institutional-tracker/"
    SSH_TARGET="$VM_USER@$VM_IP"
else
    VM_IP="apex-capital (SSH alias)"
    REMOTE_DIR="apex-capital:~/institutional-tracker/"
    SSH_TARGET="apex-capital"
fi

echo "=============================================="
echo "  APEX CAPITAL — GCP DEPLOY"
echo "  Target: $VM_IP"
echo "  $(date)"
echo "=============================================="

# ── 0. Stamp this deploy (SHA + date) — enables the VM's daily drift check ──
# pod_health_monitor on the VM compares this stamp to the repo; if the repo has
# moved and the stamp is >3 days old it escalates CRITICAL (anti-"stale prod").
GIT_SHA=$(cd "$LOCAL_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
cat > "${LOCAL_DIR}deploy_stamp.json" <<STAMP
{"sha": "$GIT_SHA", "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "from_host": "$(hostname)"}
STAMP
echo "  Deploy stamp: $GIT_SHA @ $(date -u +%Y-%m-%d)"

# ── 0b. Pull the live crontab BEFORE pushing anything ──────────────────────
# data/vm_crontab_snapshot.txt is in the rsync allowlist below, so this deploy
# is about to push the Mac's copy over the VM's. The VM refreshes that file
# itself daily (11:10 UTC), which means a Mac copy that hasn't been refreshed
# in a week would overwrite a fresher mirror with an older one — and the whole
# point of the snapshot is to be the ground truth wiring_audit.py and hr_agent.py
# measure prod against. Refreshing first makes the file we push identical to the
# one already there. Non-fatal: a failed capture leaves the existing snapshot
# alone (the script fails closed), and a stale mirror still beats no deploy.
bash "${LOCAL_DIR}scripts/refresh_crontab_snapshot.sh" || \
    echo "  WARNING: crontab snapshot refresh failed — pushing the existing copy."

# ── 1. Sync project files ──────────────────────────────────────────────────
echo ""
echo "[1/3] Syncing project files..."
# data/ is runtime state (cloud-synced, not deployed) — EXCEPT for a short
# allowlist of tracked CONFIG files that code reads to make decisions. Excluding
# data/ wholesale meant wiring_audit.py passed clean on the Mac and reported 4
# violations on the VM off the same md5-identical code, because the VM was
# running the audit against a missing schedule snapshot and a stale orphan
# baseline. Config that changes behaviour has to travel with the code that reads
# it. Includes MUST precede the exclude — rsync takes the first matching rule.
#
# DO NOT ADD data/agent_registry.json HERE. It is written live on both hosts by
# the Chief-of-Staff factory, and the two copies had diverged to 37 rows on the
# Mac and 66 on the VM. rsync is one-way, so allowlisting it would have deleted
# 30 agents — the entire FE and FI pods — from production on the next deploy.
# Reconcile it instead:  python3 agent_roster.py --sync-registry
# (agent_roster_manifest.json below is safe: it is DECLARED, git-tracked, and
# regenerated from the merged view of both hosts by --adopt.)
#
# data/.host_id is excluded FIRST, ahead of the includes, so it survives anyone
# later broadening the allowlist. It is this host's stable trading identity; if
# the Mac's copy reached the VM, both hosts would stamp orders `mac` and the
# single-writer detector — whose entire job is counting distinct hosts — would
# report one writer forever. A deploy must never be able to blind a detector.
rsync -avz --progress \
    --exclude="data/.host_id" \
    --include="data/" \
    --include="data/orphan_agents_baseline.json" \
    --include="data/vm_crontab_snapshot.txt" \
    --include="data/agent_roster_manifest.json" \
    --exclude="data/*" \
    --exclude="logs/" \
    --exclude="__pycache__/" \
    --exclude="*.pyc" \
    --exclude=".git/" \
    --exclude="gcp_setup/" \
    --exclude="oracle_setup/oracle_attempts.log" \
    "$LOCAL_DIR" \
    "$REMOTE_DIR"

echo "  Project files synced."

# ── 2. Sync .env (credentials) ────────────────────────────────────────────
echo ""
echo "[2/3] Syncing .env credentials..."
if [ -f "${LOCAL_DIR}.env" ]; then
    scp "${LOCAL_DIR}.env" "$SSH_TARGET:~/institutional-tracker/.env"
    echo "  .env deployed."
else
    echo "  WARNING: No .env found locally. You'll need to create one on the VM."
    echo "  SSH in and run: nano ~/institutional-tracker/.env"
fi

# ── 3. Sync Oracle setup ──────────────────────────────────────────────────
echo ""
echo "[3/3] Syncing Oracle provisioning script..."
rsync -avz \
    "${LOCAL_DIR}oracle_setup/" \
    "$SSH_TARGET:~/institutional-tracker/oracle_setup/" \
    --exclude="oracle_attempts.log"
echo "  Oracle setup synced."

# ── Verify ────────────────────────────────────────────────────────────────
echo ""
echo "  Verifying remote install..."
ssh "$SSH_TARGET" "cd ~/institutional-tracker && ~/venv/bin/python3 -c 'import pandas, yfinance, alpaca; print(\"  Dependencies OK\")' 2>/dev/null || python3 -c 'import pandas; print(\"  Core deps OK (venv not found, using system python)\")' 2>/dev/null || echo '  Skipping dep check'"

# ── Stale-on-VM report ────────────────────────────────────────────────────
# rsync here runs WITHOUT --delete, deliberately: the VM holds ~97 files that
# were never committed from the Mac (see the `orphan_code_2026_07` memory,
# incl. the agent_meeting.py governance system), and --delete would have
# destroyed all of them the first time it ran. The cost of that safety is that
# a file DELETED from the repo lives on in production forever. On 2026-08-01
# new_crontab.txt survived its own deletion exactly this way — a stale schedule
# document sitting in prod one day after the commit whose entire purpose was to
# ensure only one schedule document exists.
#
# So: never delete automatically, but never let it be silent either. List what
# is on the VM and not on the Mac and let a human decide. Only .py/.txt/.sh at
# depth 1-2 — data/ and logs/ are legitimately VM-only and always will be.
echo ""
echo "  Checking for files that exist on the VM but not here..."
# Non-fatal throughout: the deploy itself has already succeeded by this point,
# and an unreachable VM or an odd `find` must not turn a good deploy into a
# failed one under `set -e`.
_local_list="$(cd "$LOCAL_DIR" && find . -maxdepth 2 \( -name '*.py' -o -name '*.txt' -o -name '*.sh' \) \
    -not -path './data/*' -not -path './logs/*' -not -path './__pycache__/*' \
    -not -path './.git/*' 2>/dev/null | sort || true)"
_remote_list="$(ssh "$SSH_TARGET" "cd ~/institutional-tracker && find . -maxdepth 2 \( -name '*.py' -o -name '*.txt' -o -name '*.sh' \) \
    -not -path './data/*' -not -path './logs/*' -not -path './__pycache__/*' \
    -not -path './.git/*' 2>/dev/null | sort" || true)"
_stale="$(comm -13 <(echo "$_local_list") <(echo "$_remote_list") || true)"
if [ -z "$_remote_list" ]; then
    echo "  ⚠ Could not list the VM's files — divergence UNKNOWN, not clean."
elif [ -n "$_stale" ]; then
    echo "  ⚠ VM-only file(s) — deleted here but still live in prod, or never committed:"
    echo "$_stale" | sed 's/^/      /'
    echo "      Decide each one: commit it back, or remove it on the VM."
else
    echo "  No VM-only files — both hosts carry the same modules."
fi

echo ""
echo "=============================================="
echo "  DEPLOY COMPLETE"
echo "=============================================="
echo ""
echo "  SSH in:            ssh apex-capital"
echo "  Check cron:        crontab -l"
echo "  Tail pipeline log: tail -f ~/institutional-tracker/logs/cron_\$(date +%Y%m%d).log"
echo "  Tail Oracle log:   tail -f ~/institutional-tracker/oracle_setup/oracle_attempts.log"
echo ""
echo "  To redeploy after code changes, just run this script again."
echo ""
