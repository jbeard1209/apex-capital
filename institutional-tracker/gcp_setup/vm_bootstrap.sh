#!/bin/bash
# =============================================================================
# APEX CAPITAL — GCP VM BOOTSTRAP
# Run this ONCE on the fresh GCP e2-micro instance after SSH'ing in.
# It installs all dependencies, sets up the project directory, and
# configures cron to match the local Mac schedule.
#
# Usage:
#   bash vm_bootstrap.sh
# =============================================================================

set -e
PROJECT_DIR="$HOME/institutional-tracker"

echo "=============================================="
echo "  APEX CAPITAL — GCP VM BOOTSTRAP"
echo "  $(date)"
echo "=============================================="

# ── 1. System packages ──────────────────────────────────────────────────────
echo ""
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git curl wget unzip

python3 --version
pip3 --version

# ── 2. Python dependencies ──────────────────────────────────────────────────
echo ""
echo "[2/6] Installing Python packages..."
pip3 install --upgrade pip --quiet
pip3 install \
    "alpaca-py==0.43.2" \
    "beautifulsoup4==4.14.3" \
    "matplotlib==3.9.4" \
    "numpy==2.0.2" \
    "oci==2.173.0" \
    "pandas==2.3.3" \
    "requests==2.32.3" \
    "scipy==1.13.1" \
    "seaborn==0.13.2" \
    "yfinance==1.2.0" \
    "python-dotenv" \
    "pytz" \
    "tabulate" \
    "cryptography" \
    "paramiko" \
    --quiet

echo "  Python packages installed."

# ── 3. Project directory ─────────────────────────────────────────────────────
echo ""
echo "[3/6] Setting up project directory..."
mkdir -p "$PROJECT_DIR/data"
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$PROJECT_DIR/oracle_setup"
echo "  Directories created at $PROJECT_DIR"

# ── 4. Timezone — set to US Eastern so cron times match Mac ──────────────────
echo ""
echo "[4/6] Setting timezone to US/Eastern..."
sudo timedatectl set-timezone America/New_York 2>/dev/null || \
    sudo ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime
echo "  Timezone: $(date +%Z)"

# ── 5. Crontab — matches local Mac schedule (all times ET) ───────────────────
echo ""
echo "[5/6] Installing crontab..."

CRON_CONTENT="
# ── APEX CAPITAL PIPELINE — 24/7 GCP SCHEDULE ─────────────────────────
SHELL=/bin/bash
PATH=/home/jackbeard/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Main pipeline — weekdays only (markets don't trade overnight)
45 9  * * 1-5 cd $PROJECT_DIR && python3 main.py >> logs/cron_\$(date +\%Y\%m\%d).log 2>&1
0  12 * * 1-5 cd $PROJECT_DIR && python3 main.py >> logs/cron_\$(date +\%Y\%m\%d).log 2>&1
0  14 * * 1-5 cd $PROJECT_DIR && python3 main.py >> logs/cron_\$(date +\%Y\%m\%d).log 2>&1
15 16 * * 1-5 cd $PROJECT_DIR && python3 main.py >> logs/cron_\$(date +\%Y\%m\%d).log 2>&1

# R&D Team Leader — hourly, 24/7 (was every 2h)
0 * * * * cd $PROJECT_DIR && python3 rd_team_leader.py >> logs/rd_\$(date +\%Y\%m\%d).log 2>&1

# Canary Agent — every 3 min during market hours, every 5 min 24/7 otherwise
*/3 9-16 * * 1-5 cd $PROJECT_DIR && python3 canary_agent.py >> logs/canary_\$(date +\%Y\%m\%d).log 2>&1
*/5 0-8,17-23 * * 1-5 cd $PROJECT_DIR && python3 canary_agent.py >> logs/canary_\$(date +\%Y\%m\%d).log 2>&1
*/5 * * * 0,6   cd $PROJECT_DIR && python3 canary_agent.py >> logs/canary_\$(date +\%Y\%m\%d).log 2>&1

# Board meeting — weekday open (9:30 AM) + weekday close (5 PM) + weekend planning (Sun 8 PM)
30 9  * * 1-5 cd $PROJECT_DIR && python3 board_meeting.py >> logs/board_\$(date +\%Y\%m\%d).log 2>&1
0  17 * * 1-5 cd $PROJECT_DIR && python3 board_meeting.py >> logs/board_\$(date +\%Y\%m\%d).log 2>&1
0  20 * * 0   cd $PROJECT_DIR && python3 board_meeting.py >> logs/board_\$(date +\%Y\%m\%d).log 2>&1

# Pre-market check — weekdays 8:30 AM ET
30 8 * * 1-5 cd $PROJECT_DIR && python3 premarket_check.py >> logs/premarket_\$(date +\%Y\%m\%d).log 2>&1

# Morning briefing — daily including weekends (was weekdays only)
15 9 * * * cd $PROJECT_DIR && python3 morning_briefing.py >> logs/briefing_\$(date +\%Y\%m\%d).log 2>&1

# Evening digest email — daily 10 PM ET
0 22 * * * cd $PROJECT_DIR && python3 email_buffer.py >> logs/digest_\$(date +\%Y\%m\%d).log 2>&1

# CIT Director — 6 sessions/day: 7 AM, 10 AM, 1 PM, 4 PM, 10 PM, 1 AM ET (was 4)
0  7 * * * cd $PROJECT_DIR && python3 cit_director.py --session morning >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1
0 10 * * * cd $PROJECT_DIR && python3 cit_director.py --session morning >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1
0 13 * * * cd $PROJECT_DIR && python3 cit_director.py --session morning >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1
0 16 * * * cd $PROJECT_DIR && python3 cit_director.py --session morning >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1
0 22 * * * cd $PROJECT_DIR && python3 cit_director.py --session evening >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1
0  1 * * * cd $PROJECT_DIR && python3 cit_director.py --session evening >> logs/cit_\$(date +\%Y\%m\%d).log 2>&1

# HR Agent — nightly 11 PM ET (was never scheduled)
0 23 * * * cd $PROJECT_DIR && python3 hr_agent.py >> logs/hr_\$(date +\%Y\%m\%d).log 2>&1

# Oracle script — keep trying forever in the background
@reboot cd $PROJECT_DIR/oracle_setup && python3 try_oracle.py >> oracle_attempts.log 2>&1 &
"

echo "$CRON_CONTENT" | crontab -
echo "  Crontab installed. Jobs:"
crontab -l | grep -v "^#" | grep -v "^$" | grep -v "^SHELL\|^PATH" | wc -l | xargs echo "   " jobs scheduled

# ── 6. Done ──────────────────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "  BOOTSTRAP COMPLETE"
echo "=============================================="
echo ""
echo "  Next step: run the deploy script from your Mac:"
echo "    bash gcp_setup/deploy.sh <VM_IP>"
echo ""
echo "  That will rsync all project files + your .env to this VM."
echo ""
