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
GIT_BRANCH=$(cd "$LOCAL_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
cat > "${LOCAL_DIR}deploy_stamp.json" <<STAMP
{"sha": "$GIT_SHA", "branch": "$GIT_BRANCH", "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "from_host": "$(hostname)"}
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
echo "[1/2] Syncing project files..."
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
# parameter_derivations.json + parameter_audit_baseline.json are safe for a
# different reason, and it is worth stating so nobody generalises from the
# agent_registry disaster above. Both are pure functions of the source tree:
# parameter_audit reads CODE, never runtime state, so a one-way rsync cannot
# destroy anything one host learned and the other did not. They must travel with
# the code because the alternative is what signoff_ledger's deploy budget became
# — a limit described as firm-wide that was quietly enforced per-host.
#
# This comment first said the two hosts therefore "compute byte-identical
# numbers". Measured on 2026-08-21 immediately after deploying d7b5d9b, that was
# false in the totals: Mac 324 modules / 441 constants, VM 320 / 438. Every
# audited number agreed exactly (77 consequential, 53 reachable, 3 documented,
# 50 undocumented, ratchet FLAT) — the gap was four untracked host-local files
# this script correctly refuses to ship, already parked in `dormant`. The claim
# was true where it mattered and wrong as written, which is the exact failure
# parameter_audit exists to catch, so: same checkout AND same working tree gives
# identical numbers, and the census now prints `of which host-local` so the
# difference explains itself instead of needing this paragraph.
#
# mac_launchd_snapshot.txt travels for the same reason vm_crontab_snapshot.txt
# does, and the direction is not a mistake. The firm runs TWO schedulers: cron on
# the VM, launchd on the Mac. Each host can only capture its own, and only this
# rsync crosses between them — so the Mac captures launchd and ships it, exactly
# as the Mac SSHes the VM for its crontab and ships that. Without the include,
# the same wiring_audit.py on md5-identical code sees a schedule on the Mac and
# half a schedule on the VM: the fix reaches the repo and never reaches
# production, one file after gcp_setup/ itself did (memory
# `fix-that-cannot-reach-production`). rsync is one-way and the VM never writes
# this file, so there is nothing on the far side to destroy — the agent_registry
# hazard above does not apply.
#
# data/.host_id is excluded FIRST, ahead of the includes, so it survives anyone
# later broadening the allowlist. It is this host's stable trading identity; if
# the Mac's copy reached the VM, both hosts would stamp orders `mac` and the
# single-writer detector — whose entire job is counting distinct hosts — would
# report one writer forever. A deploy must never be able to blind a detector.
#
# gcp_setup/ used to be excluded here. It is the one directory whose contents
# are written TO BE RUN ON THE VM — vm_bootstrap.sh rebuilds it, and
# restore_crontab.sh reinstalls its 86-job schedule — so excluding it meant the
# VM's copies were frozen at whatever someone last hand-scp'd. On 2026-08-16 the
# VM was carrying the 2026-05-06 vm_bootstrap.sh: the version that sets
# America/New_York (shifting every UTC-authored job 4-5 hours) and pipes its
# 21-job seed straight over the live crontab with no shrink guard. The commit
# that fixed both (a6dcf3c) could not reach the host it was written to protect,
# and restore_crontab.sh — whose own header says "run this ON THE VM, after
# deploy.sh has rsynced the repo across" — had never been deployed at all.
# A repair that cannot travel to the machine it repairs is not deployed.
#
# The Oracle provisioning kit is excluded below, and that exclusion is the rare
# kind that is correct: it is the tooling that CREATES a third host, plus the
# private key that authenticates to the account. A forgotten Oracle instance
# already ran a full parallel firm against this same Alpaca account for 69 days
# (terminated 2026-07-27). The kit is gitignored, so it reached the VM without
# ever passing a review, and nothing there schedules or imports it. Shipping the
# means to rebuild the incident, to the host that would rebuild it, buys
# nothing. It stays on the Mac, where Jack runs it by hand or not at all.
#
# ...and that reasoning applies to every gitignored file, not just that one
# directory. Naming oracle_setup/ fixed the instance and left the class open:
# on 2026-08-18 `deployed_set_matches_repo` reported two files "on host, in no
# commit" — _measure_horizon.py and _measure_walk.py, throwaway measurement
# scripts written in this directory an hour earlier. 362 untracked files were
# shipping to the trading host on every deploy, none of them reviewed, none of
# them in any commit. A scratch file named like a module the pipeline imports
# would shadow it, and the shadow would exist only in production.
#
# So the exclusion is now DERIVED, not listed: git decides what is deployable.
# `git ls-files --others` (deliberately WITHOUT --exclude-standard, so it
# reports ignored files too) is every path git does not track; each is anchored
# with a leading "/" so it matches from the transfer root and not at any depth.
# .env is in that set and is meant to be — it travels in step 2, by itself, on
# purpose. If this list ever over-matches and drops a file the VM needs, the
# same law reports it from the other side as "absent from apex-capital", so
# both failure directions are observable rather than silent.
DEPLOY_EXCLUDES="$(mktemp "${TMPDIR:-/tmp}/apex_deploy_excludes.XXXXXX")"
trap 'rm -f "$DEPLOY_EXCLUDES"' EXIT
git -C "$LOCAL_DIR" ls-files --others | sed 's|^|/|' > "$DEPLOY_EXCLUDES"
echo "  $(wc -l < "$DEPLOY_EXCLUDES" | tr -d ' ') untracked path(s) held back — git decides what deploys."

# data/signoff_mode.json is in the allowlist because the autonomy gate is a
# firm-wide decision carried in a per-host file. It was tracked in git from the
# start but never shipped, and the VM is not a checkout — so it had never once
# arrived there. Both hosts read SHADOW today, which is why nothing showed:
# get_mode() returns SHADOW for an absent file exactly as it does for a recorded
# one. The day board+CIT vote ENFORCE, that write lands on whichever host ran
# the vote; without this line the VM keeps auto-actuating on a default while the
# firm believes it is enforcing, on the same Alpaca account. A guardrail that
# reaches one of two hosts is the shape of guardrail-on-one-path.
rsync -avz --progress \
    --exclude="data/.host_id" \
    --include="data/" \
    --include="data/orphan_agents_baseline.json" \
    --include="data/vm_crontab_snapshot.txt" \
    --include="data/mac_launchd_snapshot.txt" \
    --include="data/agent_roster_manifest.json" \
    --include="data/pick_gate_trial.json" \
    --include="data/parameter_derivations.json" \
    --include="data/parameter_audit_baseline.json" \
    --include="data/signoff_mode.json" \
    --exclude="data/*" \
    --exclude-from="$DEPLOY_EXCLUDES" \
    --exclude="logs/" \
    --exclude="__pycache__/" \
    --exclude="*.pyc" \
    --exclude=".git/" \
    --exclude="oracle_setup/" \
    "$LOCAL_DIR" \
    "$REMOTE_DIR"

echo "  Project files synced."

# ── 1b. INFRASTRUCTURE.md ────────────────────────────────────────────────────
# The host inventory lives at the REPO ROOT, one level above the directory this
# script rsyncs, so no include pattern above could ever reach it. That gap had a
# consequence: coherence_laws.law_broker_host_census names INFRASTRUCTURE.md as
# its source B, and on the VM the doc did not exist — so the one law written to
# catch an undocumented host (the Oracle instance ran 69 days on this account)
# was reporting HOLDS on the host that does the trading, with its written source
# absent. The test that pins DOCUMENTED_HOST_TAGS to the doc could not run there
# either. A source that is not deployed is not a source.
if [ -f "${LOCAL_DIR}../INFRASTRUCTURE.md" ]; then
    scp "${LOCAL_DIR}../INFRASTRUCTURE.md" "$SSH_TARGET:~/institutional-tracker/INFRASTRUCTURE.md"
    echo "  INFRASTRUCTURE.md deployed (source B for the host census)."
else
    echo "  WARNING: INFRASTRUCTURE.md not found — the VM's host census will"
    echo "  have no written source to check itself against."
fi

# ── 2. Sync .env (credentials) ────────────────────────────────────────────
echo ""
echo "[2/2] Syncing .env credentials..."
if [ -f "${LOCAL_DIR}.env" ]; then
    scp "${LOCAL_DIR}.env" "$SSH_TARGET:~/institutional-tracker/.env"
    echo "  .env deployed."
else
    echo "  WARNING: No .env found locally. You'll need to create one on the VM."
    echo "  SSH in and run: nano ~/institutional-tracker/.env"
fi

# ── 2b. Stamp the VM, over ssh, only now that the deploy has succeeded ───────
# The stamp written at step 0 is the MAC's copy. It used to reach the VM as an
# ordinary rsync payload, which quietly made the freshness detector depend on
# the deploy's own file list: the moment step 1 started deriving its exclusions
# from git, deploy_stamp.json — untracked, generated, in no commit — was held
# back, and the VM's stamp froze at 2026-08-18T03:23Z while deploys kept
# landing. pod_health_monitor.py and reentry_slate.py both read that file on
# whichever host they run; a frozen stamp does not read as "stale", it reads as
# the last deploy that happened to slip through, forever. That is the shape of
# `fix_that_erased_its_detector` — a remediation deleting the field its own
# check consumes — and it was caught by looking at the stamp after a deploy
# rather than by trusting that the deploy had updated it.
#
# So the VM is now stamped directly, and deliberately LAST: `set -e` means any
# failure above aborts before this line, leaving the previous stamp in place so
# a half-finished deploy reports as drift instead of as success.
ssh "$SSH_TARGET" "cat > ~/institutional-tracker/deploy_stamp.json" <<STAMP
{"sha": "$GIT_SHA", "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "from_host": "$(hostname)"}
STAMP
echo "  VM stamped: $GIT_SHA (written on the host, not rsynced)."

# ── 3. (removed 2026-08-17) Oracle setup is deliberately NOT deployed ─────
# This step used to push oracle_setup/ — provisioning scripts and oci_key.pem —
# to production on every deploy. See the note above rsync in step 1. The VM's
# copy was quarantined to ~/quarantine_oracle_20260817/ the same day; the
# originals remain on the Mac.

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
echo ""
echo "  To redeploy after code changes, just run this script again."
echo ""
