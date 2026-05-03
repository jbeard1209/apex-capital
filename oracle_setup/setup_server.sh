#!/bin/bash
# =============================================================================
# APEX CAPITAL — Server Setup Script
# Runs automatically on the Oracle server after first SSH connection.
# Do not run this manually.
# =============================================================================

set -e  # Exit immediately if any command fails

echo ""
echo "============================================================"
echo "  APEX CAPITAL — Automated Server Setup"
echo "============================================================"

# ── 1. System update ─────────────────────────────────────────────────────────
echo ""
echo "  [1/7] Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
echo "  ✓ System updated"

# ── 2. Install Python and dependencies ───────────────────────────────────────
echo ""
echo "  [2/7] Installing Python 3, pip, and git..."
sudo apt-get install -y -qq python3 python3-pip python3-venv git curl
echo "  ✓ Python $(python3 --version) installed"
echo "  ✓ Git $(git --version) installed"

# ── 3. Clone the repository ───────────────────────────────────────────────────
echo ""
echo "  [3/7] Cloning Apex Capital repository from GitHub..."
cd /home/ubuntu

if [ -d "apex-capital" ]; then
    echo "  Directory exists — pulling latest..."
    cd apex-capital && git pull
else
    git clone https://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@github.com/jbeard1209/apex-capital.git
    cd apex-capital
fi
echo "  ✓ Repository cloned to /home/ubuntu/apex-capital"

# ── 4. Install Python packages ───────────────────────────────────────────────
echo ""
echo "  [4/7] Installing Python packages..."
pip3 install -q -r requirements.txt
echo "  ✓ Python packages installed"

# ── 5. Create required directories ──────────────────────────────────────────
echo ""
echo "  [5/7] Creating data and log directories..."
mkdir -p logs data/backups
echo "  ✓ Directories created"

# ── 6. Verify the setup ──────────────────────────────────────────────────────
echo ""
echo "  [6/7] Verifying setup..."
python3 -c "import alpaca_trade_api, yfinance, requests; print('  ✓ Core packages verified')" || \
python3 -c "import requests; print('  ✓ Basic packages verified')"

# ── 7. Done ──────────────────────────────────────────────────────────────────
echo ""
echo "  [7/7] Setup complete!"
echo ""
echo "============================================================"
echo "  Server is ready for crontab configuration."
echo "============================================================"
echo ""
