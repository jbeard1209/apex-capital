#!/bin/bash
# =============================================================================
# APEX CAPITAL — UPDATE GCP IP
# Run this when the GCP instance gets a new external IP (e.g. after a restart).
#
# Usage:
#   bash gcp_setup/update_ip.sh <NEW_IP>
#
# This updates ~/.ssh/config so that "apex-capital" resolves to the new IP.
# All scripts that use "ssh apex-capital" or "scp apex-capital:..." will
# automatically pick up the change — nothing else needs updating.
#
# Once you reserve a static IP in GCP Cloud Console, you can delete this script.
# =============================================================================

set -e

NEW_IP="${1:?Usage: bash gcp_setup/update_ip.sh <NEW_IP>}"
SSH_CONFIG="$HOME/.ssh/config"

if [ ! -f "$SSH_CONFIG" ]; then
    echo "ERROR: ~/.ssh/config not found."
    exit 1
fi

# Extract old IP for reporting
OLD_IP=$(grep -A1 "Host apex-capital" "$SSH_CONFIG" | grep "HostName" | awk '{print $2}' || echo "unknown")

# Replace HostName line under the apex-capital block
sed -i '' "/^Host apex-capital$/,/^Host / s/HostName .*/HostName $NEW_IP/" "$SSH_CONFIG"

echo "✓ Updated ~/.ssh/config"
echo "  apex-capital: $OLD_IP  →  $NEW_IP"
echo ""
echo "Verify with: ssh apex-capital 'echo connected'"
