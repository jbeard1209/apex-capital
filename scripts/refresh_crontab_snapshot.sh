#!/usr/bin/env bash
# Refresh the canonical prod-schedule snapshot that wiring_audit.py trusts.
# The VM's live crontab is canonical for prod (see new_crontab.txt header); this
# pulls it down so the audit stops judging "scheduled" from a stale Mac template.
set -euo pipefail
cd "$(dirname "$0")/../institutional-tracker"
OUT="data/vm_crontab_snapshot.txt"
TMP="$(mktemp)"
{
  echo "# CANONICAL PROD SCHEDULE — verbatim snapshot of the GCP VM's live crontab."
  echo "# Captured $(date -u +%Y-%m-%dT%H:%M:%SZ) from apex-capital via: ssh apex-capital 'crontab -l'"
  echo "# DO NOT hand-edit — it is a mirror, not a control surface."
  echo ""
  ssh -o ConnectTimeout=20 apex-capital 'crontab -l'
} > "$TMP"
# Never install an empty/failed pull over a good snapshot.
if [ "$(grep -cE '^[^#[:space:]]' "$TMP")" -lt 10 ]; then
  echo "REFUSING: pulled crontab has <10 job lines — VM unreachable or crontab wiped." >&2
  rm -f "$TMP"; exit 1
fi
mv "$TMP" "$OUT"
echo "Snapshot refreshed: $(grep -cE '^[^#[:space:]]' "$OUT") job lines -> $OUT"
