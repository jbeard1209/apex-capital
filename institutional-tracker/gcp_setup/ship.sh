#!/bin/bash
# =============================================================================
# APEX CAPITAL — SHIP: the ONLY sanctioned way to release code to production.
#   push to GitHub -> deploy to the GCP VM (with stamp) -> remote smoke test.
# One command so "forgot to deploy" (the June/July stale-prod incident) can't
# happen out of laziness. Usage: bash gcp_setup/ship.sh
# =============================================================================
set -e
DIR="/Users/jackbeard/Claude investment strategy/institutional-tracker"
cd "$DIR"
echo "[ship 1/3] push to GitHub..."
git push
echo "[ship 2/3] deploy to VM..."
bash gcp_setup/deploy.sh
echo "[ship 3/3] remote verification..."
ssh -o ConnectTimeout=10 -o BatchMode=yes apex-capital \
  'cd ~/institutional-tracker && cat deploy_stamp.json && ~/venv/bin/python3 test_pipeline_smoke.py 2>&1 | tail -2'
echo "SHIPPED ✅ — prod is current."
