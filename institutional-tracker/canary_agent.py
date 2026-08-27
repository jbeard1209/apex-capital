# =============================================================================
# APEX CAPITAL — CANARY AGENT
# =============================================================================
# The Canary is always awake. It runs every 5 minutes during market hours and
# every 30 minutes overnight/weekends via cron, watching for anything that
# warrants waking the Manager Agent outside the normal pipeline schedule.
#
# ALERT LEVELS
# ─────────────
#   GREEN   All clear. Market within normal parameters.
#   YELLOW  Early stress signal. Canary increases monitoring sensitivity.
#           No action triggered — logged only.
#   ORANGE  Significant move. Chairman notified. Manager Agent placed on
#           standby — will act at next scheduled pipeline run.
#   RED     CRASH / CRISIS signal. Manager Agent woken immediately.
#           Full pipeline executes right now, with canary context injected
#           so the Manager knows to prioritise risk reduction.
#
# WHAT IT MONITORS
# ─────────────────
#   Market hours  (9:30 AM – 4:00 PM ET, Mon–Fri)
#     • SPY intraday % change vs prior close
#     • QQQ intraday % change
#     • VIX level and daily % change
#     • Any open Alpaca position down > threshold intraday
#     • Sector ETF breadth (how many of the 16 sectors are negative)
#
#   After hours / overnight / weekends
#     • S&P 500 futures (ES=F)
#     • Nasdaq futures (NQ=F)
#     • VIX (settles at close but futures available)
#     • Major global indices that are currently open
#
# COOLDOWNS (prevents alert spam)
#   RED    → 2 hours before another RED can fire
#   ORANGE → 45 minutes before another ORANGE can fire
#   YELLOW → 15 minutes before another YELLOW can fire
#
# THRESHOLDS (all configurable at top of file)
# ─────────────────────────────────────────────
#   SPY down -1.5% intraday   → YELLOW
#   SPY down -2.5% intraday   → ORANGE
#   SPY down -3.5% intraday   → RED
#   VIX level  ≥ 22            → YELLOW
#   VIX level  ≥ 28            → ORANGE
#   VIX level  ≥ 35            → RED
#   VIX spike  ≥ +20% 1-day   → ORANGE
#   VIX spike  ≥ +35% 1-day   → RED
#   Any position down -5%     → ORANGE
#   Any position down -8%     → RED
#   Futures drop ≥ -1.5%     → YELLOW (overnight)
#   Futures drop ≥ -2.5%     → ORANGE (overnight)
#
# State file:  data/canary_state.json   (persists between runs)
# Alert file:  data/canary_alert.json   (read by main.py on startup)
#
# Usage (called by cron — never needs manual invocation):
#   python3 canary_agent.py
#   python3 canary_agent.py --test-red    # simulate RED for testing
#   python3 canary_agent.py --status      # print current state and exit
# =============================================================================

import fcntl
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import yfinance as yf

# Suppress yfinance's internal download-failure ERROR messages — these are
# noisy API failures (NoneType errors, 1 Failed download, etc.) that get
# printed to stderr and counted as ERROR lines by the CIT Pipeline Monitor.
# The canary already handles None returns from _get_intraday_change() gracefully.
import logging as _logging
_logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
_logging.getLogger("yfinance.base").setLevel(_logging.CRITICAL)
_logging.getLogger("yfinance.download").setLevel(_logging.CRITICAL)
_logging.getLogger("peewee").setLevel(_logging.CRITICAL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("canary")

# ── File paths ────────────────────────────────────────────────────────────────
STATE_PATH  = "data/canary_state.json"
ALERT_PATH  = "data/canary_alert.json"

# ── Thresholds ────────────────────────────────────────────────────────────────
SPY_YELLOW  = -1.5    # % intraday decline
SPY_ORANGE  = -2.5
SPY_RED     = -3.5

VIX_YELLOW  = 22      # absolute VIX level
VIX_ORANGE  = 28
VIX_RED     = 35

VIX_SPIKE_ORANGE = 20.0   # % daily VIX increase
VIX_SPIKE_RED    = 35.0

POSITION_ORANGE  = -5.0   # % unrealised loss on any single position
POSITION_RED     = -8.0

FUTURES_YELLOW   = -1.5   # % futures decline (overnight)
FUTURES_ORANGE   = -2.5

# ── Cooldown durations (minutes) ──────────────────────────────────────────────
COOLDOWN_RED    = 120
COOLDOWN_ORANGE = 45
COOLDOWN_YELLOW = 15

# ── Sector ETFs to check for breadth ─────────────────────────────────────────
SECTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLY", "XLI",
    "XLB", "XLRE", "XLC", "XLU", "XLP", "XLE",
]


# =============================================================================
# MARKET HOURS CHECK
# =============================================================================

def _is_market_open() -> bool:
    """Returns True if the US equity market is currently open (ET)."""
    from zoneinfo import ZoneInfo
    et   = ZoneInfo("America/New_York")
    now  = datetime.now(et)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def _is_weekday() -> bool:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).weekday() < 5


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_level":       "GREEN",
        "last_level_time":  None,
        "last_red_time":    None,
        "last_orange_time": None,
        "last_yellow_time": None,
        "alerts_today":     [],
        "manager_triggers": [],
    }


