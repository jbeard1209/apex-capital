#!/usr/bin/env bash
# ml_loop_runner.sh — keeps calling ml_improvement_loop.py until all ideas tested
# Run this from ~/institutional-tracker with the venv python
# 
# LOCK FILE (#27): Only one ML loop can run at a time.
# If a loop is already running, this script exits immediately.

set -e
cd ~/institutional-tracker

PYTHON=~/venv/bin/python3
LOG_DIR=logs
LOCK_FILE="/tmp/ml_loop_runner.lock"
mkdir -p "$LOG_DIR"

# ── Single-instance guard ─────────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[ml_loop_runner] Another ML loop is already running (lock: $LOCK_FILE). Exiting."
    exit 0
fi
trap "flock -u 9; rm -f $LOCK_FILE" EXIT

echo "[ml_loop_runner] Starting continuous ML improvement loop at $(date -u)"
echo "[ml_loop_runner] Lock acquired: $LOCK_FILE (PID $$)"

while true; do
    # Check if there are any untested ideas
    NEXT_IDEA=$($PYTHON ml_improvement_loop.py --list 2>/dev/null | grep '\[UNTESTED\]' | head -1)
    if [ -z "$NEXT_IDEA" ]; then
        echo "[ml_loop_runner] All ideas tested. Loop complete at $(date -u)"
        break
    fi

    IDEA_NUM=$(echo "$NEXT_IDEA" | grep -oP 'Idea \K\d+')
    IDEA_NAME=$(echo "$NEXT_IDEA" | grep -oP ': \K[a-z_]+')
    echo "[ml_loop_runner] $(date -u)  Starting idea $IDEA_NUM: $IDEA_NAME"

    LOGFILE="$LOG_DIR/ml_improvement_idea${IDEA_NUM}.log"
    # Run the idea — captures to log AND stdout
    $PYTHON ml_improvement_loop.py 2>&1 | tee "$LOGFILE"

    echo "[ml_loop_runner] $(date -u)  Idea $IDEA_NUM finished. Sleeping 30s..."
    sleep 30
done
