#!/bin/bash
# setup_healthcheck.sh — One-time setup for healthchecks.io monitoring
# 
# USAGE:
#   1. Go to https://healthchecks.io and create a free account
#   2. Create a new check: name "Apex Capital Main", period 6h, grace 2h
#   3. Copy the ping URL (looks like: https://hc-ping.com/<uuid>)
#   4. Run: bash setup_healthcheck.sh <paste-url-here>

set -e

if [ -z "$1" ]; then
    echo "Usage: bash setup_healthcheck.sh <healthchecks-ping-url>"
    echo "Example: bash setup_healthcheck.sh https://hc-ping.com/abc123"
    exit 1
fi

HC_URL="$1"

# Add to bashrc
if ! grep -q "APEX_HEALTHCHECK_URL" ~/.bashrc; then
    echo "" >> ~/.bashrc
    echo "# Apex Capital healthcheck URL (set by setup_healthcheck.sh)" >> ~/.bashrc
    echo "export APEX_HEALTHCHECK_URL=\"$HC_URL\"" >> ~/.bashrc
    echo "Added to ~/.bashrc"
fi

# Add to crontab environment  
CURRENT_CRON=$(crontab -l 2>/dev/null)
if ! echo "$CURRENT_CRON" | grep -q "APEX_HEALTHCHECK_URL"; then
    (echo "APEX_HEALTHCHECK_URL=$HC_URL"; echo "$CURRENT_CRON") | crontab -
    echo "Added APEX_HEALTHCHECK_URL to crontab environment"
fi

echo ""
echo "✅ Healthcheck configured: $HC_URL"
echo "   main.py will ping this URL at the end of every successful run."
echo "   healthchecks.io will email you if it goes silent for 2+ hours."
echo ""
echo "Test with: curl -fsS --max-time 10 \"$HC_URL\""
