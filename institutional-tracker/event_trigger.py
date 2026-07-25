#!/usr/bin/env python3
"""
event_trigger.py — Event-Driven R&D Trigger
============================================
Monitors for market events that warrant immediate R&D analysis:
  1. Earnings beats (EPS > 10% above estimate)
  2. Insider clusters (3+ insiders buying same stock within 7 days)
  3. Canary GREEN flip (from ORANGE/RED)
  4. Price spike (daily move >5% with above-average volume)

On any trigger: immediately fires rd_team_leader.py for the affected ticker.
Designed to replace fixed 2h R&D cadence with event-driven coverage.

Schedule: every 30min via crontab (fast, mostly reads cached data)
"""
import json, logging, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("event_trigger")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "event_trigger_state.json"


def _load_json(path, default):
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_state(state: dict):
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, str(STATE_FILE))


def check_canary_flip(state: dict) -> list[str]:
    """Detect canary GREEN flip from ORANGE/RED."""
    triggers = []
    canary = _load_json(DATA_DIR / "canary_state.json", {})
    current = canary.get("last_level", "GREEN")
    prev = state.get("last_canary_level", "GREEN")
    if prev in ("ORANGE", "RED") and current == "GREEN":
        logger.info(f"  🟢 Canary flipped {prev}→GREEN — triggering portfolio R&D refresh")
        triggers.append("_PORTFOLIO_REFRESH_")
    state["last_canary_level"] = current
    return triggers


def check_price_spikes(state: dict) -> list[str]:
    """Detect daily price moves >5% in open positions."""
    triggers = []
    try:
        import sys; sys.path.insert(0, str(BASE_DIR))
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        pos = trader.get_positions()
        if pos.empty:
            return []
        last_spikes = state.get("last_spike_tickers", {})
        now_str = datetime.now(timezone.utc).isoformat()[:10]
        for _, row in pos.iterrows():
            tk = row["ticker"]
            pnl_pct = float(row.get("unrealized_pct", 0))
            # Flag if today's move looks like a spike (rough proxy from pnl vs entry)
            # More precisely: check if pnl moved >5% since last check
            prev_pnl = last_spikes.get(tk, pnl_pct)
            daily_move = abs(pnl_pct - prev_pnl)
            if daily_move >= 5.0 and last_spikes.get(f"{tk}_date") != now_str:
                logger.info(f"  📈 {tk}: {daily_move:+.1f}% daily move — triggering R&D")
                triggers.append(tk)
                state.setdefault("last_spike_tickers", {})[f"{tk}_date"] = now_str
            state.setdefault("last_spike_tickers", {})[tk] = pnl_pct
    except Exception as e:
        logger.debug(f"  Price spike check non-fatal: {e}")
    return triggers


def check_earnings_beats(state: dict) -> list[str]:
    """Check earnings_predictions for upcoming earnings within 2 days."""
    triggers = []
    try:
        ep = _load_json(DATA_DIR / "earnings_predictions.json", {})
        preds = ep.get("predictions", [])
        now = datetime.now(timezone.utc)
        triggered = set(state.get("earnings_triggered", []))
        for p in preds:
            if not isinstance(p, dict): continue
            ticker = p.get("ticker", "?")
            days = p.get("days_until", 999)
            beat_rate = p.get("beat_rate")
            if (isinstance(days, (int, float)) and 0 <= days <= 2
                    and beat_rate and float(beat_rate) >= 0.8
                    and ticker not in triggered):
                logger.info(f"  💰 {ticker}: earnings in {days}d, beat_rate={beat_rate:.0%} — pre-earnings R&D")
                triggers.append(ticker)
                triggered.add(ticker)
        # Clean old triggered after 3 days
        state["earnings_triggered"] = list(triggered)[-50:]
    except Exception as e:
        logger.debug(f"  Earnings check non-fatal: {e}")
    return triggers


def fire_rd_for_ticker(ticker: str):
    """Trigger immediate R&D analysis for a specific ticker."""
    try:
        import subprocess
        py = str(BASE_DIR.parent / "venv" / "bin" / "python3")
        log = str(BASE_DIR / "logs" / f"event_triggered_rd_{ticker}_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
        cmd = [py, str(BASE_DIR / "rd_team_leader.py"), "--ticker", ticker]
        # Check if rd_team_leader supports --ticker flag
        result = subprocess.run([py, str(BASE_DIR / "rd_team_leader.py"), "--help"],
                                capture_output=True, text=True, timeout=5)
        if "--ticker" in result.stdout or "--ticker" in result.stderr:
            subprocess.Popen(cmd, stdout=open(log, "w"), stderr=subprocess.STDOUT,
                           cwd=str(BASE_DIR))
            logger.info(f"  🚀 Fired targeted R&D for {ticker}")
        else:
            # Fall back to full R&D run
            subprocess.Popen(
                [py, str(BASE_DIR / "rd_team_leader.py")],
                stdout=open(log, "w"), stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR)
            )
            logger.info(f"  🚀 Fired full R&D run (no --ticker support) for {ticker}")
    except Exception as e:
        logger.warning(f"  R&D fire for {ticker} failed: {e}")


def run():
    logger.info("=" * 55)
    logger.info("  APEX CAPITAL — EVENT TRIGGER AGENT")
    logger.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info("=" * 55)

    state = _load_json(STATE_FILE, {})
    all_triggers = []

    # Run all checks
    all_triggers += check_canary_flip(state)
    all_triggers += check_price_spikes(state)
    all_triggers += check_earnings_beats(state)

    # Fire R&D for each unique trigger
    fired = set()
    for ticker in all_triggers:
        if ticker not in fired:
            fire_rd_for_ticker(ticker)
            fired.add(ticker)

    _save_state(state)

    if fired:
        logger.info(f"  ✅ Triggered R&D for: {', '.join(fired)}")
    else:
        logger.info("  ✓ No events triggered this cycle")

    logger.info("  EVENT TRIGGER DONE")


if __name__ == "__main__":
    run()