def _save_state(state: dict):
    os.makedirs("data", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _minutes_since(iso_time: Optional[str]) -> float:
    if not iso_time:
        return float("inf")
    try:
        t = datetime.fromisoformat(iso_time)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - t
        return delta.total_seconds() / 60
    except Exception:
        return float("inf")


def _in_cooldown(state: dict, level: str) -> bool:
    if level == "RED":
        return _minutes_since(state.get("last_red_time")) < COOLDOWN_RED
    if level == "ORANGE":
        return _minutes_since(state.get("last_orange_time")) < COOLDOWN_ORANGE
    if level == "YELLOW":
        return _minutes_since(state.get("last_yellow_time")) < COOLDOWN_YELLOW
    return False


# =============================================================================
# MARKET DATA FETCHERS
# =============================================================================

def _flatten_yf(df) -> "pd.DataFrame":
    """
    yfinance ≥ 0.2.x returns MultiIndex columns (metric, ticker) when
    downloading a single ticker. Flatten to single-level so df["Close"]
    always returns a Series regardless of yfinance version.
    """
    import pandas as pd
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    return df


def _pct_change_today(ticker: str, interval: str = "5m") -> Optional[float]:
    """
    Returns the intraday % change for a ticker (vs prior close).
    Uses 5-minute bars during market hours; falls back to daily bar otherwise.
    """
    try:
        if _is_market_open():
            df = _flatten_yf(yf.download(ticker, period="2d", interval=interval,
                                         progress=False, auto_adjust=True))
            if df.empty or len(df) < 2:
                return None
            today_str = datetime.now().strftime("%Y-%m-%d")
            today_bars = df[df.index.strftime("%Y-%m-%d") == today_str]
            prev_bars  = df[df.index.strftime("%Y-%m-%d") < today_str]
            if today_bars.empty or prev_bars.empty:
                return None
            close = float(today_bars["Close"].iloc[-1])
            prev  = float(prev_bars["Close"].iloc[-1])
            return (close - prev) / prev * 100
        else:
            df = _flatten_yf(yf.download(ticker, period="5d", progress=False, auto_adjust=True))
            if df.empty or len(df) < 2:
                return None
            close = float(df["Close"].iloc[-1])
            prev  = float(df["Close"].iloc[-2])
            return (close - prev) / prev * 100
    except Exception:
        return None


def _get_vix() -> tuple[Optional[float], Optional[float]]:
    """Returns (current VIX level, VIX daily % change)."""
    try:
        df = _flatten_yf(yf.download("^VIX", period="5d", progress=False, auto_adjust=True))
        if df.empty or len(df) < 2:
            return None, None
        level   = float(df["Close"].iloc[-1])
        prev    = float(df["Close"].iloc[-2])
        pct_chg = (level - prev) / prev * 100
        return level, pct_chg
    except Exception:
        return None, None


def _get_open_positions_changes() -> list[dict]:
    """
    Returns intraday % change for each open Alpaca position.
    Falls back to empty list if Alpaca unavailable.
    """
    positions = []
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        pos_df = trader.get_positions()
        if pos_df.empty:
            return []
        for _, row in pos_df.iterrows():
            ticker  = row.get("ticker", "")
            pnl_pct = float(row.get("unrealized_pct", 0))
            positions.append({"ticker": ticker, "pnl_pct": pnl_pct})
    except Exception:
        pass
    return positions


def _get_futures_change() -> Optional[float]:
    """S&P 500 futures % change — used for overnight monitoring."""
    return _pct_change_today("ES=F", interval="30m")


def _count_negative_sectors() -> int:
    """How many of the tracked sector ETFs are negative today."""
    count = 0
    for etf in SECTOR_ETFS:
        chg = _pct_change_today(etf)
        if chg is not None and chg < -0.5:
            count += 1
    return count


# =============================================================================
# ALERT ASSESSMENT
# =============================================================================

def assess_alert_level() -> dict:
    """
    Gather all market data and determine the current alert level.
    Returns a dict with level, triggers, and all raw readings.
    """
    triggers: list[str] = []
    readings: dict      = {}
    level               = "GREEN"

    market_open = _is_market_open()
    readings["market_open"] = market_open

    # ── SPY (or futures if market closed) ─────────────────────────────────
    if market_open:
        spy_chg = _pct_change_today("SPY")
        qqq_chg = _pct_change_today("QQQ")
        readings["spy_pct"] = spy_chg
        readings["qqq_pct"] = qqq_chg

        # If SPY data is unavailable (Yahoo Finance blip), raise at most YELLOW.
        # Defaulting to ORANGE on a data-feed failure is too aggressive — it blocks
        # all capital deployment for a network/API glitch rather than an actual crash.
        # Other triggers (VIX, positions, QQQ) still fire independently.
        if spy_chg is None:
            _lvl_order = ["GREEN", "YELLOW", "ORANGE", "RED"]
            level = max(level, "YELLOW", key=lambda l: _lvl_order.index(l))
            triggers.append("SPY data unavailable — raising to YELLOW (data feed issue; not a crash signal)")

        if spy_chg is not None:
            if spy_chg <= SPY_RED:
                level = "RED"
                triggers.append(f"SPY down {spy_chg:.2f}% intraday (threshold: {SPY_RED}%)")
            elif spy_chg <= SPY_ORANGE:
                level = max(level, "ORANGE",
                            key=lambda l: ["GREEN","YELLOW","ORANGE","RED"].index(l))
                triggers.append(f"SPY down {spy_chg:.2f}% intraday")
            elif spy_chg <= SPY_YELLOW:
                level = max(level, "YELLOW",
                            key=lambda l: ["GREEN","YELLOW","ORANGE","RED"].index(l))
                triggers.append(f"SPY down {spy_chg:.2f}% (early stress)")

        if qqq_chg is not None and qqq_chg <= SPY_ORANGE:
            level = max(level, "ORANGE",
                        key=lambda l: ["GREEN","YELLOW","ORANGE","RED"].index(l))
            triggers.append(f"QQQ down {qqq_chg:.2f}% intraday")
    else:
        # After hours: check futures
        fut_chg = _get_futures_change()
        readings["futures_pct"] = fut_chg
        if fut_chg is not None:
            if fut_chg <= FUTURES_ORANGE:
                level = max(level, "ORANGE",
                            key=lambda l: ["GREEN","YELLOW","ORANGE","RED"].index(l))
                triggers.append(f"S&P futures down {fut_chg:.2f}% overnight")
            elif fut_chg <= FUTURES_YELLOW:
                level = max(level, "YELLOW",
                            key=lambda l: ["GREEN","YELLOW","ORANGE","RED"].index(l))
                triggers.append(f"S&P futures down {fut_chg:.2f}% overnight")

    # ── VIX ───────────────────────────────────────────────────────────────
    vix_level, vix_spike = _get_vix()
    readings["vix_level"]  = vix_level
    readings["vix_change"] = vix_spike

    _levels = ["GREEN", "YELLOW", "ORANGE", "RED"]
    # When VIX data is unavailable during market hours, raise to YELLOW only.
    # ORANGE was too aggressive — a Yahoo Finance blip would launch a full emergency
    # cascade (CRO → Review → Manager → Board) on a data-feed failure, not a crash.
    # YELLOW logs the gap and increases monitoring sensitivity without waking agents.
    # If the feed is down for multiple consecutive runs, other triggers (SPY, QQQ,
    # positions, sector breadth) will still fire at appropriate levels independently.
    if vix_level is None and market_open:
        level = max(level, "YELLOW", key=lambda l: _levels.index(l))
        triggers.append("VIX data unavailable — raising to YELLOW (data feed issue, not a crash signal)")

    if vix_level is not None:
        if vix_level >= VIX_RED:
            level = max(level, "RED", key=lambda l: _levels.index(l))
            triggers.append(f"VIX at {vix_level:.1f} (threshold: {VIX_RED})")
        elif vix_level >= VIX_ORANGE:
            level = max(level, "ORANGE", key=lambda l: _levels.index(l))
            triggers.append(f"VIX at {vix_level:.1f} — elevated fear")
        elif vix_level >= VIX_YELLOW:
            level = max(level, "YELLOW", key=lambda l: _levels.index(l))
            triggers.append(f"VIX at {vix_level:.1f} — market stress building")

    if vix_spike is not None:
        if vix_spike >= VIX_SPIKE_RED:
            level = max(level, "RED", key=lambda l: _levels.index(l))
            triggers.append(f"VIX spiked +{vix_spike:.1f}% today (panic signal)")
        elif vix_spike >= VIX_SPIKE_ORANGE:
            level = max(level, "ORANGE", key=lambda l: _levels.index(l))
            triggers.append(f"VIX up +{vix_spike:.1f}% today")

    # ── Open positions ─────────────────────────────────────────────────────
    if market_open:
        positions = _get_open_positions_changes()
        readings["positions"] = positions
        for pos in positions:
            pnl = pos["pnl_pct"]
            if pnl <= POSITION_RED:
                level = max(level, "RED", key=lambda l: _levels.index(l))
                triggers.append(
                    f"POSITION ALERT: {pos['ticker']} down {pnl:.1f}% today"
                )
            elif pnl <= POSITION_ORANGE:
                level = max(level, "ORANGE", key=lambda l: _levels.index(l))
                triggers.append(
                    f"Position pressure: {pos['ticker']} down {pnl:.1f}%"
                )

    # ── Sector breadth (during market hours) ──────────────────────────────
    if market_open:
        neg_sectors = _count_negative_sectors()
        readings["negative_sectors"] = neg_sectors
        if neg_sectors >= 9:     # 9 of 11 sectors red
            level = max(level, "ORANGE", key=lambda l: _levels.index(l))
            triggers.append(f"Broad market selloff: {neg_sectors}/{len(SECTOR_ETFS)} sectors negative")
        elif neg_sectors >= 7:
            level = max(level, "YELLOW", key=lambda l: _levels.index(l))
            triggers.append(f"Wide sector weakness: {neg_sectors}/{len(SECTOR_ETFS)} sectors negative")

    # ── Regime field (CIT-2026-203) ─────────────────────────────────────────
    # Maps canary alert level → strategic regime label consumed by R&D leader
    # and Manager Agent to shift research mandate and position sizing.
    _regime_map = {
        "GREEN":  "BULL",
        "YELLOW": "NEUTRAL",
        "ORANGE": "DEFENSIVE",
        "RED":    "CRISIS",
    }
    regime = _regime_map.get(level, "NEUTRAL")

    return {
        "level":       level,
        "regime":      regime,
        "triggers":    triggers,
        "readings":    readings,
        "assessed_at": datetime.now().isoformat(),
        "market_open": market_open,
    }


# =============================================================================
# ACTIONS
# =============================================================================

def _llm_stress_assessment(assessment: dict) -> Optional[str]:
    """Fable 5 market-stress read — only called on YELLOW/ORANGE/RED alerts."""
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("canary_agent"):
            return None
        triggers = "; ".join(assessment.get("triggers", [])[:4])
        readings = assessment.get("readings", {})
        ctx = (
            f"Canary {assessment['level']} | Triggers: {triggers}\n"
            f"Readings: SPY {readings.get('spy_change','?')}%, VIX {readings.get('vix','?')}\n"
            "2 sentences: is this transient noise or the start of real stress? "
            "What should a defensive paper-trading firm do in the next 24h?"
        )
        return _r("You are a market-stress analyst for Apex Capital.",
                  ctx, agent_name="canary_agent", max_tokens=120)
    except Exception:
        return None


# Levels that mean "the firm should be in defensive mode". YELLOW is a
# heads-up, not a gate.
ALERTING_LEVELS = ("RED", "ORANGE")

# How long an alert may sit `active` before we say so out loud. Not an expiry —
# see the comment at the bottom of load_active_alert().
STALE_ACTIVE_ALERT_HOURS = 24.0


def load_active_alert(path: Optional[str] = None) -> dict:
    """Is the canary alerting RIGHT NOW? The one supported way to ask.

    Returns the live alert record, or `{}` when the canary is not alerting.

    THE ESCAPE (found 2026-08-27). `_clear_alert_file` below flips `active` to
    False and deliberately leaves `level` where it was: the record is the
    *history* of an episode, and `triggered_at` / `triggers` / `readings` all
    describe the ORANGE that happened. So a cleared record reads
    `{"active": False, "level": "ORANGE", "cleared_at": ...}` — and every field
    in it is correct.

    `main.py::_load_canary_alert` tested `level in ("RED","ORANGE")` and never
    read `active`. Its own docstring said "active"; its code did not. An ORANGE
    that lived fifteen minutes on 2026-08-24 (13:30:10 → 13:45:07) therefore
    held the firm in DEFENSIVE MODE for the next three days — 1,496 "all new
    capital deployment blocked" decisions and 2 SELL fast-tracks — while
    `canary_state.json` read GREEN the entire time.

    It was not one reader's slip. Four modules read this file and three had
    each invented a different contract against it:

      main.py            `level`, never `active`      → the three days above
      morning_briefing   `level`, never `active`      → "🚨 emergency cascade
                                                        active" in Jack's
                                                        briefing for 3 days
      morning_briefing   `alerts`, `generated_at`     → keys this module has
                                                        never written, so
                                                        alert_count was always
                                                        0 and last_updated ""
      etf_pod_board      `alerts` / `active_alerts`   → same; measured
                                                        2026-08-27, its canary
                                                        injection had fired
                                                        ZERO times, ever
      deputy_agents      `active` AND `level`         → correct

    Four readers, one file, no shared definition of "alerting". So liveness is
    decided here, once, and a reader that wants the level asks this function
    for it. `TestCanaryAlertHasOneReader` pins that every behavioural reader
    routes through here and that every key they take off the result is a key
    `_write_alert_file` actually writes.

    `active is True` is exact on purpose. This module writes a JSON bool and
    nothing else, so anything truthy-but-not-True is corruption — and a record
    corrupt enough to hold a string in `active` cannot be trusted to hold a
    real `level` either. Corruption is logged, never silently resolved: this
    gate both blocks buying (safe to get wrong) and force-sells (not safe to
    get wrong), so there is no conservative constant to fall back on.
    """
    p = path or ALERT_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            alert = json.load(fh)
    except Exception as exc:
        logger.warning("  [Canary] %s unreadable (%s) — treating as no alert", p, exc)
        return {}
    if not isinstance(alert, dict):
        logger.warning("  [Canary] %s is not an object — treating as no alert", p)
        return {}

    active = alert.get("active")
    if active is not True:
        if active is not None and not isinstance(active, bool):
            logger.warning(
                "  [Canary] %s has a non-boolean `active` (%r) — treating as no "
                "alert, but this file is corrupt and its `level` cannot be "
                "trusted either", p, active)
        return {}
    if alert.get("level") not in ALERTING_LEVELS:
        return {}

    # The second way this gate can stick on: only the canary clears the file,
    # so if the canary stops running, an active alert is never cleared and
    # defensive mode runs forever on a stale reading. Do NOT expire it here.
    # "The canary is down" is not "the market is fine" — inventing an all-clear
    # is the same class of mistake as the three days this docstring opens with,
    # pointed the other way. Say it loudly and let the freshness sweep own it.
    ts = alert.get("triggered_at")
    if isinstance(ts, str):
        try:
            age_h = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600.0
        except ValueError:
            age_h = None
        if age_h is not None and age_h > STALE_ACTIVE_ALERT_HOURS:
            logger.warning(
                "  [Canary] alert has been ACTIVE %.1fh without being cleared or "
                "re-fired (level %s, triggered %s). Only the canary clears this "
                "file — if it has stopped running, defensive mode is stuck on. "
                "NOT auto-clearing: a down canary is not a GREEN market.",
                age_h, alert.get("level"), ts)
    return alert


def _write_alert_file(assessment: dict):
    """Save active alert so main.py picks it up on next scheduled run."""
    alert = {
        "active":       True,
        "level":        assessment["level"],
        "triggered_at": assessment["assessed_at"],
        "triggers":     assessment["triggers"],
        "readings":     assessment["readings"],
    }
    llm_read = _llm_stress_assessment(assessment)
    if llm_read:
        alert["llm_assessment"] = llm_read.strip()
    os.makedirs("data", exist_ok=True)
    with open(ALERT_PATH, "w") as f:
        json.dump(alert, f, indent=2, default=str)

    # Broadcast market stress to firm channel immediately
    try:
        from firm_channel import post as _fc_post
        _level   = assessment["level"]
        _triggers = "; ".join(assessment.get("triggers", [])[:3])
        _fc_post(
            "Canary Agent", "MARKET_NOTE",
            f"🚨 CANARY {_level} — {_triggers}. All BUY orders blocked. Defensive mode active.",
            priority="URGENT",
        )
    except Exception:
        pass


def _clear_alert_file():
    """Clear alert file when level returns to GREEN."""
    if os.path.exists(ALERT_PATH):
        try:
            with open(ALERT_PATH) as f:
                existing = json.load(f)
            if existing.get("active"):
                existing["active"]    = False
                existing["cleared_at"] = datetime.now().isoformat()
                with open(ALERT_PATH, "w") as f:
                    json.dump(existing, f, indent=2)
        except Exception:
            pass


def _wake_manager_agent(assessment: dict):
    """
    Immediately run the full Manager Agent pipeline with canary context.
    The Manager Agent will see the canary_triggered flag and apply
    defensive decision logic (prioritise SELL, block new BUYs).
    Triggered on both ORANGE and RED alerts.
    """
    level = assessment.get("level", "RED")
    logger.info(f"  [Canary] {level} ALERT — waking Manager Agent NOW")
    try:
        from manager_agent import run_manager
        run_manager(
            dry_run=False,
            skip_review=False,
            skip_email=False,
            canary_alert=assessment,
        )
        logger.info("  [Canary] Manager Agent emergency run complete")
    except Exception as e:
        logger.error(f"  [Canary] Failed to wake Manager Agent: {e}")


def _enforce_canary_exits(assessment: dict):
    """
    THE LOOP-BREAKER — structural fix for alert fatigue (board directive 2026-05-01).

    After the Manager Agent runs in the cascade, any position that originally
    triggered the canary alert may still be open if the Manager Agent
    deprioritised it, ran into a pipeline ordering issue, or hit a transient
    execution error.

    This function:
      1. Reads the positions that triggered the current alert
      2. Checks whether those positions are still open in Alpaca
      3. For any that remain open AND still breach their threshold,
         submits a direct market-close order — bypassing the normal
         deliberation pipeline entirely
      4. Logs every forced exit so the Chairman and board can review

    This is why the canary fired 23 times on BA and SH: the Manager Agent
    ran each time but these two positions stayed open. With this step live,
    the first RED/ORANGE fire resolves the triggering position on the spot.
    """
    triggers   = assessment.get("triggers", [])
    positions  = assessment.get("readings", {}).get("positions", [])
    level      = assessment.get("level", "RED")
    forced     = []

    if not positions:
        logger.info("  [Canary] Step 3b — no position data in alert; skipping force-exit")
        return

    try:
        from alpaca_trader import AlpacaTrader
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        trader        = AlpacaTrader()
        open_positions = {p.symbol: p for p in trader.client.get_all_positions()}

        for pos in positions:
            ticker  = pos.get("ticker", "")
            pnl_pct = float(pos.get("pnl_pct", 0))

            # Only act on positions that appear in the trigger message
            in_trigger = any(ticker in t for t in triggers)
            if not in_trigger:
                continue

            # Is it still open?
            alpaca_pos = open_positions.get(ticker)
            if not alpaca_pos:
                logger.info(f"  [Canary] {ticker}: already closed — no force-exit needed")
                continue

            qty     = int(float(alpaca_pos.qty))
            is_long = qty > 0

            # Determine if threshold still breached
            stop_breach = pnl_pct <= POSITION_RED       # -8% on a long
            take_profit = pnl_pct >= 30.0               # significantly past target on a long
            short_stop  = pnl_pct <= POSITION_RED       # short position running against us

            needs_exit = stop_breach or (is_long and take_profit)

            if needs_exit:
                side    = OrderSide.SELL if is_long else OrderSide.BUY
                abs_qty = abs(qty)
                reason  = (
                    "canary_forced_stop_exit" if stop_breach
                    else "canary_forced_takeprofit_exit"
                )
                try:
                    req   = MarketOrderRequest(
                        symbol=ticker, qty=abs_qty, side=side,
                        time_in_force=TimeInForce.DAY,
                    )
                    order = trader.client.submit_order(req)
                    forced.append(ticker)
                    logger.warning(
                        f"  [Canary] ⚡ FORCED EXIT: {ticker} qty={abs_qty} "
                        f"({side.value.upper()}) pnl={pnl_pct:+.1f}% reason={reason} "
                        f"order_id={str(order.id)[:8]}"
                    )
                except Exception as ex:
                    logger.error(f"  [Canary] Force-exit order failed for {ticker}: {ex}")
            else:
                logger.info(
                    f"  [Canary] {ticker} still open (pnl={pnl_pct:+.1f}%) "
                    f"but threshold no longer breached — leaving open"
                )

    except Exception as e:
        logger.error(f"  [Canary] Force-exit enforcement failed: {e}")
        return

    if forced:
        logger.warning(
            f"  [Canary] ✓ Force-closed {len(forced)} position(s): {', '.join(forced)}. "
            f"Alert-fatigue loop broken."
        )
    else:
        logger.info(
            "  [Canary] ✓ All triggering positions resolved by Manager Agent — "
            "no forced exits needed."
        )


def _emergency_response_cascade(assessment: dict):
    """
    Full emergency response cascade — triggered on ORANGE and RED alerts.

    Sequence (order matters — each step feeds the next):

      1. CRO  — publish a fresh risk bulletin with current market data.
                The board meeting and Manager Agent both read from this.

      2. Review Agent — emergency position scan. Checks all open positions
                for stop-loss proximity, concentration breaches, and any
                names that should be exited immediately given the alert.

      3. Manager Agent — defensive execution with canary context injected.
                Prioritises risk reduction: sells, trims, stops. Blocks
                new BUY orders while the alert is active.

      4. Board Meeting — anonymous deliberation. The CRO bulletin and
                review output are anonymised and distributed to all four
                senior agents (Manager, CoS, CRO, IRM). Consensus is
                voted on and additional actions are implemented immediately.
                Chairman receives full meeting minutes.

      5. IRM  — independent governance check (RED only). The Independent
                Risk Manager runs its own assessment and reports to the
                Chairman separately, providing a second opinion on whether
                the board's response was adequate.

    Alert-level routing:
      RED    → all five steps
      ORANGE → steps 1–4 (IRM step skipped for speed; board covers oversight)

    All steps are wrapped individually — a failure in one step never
    prevents the remaining steps from running.
    """
    level = assessment.get("level", "RED")
    triggers = assessment.get("triggers", [])
    logger.info(f"\n  [Canary] ═══ EMERGENCY RESPONSE CASCADE — {level} ═══")
    logger.info(f"  [Canary] Triggers: {'; '.join(triggers)}")

    # ── Step 1: CRO — fresh risk bulletin ────────────────────────────────
    logger.info("  [Canary] Step 1/5 — CRO: publishing emergency risk bulletin...")
    try:
        from risk_manager_agent import run_risk_manager
        run_risk_manager(send_email=True)
        logger.info("  [Canary] ✓ CRO emergency bulletin published")
    except Exception as e:
        logger.error(f"  [Canary] CRO step failed: {e}")

    # ── Step 2: Review Agent — emergency position scan ───────────────────
    logger.info("  [Canary] Step 2/5 — Review Agent: emergency position scan...")
    try:
        from review_agent import run_review
        run_review(quiet=True)
        logger.info("  [Canary] ✓ Review Agent scan complete")
    except Exception as e:
        logger.error(f"  [Canary] Review Agent step failed: {e}")

    # ── Step 3: Manager Agent — defensive execution ──────────────────────
    logger.info("  [Canary] Step 3/5 — Manager Agent: defensive execution...")
    try:
        from manager_agent import run_manager
        run_manager(
            dry_run=False,
            skip_review=False,
            skip_email=False,
            canary_alert=assessment,
        )
        logger.info("  [Canary] ✓ Manager Agent emergency run complete")
    except Exception as e:
        logger.error(f"  [Canary] Manager Agent step failed: {e}")

    # ── Step 3b: Force-close canary-triggering positions ─────────────────
    # THE LOOP-BREAKER. After the Manager Agent runs, verify that every
    # position that triggered this alert has actually been closed.
    # If any remain open and still breach their threshold, close them
    # directly — no further deliberation. This prevents the same alert
    # from firing again next cycle on the same unresolved position.
    logger.info("  [Canary] Step 3b/5 — Enforcing exits on triggering positions...")
    _enforce_canary_exits(assessment)

    # ── Step 4: Board Meeting — anonymous deliberation ───────────────────
    logger.info("  [Canary] Step 4/5 — Board Meeting: emergency convening...")
    try:
        from board_meeting import run_board_meeting
        board_result = run_board_meeting(dry_run=False, send_email=True)
        consensus = board_result.get("consensus", {})
        logger.info(
            f"  [Canary] ✓ Board meeting complete — "
            f"{consensus.get('unanimous_count', 0)} unanimous, "
            f"{consensus.get('majority_count', 0)} majority, "
            f"{consensus.get('vetoed_count', 0)} vetoed"
        )
    except Exception as e:
        logger.error(f"  [Canary] Board meeting step failed: {e}")

    # ── Step 5: IRM — independent oversight (RED only) ───────────────────
    if level == "RED":
        logger.info("  [Canary] Step 5/5 — IRM: independent governance check (RED)...")
        try:
            from irm_review_agent import run_irm_daily
            irm_result = run_irm_daily(send_email=True)
            bp = irm_result.get("board_panel", {})
            logger.info(
                f"  [Canary] ✓ IRM check complete — "
                f"flags: {len(bp.get('combined_red_flags', []))}, "
                f"verdict: {bp.get('board_verdict', '—')[:50]}"
            )
        except Exception as e:
            logger.error(f"  [Canary] IRM step failed: {e}")
    else:
        logger.info("  [Canary] Step 5/5 — IRM: skipped (ORANGE; board provides oversight)")

    logger.info(f"  [Canary] ═══ CASCADE COMPLETE — {level} ═══\n")


def _send_alert_email(assessment: dict, state: dict):
    """Email Chairman with an urgent canary alert."""
    try:
        from email_sender import send_daily_report
        from creative_agent import polish_email

        level    = assessment["level"]
        triggers = assessment["triggers"]
        readings = assessment["readings"]
        now      = datetime.now().strftime("%B %d, %Y at %H:%M")

        level_colors = {
            "RED":    "#f85149",
            "ORANGE": "#f0883e",
            "YELLOW": "#f0c000",
            "GREEN":  "#3fb950",
        }
        lc = level_colors.get(level, "#58a6ff")

        trigger_list = "".join(
            f'<li style="color:#f0883e;margin-bottom:6px;">{t}</li>'
            for t in triggers
        )

        vix     = readings.get("vix_level")
        spy_chg = readings.get("spy_pct") or readings.get("futures_pct")
        neg_sec = readings.get("negative_sectors", "—")
        mkt     = "OPEN" if readings.get("market_open") else "CLOSED"

        action_taken = (
            "Manager Agent has been woken immediately and is executing risk reduction in Defensive Mode."
            if level in ("RED", "ORANGE") else
            "Canary on heightened alert. No immediate Manager action at YELLOW."
        )

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:900px;margin:0 auto;">
  <div style="border-left:4px solid {lc};padding-left:20px;margin-bottom:24px;">
    <h1 style="color:{lc};margin:0 0 8px 0;font-size:28px;">
      ⚑ CANARY ALERT — {level}
    </h1>
    <p style="color:#8b949e;margin:0;font-size:13px;">{now} &nbsp;·&nbsp; Market: {mkt}</p>
  </div>

  <div style="background:#161b22;border:1px solid {lc};border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:24px;flex-wrap:wrap;">
    {'<div><div style="color:#8b949e;font-size:11px;text-transform:uppercase;">SPY / Futures</div>'
     f'<div style="font-size:20px;font-weight:bold;color:{lc};">{spy_chg:+.2f}%</div></div>'
     if spy_chg is not None else ''}
    {'<div><div style="color:#8b949e;font-size:11px;text-transform:uppercase;">VIX</div>'
     f'<div style="font-size:20px;font-weight:bold;color:{lc};">{vix:.1f}</div></div>'
     if vix is not None else ''}
    {'<div><div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Sectors Negative</div>'
     f'<div style="font-size:20px;font-weight:bold;color:{lc};">{neg_sec}/{len(SECTOR_ETFS)}</div></div>'
     if isinstance(neg_sec, int) else ''}
  </div>

  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:10px;">Triggers</h2>
  <ul style="background:#161b22;border:1px solid #30363d;border-radius:8px;
             padding:16px 16px 16px 36px;margin-bottom:24px;line-height:2.0;">
    {trigger_list or '<li style="color:#8b949e;">No specific triggers — general stress conditions.</li>'}
  </ul>

  <div style="background:#21262d;border-radius:8px;padding:14px 18px;margin-bottom:24px;">
    <p style="color:#58a6ff;font-weight:bold;margin:0 0 6px 0;">Action Taken</p>
    <p style="color:#c9d1d9;margin:0;">{action_taken}</p>
  </div>

  <p style="color:#484f58;font-size:11px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital Intelligence — Canary Agent &nbsp;·&nbsp;
    24/7 market monitoring &nbsp;·&nbsp; {now}
  </p>
</body></html>"""

        # Fable 5 LLM market analysis for ORANGE/RED alerts
        llm_analysis = None
        if level in ("RED", "ORANGE"):
            try:
                from apex_llm import reason as _llm_r, is_available as _llm_ok
                if _llm_ok("canary_agent"):
                    _triggers_text = "; ".join(triggers[:5])
                    _analysis = _llm_r(
                        "You are the Apex Capital Canary Agent — a market stress monitor. "
                        "Write sharp, actionable market commentary for urgent alerts. "
                        "Be specific about what the readings mean and what to watch.",
                        f"Alert level: {level}\n"
                        f"SPY/Futures: {spy_chg:+.2f}% | VIX: {vix:.1f} | "
                        f"Negative sectors: {neg_sec}/{len(SECTOR_ETFS)} | Market: {mkt}\n"
                        f"Triggers: {_triggers_text}\n\n"
                        "Write 2-3 sentences: what is driving this move, what it means for "
                        "the portfolio, and the single most important thing to watch.",
                        agent_name="canary_agent",
                        max_tokens=200,
                    )
                    if _analysis:
                        llm_analysis = _analysis.strip()
            except Exception:
                pass

        if llm_analysis:
            html = html.replace(
                '<div style="background:#21262d;border-radius:8px;padding:14px 18px;margin-bottom:24px;">'
                f'\n    <p style="color:#58a6ff;font-weight:bold;margin:0 0 6px 0;">Action Taken</p>'
                f'\n    <p style="color:#c9d1d9;margin:0;">{action_taken}</p>'
                '\n  </div>',
                f'<div style="background:#21262d;border-radius:8px;padding:14px 18px;margin-bottom:16px;">'
                f'\n    <p style="color:#58a6ff;font-weight:bold;margin:0 0 6px 0;">Market Analysis (Fable 5)</p>'
                f'\n    <p style="color:#c9d1d9;margin:0;">{llm_analysis}</p>'
                f'\n  </div>'
                f'\n  <div style="background:#21262d;border-radius:8px;padding:14px 18px;margin-bottom:24px;">'
                f'\n    <p style="color:#58a6ff;font-weight:bold;margin:0 0 6px 0;">Action Taken</p>'
                f'\n    <p style="color:#c9d1d9;margin:0;">{action_taken}</p>'
                f'\n  </div>',
            )

        subject = (
            f"⚑ CANARY {level} — "
            + (f"SPY {spy_chg:+.1f}% " if spy_chg else "")
            + (f"VIX {vix:.0f} " if vix else "")
            + f"| {datetime.now().strftime('%b %d %H:%M')}"
        )

        key_metrics = {
            "Alert Level": level,
            "Market":      mkt,
        }
        if spy_chg is not None:
            key_metrics["SPY/Futures"] = f"{spy_chg:+.2f}%"
        if vix is not None:
            key_metrics["VIX"] = f"{vix:.1f}"

        polished = polish_email(html, subject, "canary", key_metrics, priority="urgent")
        send_daily_report(html_body=polished, subject=subject, paper_day=0,
                          sender_name="canary")
        logger.info(f"  [Canary] Alert email sent — level: {level}")
    except Exception as e:
        logger.warning(f"  [Canary] Email error: {e}")


# =============================================================================
# MAIN CANARY LOOP
# =============================================================================

def run_canary(test_level: Optional[str] = None) -> dict:
    """
    Single canary check cycle. Called by cron every 5–30 minutes.
    Returns the assessment dict.
    """
    os.makedirs("data", exist_ok=True)
    state = _load_state()

    # ── Assess current conditions ────────────────────────────────────────
    if test_level:
        # Inject a simulated alert level for testing
        assessment = {
            "level":       test_level,
            "triggers":    [f"[TEST] Simulated {test_level} alert"],
            "readings":    {"market_open": _is_market_open()},
            "assessed_at": datetime.now().isoformat(),
            "market_open": _is_market_open(),
        }
        logger.info(f"  [Canary] TEST MODE — simulating {test_level} alert")
    else:
        assessment = assess_alert_level()

    level        = assessment["level"]
    prev_level   = state.get("last_level", "GREEN")
    _levels      = ["GREEN", "YELLOW", "ORANGE", "RED"]
    now_iso      = datetime.now().isoformat()

    logger.info(
        f"  [Canary] Level: {level} (was: {prev_level}) | "
        f"Market: {'OPEN' if assessment['market_open'] else 'CLOSED'} | "
        f"Triggers: {len(assessment['triggers'])}"
    )
    if assessment["triggers"]:
        for t in assessment["triggers"]:
            logger.info(f"    ⚑ {t}")

    # ── React based on level ──────────────────────────────────────────────

    if level == "GREEN":
        if prev_level not in ("GREEN", None):
            logger.info(f"  [Canary] Market recovered to GREEN from {prev_level}")
            _clear_alert_file()
            # Send "all clear" only if previous was ORANGE or RED
            if prev_level in ("ORANGE", "RED"):
                try:
                    from email_sender import send_daily_report
                    from creative_agent import polish_email
                    msg = (f"Market conditions have normalised. "
                           f"Previous alert level was {prev_level}. No further action needed.")
                    html = f"""<!DOCTYPE html><html><body style="background:#0d1117;
                        color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;padding:24px;">
                        <h2 style="color:#3fb950;">✓ Canary All Clear</h2>
                        <p>{msg}</p>
                        <p style="color:#8b949e;font-size:12px;">{datetime.now().strftime("%B %d, %Y at %H:%M")}</p>
                        </body></html>"""
                    subject = f"✓ Canary ALL CLEAR — {datetime.now().strftime('%b %d %H:%M')}"
                    polished = polish_email(html, subject, "canary",
                                           {"Alert Level": "ALL CLEAR"})
                    send_daily_report(html_body=polished, subject=subject, paper_day=0,
                                      sender_name="canary")
                except Exception:
                    pass

    elif level == "YELLOW":
        if not _in_cooldown(state, "YELLOW"):
            state["last_yellow_time"] = now_iso
            logger.info("  [Canary] YELLOW — logged, no immediate action")
            _write_alert_file(assessment)

    elif level == "ORANGE":
        if not _in_cooldown(state, "ORANGE"):
            state["last_orange_time"] = now_iso
            logger.info(
                "  [Canary] ORANGE — alerting Chairman, launching emergency response cascade "
                "(CRO → Review → Manager → Board)"
            )
            _write_alert_file(assessment)
            # Log to issue tracker
            try:
                import issue_tracker as _it
                _trigger = "; ".join(assessment.get("triggers", [])[:3])
                _summary = f"Canary ORANGE alert. Triggers: {_trigger}. Emergency cascade launched."
                _it.log_issue(
                    reporter="Canary Agent",
                    title=f"Market alert: ORANGE — {_trigger[:80]}",
                    description=_summary,
                    severity="HIGH",
                    category="RISK",
                )
            except Exception as _it_err:
                logger.debug(f"  [Canary] Issue tracker log error (non-fatal): {_it_err}")
            # ORANGE: internal cascade only — no Chairman email (RED only reaches Chairman)
            _emergency_response_cascade(assessment)
            state["alerts_today"] = state.get("alerts_today", []) + [
                {"level": "ORANGE", "time": now_iso,
                 "triggers": assessment["triggers"]}
            ]
            state["manager_triggers"] = state.get("manager_triggers", []) + [
                {"time": now_iso, "level": "ORANGE", "triggers": assessment["triggers"]}
            ]
        else:
            logger.info("  [Canary] ORANGE in cooldown — skipping")

    elif level == "RED":
        if not _in_cooldown(state, "RED"):
            state["last_red_time"] = now_iso
            logger.info(
                "  [Canary] RED — launching FULL emergency response cascade "
                "(CRO → Review → Manager → Board → IRM)"
            )
            _write_alert_file(assessment)
            # Log to issue tracker
            try:
                import issue_tracker as _it
                _trigger = "; ".join(assessment.get("triggers", [])[:3])
                _summary = f"Canary RED alert. Triggers: {_trigger}. Full emergency cascade launched."
                _it.log_issue(
                    reporter="Canary Agent",
                    title=f"Market alert: RED — {_trigger[:80]}",
                    description=_summary,
                    severity="CRITICAL",
                    category="RISK",
                )
            except Exception as _it_err:
                logger.debug(f"  [Canary] Issue tracker log error (non-fatal): {_it_err}")
            _send_alert_email(assessment, state)
            _emergency_response_cascade(assessment)

            # Call emergency board meeting for RED alerts
            try:
                from board_meeting import call_emergency_board_meeting
                _triggers_str = "; ".join(assessment.get("triggers", [])[:3])
                call_emergency_board_meeting(
                    reason=f"Market alert: RED — {_triggers_str}",
                    severity="CRITICAL",
                    caller="Canary Agent",
                    anonymous=False,
                )
            except Exception as _ebm_e:
                logger.error(f"  [Canary] Emergency board meeting call failed: {_ebm_e}")

            # If board meeting active, also run deputy manager
            try:
                from deputy_agents import is_board_meeting_active, DeputyManager
                if is_board_meeting_active():
                    logger.info("  [Canary] RED + board meeting active — running DeputyManager")
                    DeputyManager().run()
            except Exception as _dep_e:
                logger.debug(f"  [Canary] Deputy manager error (non-fatal): {_dep_e}")

            state["manager_triggers"] = state.get("manager_triggers", []) + [
                {"time": now_iso, "triggers": assessment.get("triggers", [])}
            ]
        else:
            mins = COOLDOWN_RED - _minutes_since(state.get("last_red_time"))
            logger.info(f"  [Canary] RED in cooldown ({mins:.0f} min remaining) — skipping")

    # ── Update state ──────────────────────────────────────────────────────
    state["last_level"]      = level
    state["last_level_time"] = now_iso
    # Persist regime field for R&D leader and other consumers (CIT-2026-203)
    state["regime"] = assessment.get("regime", "NEUTRAL")
    # Persist key market readings so downstream agents (CIT, board) can read them
    # BUG FIX: use assessment["readings"] — bare `readings` and `market_open` are
    # not in scope here; they only exist inside assess_alert_level().
    _last_readings = assessment.get("readings", {})
    state["last_readings"] = {
        "spy_pct":    _last_readings.get("spy_pct"),
        "qqq_pct":    _last_readings.get("qqq_pct"),
        "vix_level":  _last_readings.get("vix_level"),
        "vix_change": _last_readings.get("vix_change"),
        "market_open": assessment.get("market_open"),
        "updated_at":  now_iso,
    }
    _save_state(state)

    return assessment


# =============================================================================
# STATUS PRINTER
# =============================================================================

def print_status():
    state = _load_state()
    assessment = assess_alert_level()
    level_icons = {"GREEN": "✓", "YELLOW": "~", "ORANGE": "⚑", "RED": "✗"}

    print(f"\n{'═'*55}")
    print("  APEX CAPITAL — CANARY AGENT STATUS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*55}")
    print(f"\n  Current Level : {level_icons.get(assessment['level'],'')} {assessment['level']}")
    print(f"  Last Level    : {state.get('last_level','—')}")
    print(f"  Market        : {'OPEN' if assessment['market_open'] else 'CLOSED'}")

    r = assessment["readings"]
    if r.get("spy_pct") is not None:
        print(f"  SPY Today     : {r['spy_pct']:+.2f}%")
    if r.get("futures_pct") is not None:
        print(f"  S&P Futures   : {r['futures_pct']:+.2f}%")
    if r.get("vix_level") is not None:
        print(f"  VIX           : {r['vix_level']:.1f}  ({r.get('vix_change',0):+.1f}% today)")
    if isinstance(r.get("negative_sectors"), int):
        print(f"  Neg Sectors   : {r['negative_sectors']}/{len(SECTOR_ETFS)}")

    if assessment["triggers"]:
        print(f"\n  Triggers:")
        for t in assessment["triggers"]:
            print(f"    ⚑ {t}")
    else:
        print("\n  No triggers — all clear")

    last_red = state.get("last_red_time")
    if last_red:
        print(f"\n  Last RED      : {last_red}")
        print(f"  Cooldown left : {max(0, COOLDOWN_RED - _minutes_since(last_red)):.0f} min")

    triggers_today = len(state.get("manager_triggers", []))
    if triggers_today:
        print(f"\n  Manager wakes today: {triggers_today}")
    print(f"\n{'═'*55}\n")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    # ── Single-instance guard ─────────────────────────────────────────────────
    # Prevents multiple copies piling up when cron fires faster than the agent
    # finishes (Claude API calls can take 1-3 min; cron fires every 5 min).
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('Canary'):
        raise SystemExit(0)
    _LOCK_PATH = "/tmp/canary_agent.lock"
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another instance is already running — exit immediately
        sys.exit(0)

    # ── Hard timeout: 4 min 30 sec — always exits before next cron tick ──────
    def _timeout_handler(signum, frame):
        logging.getLogger(__name__).warning("  [Canary] Hard timeout reached — exiting cleanly.")
        sys.exit(0)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(270)  # 4 min 30 sec

    if "--status" in sys.argv:
        print_status()
    elif "--test-red" in sys.argv:
        run_canary(test_level="RED")
    elif "--test-orange" in sys.argv:
        run_canary(test_level="ORANGE")
    elif "--test-yellow" in sys.argv:
        run_canary(test_level="YELLOW")
    else:
        run_canary()
