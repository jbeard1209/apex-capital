#!/usr/bin/env bash
# ml_loop_market_gated.sh
# Session 1 (now → 13:25 UTC): ideas <= 24
# Market hours pause (13:25–20:15 UTC)
# Session 2 (20:15 UTC →): ideas 25-35

cd ~/institutional-tracker

PYTHON=~/venv/bin/python3
PHASE1_MAX_IDEA=24
MARKET_OPEN_UTC=1325
MARKET_CLOSE_UTC=2015

mkdir -p logs

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC'): $*"; }

# Wait for existing idea runner
EXISTING_PID=$(pgrep -f "python3 ml_improvement_loop.py" | head -1)
if [ -n "$EXISTING_PID" ]; then
    log "Waiting for existing runner PID $EXISTING_PID..."
    while kill -0 "$EXISTING_PID" 2>/dev/null; do sleep 10; done
    log "Existing runner done."
fi

log "=== Market-gated ML loop started. Phase 1: ideas <= $PHASE1_MAX_IDEA ==="

while true; do
    # Market hours gate
    HHMM=$(date -u +%H%M)
    if [ "$HHMM" -ge "$MARKET_OPEN_UTC" ] && [ "$HHMM" -lt "$MARKET_CLOSE_UTC" ]; then
        log "Market open -- sleeping 5 min..."
        sleep 300
        continue
    fi

    # Get next untested idea
    NEXT=$($PYTHON -c "
import sys
sys.path.insert(0, '.')
from ml_improvement_loop import _next_untested_idea
n = _next_untested_idea()
print(n['id'] if n else 'DONE')
" 2>/dev/null)

    if [ -z "$NEXT" ] || [ "$NEXT" = "DONE" ]; then
        log "All ideas tested. Loop complete."
        break
    fi

    # Phase 1 gate: before market open, only run ideas <= PHASE1_MAX_IDEA
    HHMM=$(date -u +%H%M)
    if [ "$HHMM" -lt "$MARKET_OPEN_UTC" ] && [ "$NEXT" -gt "$PHASE1_MAX_IDEA" ]; then
        log "Phase 1 done (next=$NEXT > $PHASE1_MAX_IDEA). Waiting for market close..."
        sleep 300
        continue
    fi

    # Run the idea
    log "Starting idea $NEXT"
    LOGFILE="logs/ml_improvement_idea${NEXT}.log"
    $PYTHON ml_improvement_loop.py >> "$LOGFILE" 2>&1
    log "Finished idea $NEXT"
    sleep 2
done

log "=== Loop complete ==="
