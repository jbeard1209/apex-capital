"""
APEX CAPITAL — ETF RESEARCH AGENT
===================================
Pre-meeting intelligence brief for the ETF Pod 3 Board.

Runs 15 minutes after R&D completes (so R&D data is fresh).
Pulls live price/momentum data from yfinance for each ETF in the universe,
enriches it with regime context and geopolitical flags, then writes a clean
research brief to data/etf_research_brief.json.

The ETF Pod Board (etf_pod_board.py) reads this file to deliberate and vote.

ETF universe: XLK, XLE, XLF, XLU, XLP, XLV, XLI, GLD, TLT, QQQ, XLY, XLB
"""

import json
import fcntl
import logging
import tempfile
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent
DATA = BASE / "data"

SECTOR_PATH     = DATA / "sector_assignments.json"
BRIDGE_PATH     = DATA / "rd_ml_bridge_signals.json"
HIST_PATH       = DATA / "rd_historical_context.json"
GEO_PATH        = DATA / "rd_geopolitical_context.json"
BRIEF_PATH      = DATA / "etf_research_brief.json"
LOCK_FILE       = DATA / ".etf_research_agent.lock"

#: Latest-state outputs. Each run computes a fresh picture of ETF research —
#: losing an intermediate generation is sampling, not destruction. Read by
#: pod_health_monitor on a slower cadence; that is intentional. See
#: wiring_audit._declared_snapshots.
_WIRING_SNAPSHOT = {"etf_research_brief.json"}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ETF_UNIVERSE: List[str] = [
    "XLK", "XLE", "XLF", "XLU", "XLP", "XLV",
    "XLI", "GLD", "TLT", "QQQ", "XLY", "XLB",
]

# Map ETF tickers to sector names (for geopolitical flag lookup)
ETF_SECTOR_MAP: Dict[str, str] = {
    "XLK": "Technology",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "GLD": "Commodities",
    "TLT": "Bonds",
    "QQQ": "Technology",
    "XLY": "Consumer Discretionary",
    "XLB": "Materials",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ETF_RESEARCH] %(levelname)s %(message)s",
)
log = logging.getLogger("etf_research_agent")


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

class SingleInstanceLock:
    """Prevents multiple simultaneous instances via fcntl.flock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        try:
            self._fh = open(self.lock_path, "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            if self._fh:
                self._fh.close()
            return False

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path, label: str) -> Optional[dict]:
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except FileNotFoundError:
        log.warning("%s not found at %s — skipping", label, path)
        return None
    except json.JSONDecodeError as exc:
        log.error("JSON parse error in %s: %s", label, exc)
        return None


def _load_sector_assignments() -> dict:
    data = _load_json(SECTOR_PATH, "sector_assignments.json")
    return data or {}


def _load_bridge_signals() -> dict:
    data = _load_json(BRIDGE_PATH, "rd_ml_bridge_signals.json")
    return data or {}


def _load_historical_context() -> dict:
    data = _load_json(HIST_PATH, "rd_historical_context.json")
    return data or {}


def _load_geopolitical_context() -> dict:
    data = _load_json(GEO_PATH, "rd_geopolitical_context.json")
    return data or {}


# ---------------------------------------------------------------------------
# Sector assignment index
# ---------------------------------------------------------------------------

def _build_sector_index(sector_data: dict) -> Dict[str, dict]:
    """
    Build {etf_ticker -> score_data} from sector_assignments.json.
    Used as fallback if yfinance fails.
    """
    index: Dict[str, dict] = {}
    for entry in sector_data.get("all_scored", []):
        score_data = entry.get("score_data", {})
        etf = score_data.get("etf", "")
        if etf:
            index[etf] = {
                "sector_name": entry.get("sector_name", "Unknown"),
                "score_data": score_data,
            }
    return index


# ---------------------------------------------------------------------------
# Geopolitical flag lookup
# ---------------------------------------------------------------------------

def _get_geo_risk(etf: str, geo_data: dict) -> bool:
    """
    Return True if this ETF's sector has a geopolitical risk flag.
    Checks both the sector_geopolitical_flags dict and high_sensitivity_sectors list.
    """
    if not geo_data:
        return False

    geo_analysis = geo_data.get("geopolitical_analysis", {})
    sector_name = ETF_SECTOR_MAP.get(etf, "")

    # Check high_sensitivity_sectors
    high_sens = geo_analysis.get("high_sensitivity_sectors", [])
    if etf in high_sens or sector_name in high_sens:
        return True

    # Check sector_geopolitical_flags dict
    flags = geo_analysis.get("sector_geopolitical_flags", {})
    if etf in flags or sector_name in flags:
        return True

    return False


# ---------------------------------------------------------------------------
# yfinance data pull + technical computation
# ---------------------------------------------------------------------------

def _compute_rsi(prices, period: int = 14) -> float:
    """Compute RSI-14 from a price series."""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    # Seed: standard Wilder approach — avg of first `period` deltas,
    # positive contributions for up-days, 0 for down-days (and vice versa).
    # Bug fix: was seeding from positive-only / negative-only pools, which
    # inflated avg_gain and understated avg_loss.
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period

    # Wilder smoothing for remaining
    for i in range(period, len(deltas)):
        g = max(deltas[i], 0.0)
        l = max(-deltas[i], 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _fetch_etf_data_yfinance(etf: str) -> Optional[dict]:
    """
    Fetch 3 months of daily price history from yfinance and compute:
    price_return_1m, price_return_3m, rsi_14, pct_from_50ma,
    pct_from_200ma (approximated from 3mo), volatility_20d, current_price.

    Returns None on any failure.
    """
    try:
        import yfinance as yf  # type: ignore
        ticker = yf.Ticker(etf)
        hist = ticker.history(period="6mo")

        if hist is None or hist.empty or len(hist) < 21:
            log.warning("yfinance: insufficient data for %s (%d rows)", etf, len(hist) if hist is not None else 0)
            return None

        closes = list(hist["Close"])
        n = len(closes)
        current_price = closes[-1]

        # 1-month return (~21 trading days)
        price_1m_ago = closes[max(0, n - 22)]
        return_1m = ((current_price - price_1m_ago) / price_1m_ago * 100.0) if price_1m_ago > 0 else 0.0

        # 3-month return (~63 trading days)
        price_3m_ago = closes[max(0, n - 64)]
        return_3m = ((current_price - price_3m_ago) / price_3m_ago * 100.0) if price_3m_ago > 0 else 0.0

        # RSI-14
        rsi_14 = _compute_rsi(closes[-30:] if n >= 30 else closes)

        # 50-day MA (use last 50 closes; fall back to available)
        ma50_window = closes[max(0, n - 50):]
        ma50 = sum(ma50_window) / len(ma50_window) if ma50_window else current_price
        pct_from_50ma = ((current_price - ma50) / ma50 * 100.0) if ma50 > 0 else 0.0

        # 200-day MA (use full 6-month window as approximation if < 200 days available)
        ma200_window = closes  # up to ~126 trading days in 6 months
        ma200 = sum(ma200_window) / len(ma200_window) if ma200_window else current_price
        pct_from_200ma = ((current_price - ma200) / ma200 * 100.0) if ma200 > 0 else 0.0

        # 20-day annualised volatility
        import math as _math
        last20 = closes[max(0, n - 21):]
        if len(last20) >= 2:
            log_rets = [_math.log(last20[i] / last20[i - 1]) for i in range(1, len(last20)) if last20[i - 1] > 0]
            mean_lr = sum(log_rets) / len(log_rets) if log_rets else 0.0
            variance = sum((r - mean_lr) ** 2 for r in log_rets) / len(log_rets) if log_rets else 0.0
            volatility_20d = round(_math.sqrt(variance) * _math.sqrt(252) * 100.0, 2)
        else:
            volatility_20d = 0.0

        return {
            "current_price": round(current_price, 2),
            "price_return_1m": round(return_1m, 4),
            "price_return_3m": round(return_3m, 4),
            "rsi_14": round(rsi_14, 2),
            "pct_from_50ma": round(pct_from_50ma, 4),
            "pct_from_200ma": round(pct_from_200ma, 4),
            "volatility_20d": round(volatility_20d, 2),
            "data_source": "yfinance",
        }

    except Exception as exc:
        log.warning("yfinance failed for %s: %s", etf, exc)
        return None


def _fetch_spy_1d_return() -> Optional[float]:
    """
    Fetch SPY's 1-day return (today's close vs yesterday's close) from yfinance.
    Returns the percentage return, or None on failure.
    Used to detect intraday market crashes for the emergency exit check.
    """
    try:
        import yfinance as yf  # type: ignore
        ticker = yf.Ticker("SPY")
        hist = ticker.history(period="5d")
        if hist is None or hist.empty or len(hist) < 2:
            log.warning("SPY 1d return: insufficient data (%d rows)", len(hist) if hist is not None else 0)
            return None
        closes = list(hist["Close"])
        prev_close = closes[-2]
        last_close = closes[-1]
        if prev_close <= 0:
            return None
        return_1d = (last_close - prev_close) / prev_close * 100.0
        log.info("SPY 1d return: %.4f%% (prev=%.2f, last=%.2f)", return_1d, prev_close, last_close)
        return round(return_1d, 4)
    except Exception as exc:
        log.warning("SPY 1d return fetch failed: %s", exc)
        return None


def _fallback_from_sector(etf: str, sector_index: Dict[str, dict]) -> Optional[dict]:
    """
    Build ETF data dict from sector_assignments.json if yfinance failed.
    Maps field names from R&D scoring format.
    """
    entry = sector_index.get(etf)
    if not entry:
        return None

    sd = entry["score_data"]
    return {
        "current_price": sd.get("price", 0.0),
        "price_return_1m": sd.get("ret_1m_pct", 0.0),
        "price_return_3m": sd.get("ret_3m_pct", 0.0),
        "rsi_14": sd.get("rsi_14", 50.0),
        "pct_from_50ma": sd.get("pct_from_50ma", 0.0),
        "pct_from_200ma": sd.get("pct_from_200ma", 0.0),
        "volatility_20d": sd.get("volatility_20d", 0.0),
        "data_source": "sector_assignments_fallback",
    }


# ---------------------------------------------------------------------------
# Brief construction
# ---------------------------------------------------------------------------

def _build_etf_entry(
    etf: str,
    market_data: dict,
    sector_index: Dict[str, dict],
    geo_data: dict,
) -> dict:
    """Assemble one ETF entry for the research brief."""
    sector_name = ETF_SECTOR_MAP.get(etf, "Unknown")
    sector_entry = sector_index.get(etf, {})
    rd_score_data = sector_entry.get("score_data", {})
    rd_sector_name = sector_entry.get("sector_name", sector_name)

    geo_risk = _get_geo_risk(etf, geo_data)

    # Derive trend from R&D data if available, else infer from price/MAs
    rd_trend = rd_score_data.get("trend", "")
    if rd_trend:
        trend = rd_trend
    else:
        pct50 = market_data.get("pct_from_50ma", 0.0)
        pct200 = market_data.get("pct_from_200ma", 0.0)
        if pct50 > 1.0 and pct200 > 1.0:
            trend = "bullish"
        elif pct50 < -1.0 or pct200 < -3.0:
            trend = "bearish"
        else:
            trend = "neutral"

    return {
        "etf": etf,
        "sector": rd_sector_name or sector_name,
        "current_price": market_data.get("current_price", 0.0),
        "price_return_1m": market_data.get("price_return_1m", 0.0),
        "price_return_3m": market_data.get("price_return_3m", 0.0),
        "rsi_14": market_data.get("rsi_14", 50.0),
        "pct_from_50ma": market_data.get("pct_from_50ma", 0.0),
        "pct_from_200ma": market_data.get("pct_from_200ma", 0.0),
        "volatility_20d": market_data.get("volatility_20d", 0.0),
        "trend": trend,
        "geo_risk": geo_risk,
        "rd_covered": etf in sector_index,  # True if R&D has sector score for this ETF
        "rd_score": rd_score_data.get("score", None),
        "data_source": market_data.get("data_source", "unknown"),
    }


def _build_regime_summary(bridge: dict, hist: dict, spy_1d_return: Optional[float] = None) -> dict:
    """Summarise current macro regime for the board meeting."""
    regime = bridge.get("regime", {})
    bull = regime.get("rd_regime_bull", 1)
    bear = regime.get("rd_regime_bear", 0)
    vix = regime.get("rd_vix", 20.0)
    spy_rsi = regime.get("rd_spy_rsi", 50.0)
    warning_count = regime.get("rd_warning_count", 0)
    overbought = regime.get("rd_spy_overbought", 0)

    # Read firm CRO level to supplement market signals
    _cro_level = "NORMAL"
    try:
        _state = json.load(open(DATA / "state.json"))
        _cro_level = _state.get("cro_level", "NORMAL")
    except Exception:
        pass

    # Classify overall regime
    if _cro_level in ("CRITICAL", "HIGH") or (bull == 0 and (vix > 25 or spy_rsi > 70)):
        regime_label = "bear"
        defensive_rotation_advised = True
    elif overbought or warning_count >= 3 or _cro_level == "ELEVATED":
        regime_label = "bull_extended"  # bull but late/overbought
        defensive_rotation_advised = True   # ELEVATED CRO = advise defensive tilt
    elif bull:
        regime_label = "bull"
        defensive_rotation_advised = False
    else:
        regime_label = "neutral"
        defensive_rotation_advised = False

    # Pull historical analogue
    hist_current = hist.get("current_regime", {})
    analogues = hist.get("historical_analogues", {})
    most_relevant = analogues.get("most_relevant_bull_analogues", [{}])[0] if regime_label != "bear" else analogues.get("most_relevant_bear_analogues", [{}])[0]

    return {
        "regime_label": regime_label,
        "bull": bool(bull),
        "bear": bool(bear),
        "vix": vix,
        "spy_rsi": spy_rsi,
        "overbought": bool(overbought),
        "warning_count": warning_count,
        "rising_rates": bool(regime.get("rd_rising_rates", 0)),
        "momentum_phase": hist_current.get("momentum_phase", "unknown"),
        "defensive_rotation_advised": defensive_rotation_advised,
        "historical_analogue": most_relevant.get("label", "None identified"),
        "analogue_lesson": most_relevant.get("lesson", ""),
        "sectors_led_in_analogue": most_relevant.get("sectors_that_led", []),
        "spy_1d_return": spy_1d_return,  # None if unavailable; negative = drop
    }


# ---------------------------------------------------------------------------
# Write output
# ---------------------------------------------------------------------------

def _write_brief(brief: dict) -> None:
    # A FIXED ".json.tmp" path is shared by every concurrent writer, and
    # open(tmp, "w") truncates BEFORE flock() is acquired — so a second writer
    # empties the first writer's temp file mid-dump, and the first writer then
    # renames the torn result over the live file. The lock cannot help: the
    # damage is done at open(). mkstemp gives each writer its own name, which
    # makes the lock unnecessary — os.replace is already atomic.
    fd, tmp = tempfile.mkstemp(dir=str(Path(BRIEF_PATH).parent),
                               prefix=".etf_research_agent.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(brief, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, BRIEF_PATH)
    log.info("ETF research brief written to %s", BRIEF_PATH)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    log.info("ETF Research Agent starting — %s", now)

    # Load all input data
    sector_data = _load_sector_assignments()
    bridge_data = _load_bridge_signals()
    hist_data   = _load_historical_context()
    geo_data    = _load_geopolitical_context()

    sector_index = _build_sector_index(sector_data)

    # Fetch SPY 1-day return for emergency market drop detection
    log.info("Fetching SPY 1-day return ...")
    spy_1d_return = _fetch_spy_1d_return()

    # Build per-ETF entries
    etf_entries: Dict[str, dict] = {}
    yf_success = 0
    yf_fallback = 0

    for etf in ETF_UNIVERSE:
        log.info("  Fetching data for %s ...", etf)

        # Try yfinance first
        market_data = _fetch_etf_data_yfinance(etf)

        if market_data is None:
            # Try sector_assignments.json fallback
            market_data = _fallback_from_sector(etf, sector_index)
            if market_data is None:
                log.warning("  No data available for %s — using zero placeholder", etf)
                market_data = {
                    "current_price": 0.0,
                    "price_return_1m": 0.0,
                    "price_return_3m": 0.0,
                    "rsi_14": 50.0,
                    "pct_from_50ma": 0.0,
                    "pct_from_200ma": 0.0,
                    "volatility_20d": 0.0,
                    "data_source": "unavailable",
                }
            else:
                yf_fallback += 1
                log.info("  %s: using sector_assignments fallback", etf)
        else:
            yf_success += 1
            log.info(
                "  %s: price=%.2f ret1m=%.1f%% rsi=%.1f vol=%.1f",
                etf,
                market_data["current_price"],
                market_data["price_return_1m"],
                market_data["rsi_14"],
                market_data["volatility_20d"],
            )

        etf_entries[etf] = _build_etf_entry(etf, market_data, sector_index, geo_data)

    # Build regime summary (includes spy_1d_return for emergency exit detection)
    regime_summary = _build_regime_summary(bridge_data, hist_data, spy_1d_return=spy_1d_return)
    log.info(
        "Regime: %s | VIX=%.1f | SPY_RSI=%.1f | warnings=%d | defensive=%s",
        regime_summary["regime_label"],
        regime_summary["vix"],
        regime_summary["spy_rsi"],
        regime_summary["warning_count"],
        regime_summary["defensive_rotation_advised"],
    )

    brief = {
        "generated_at": now,
        "agent": "ETF Research Agent",
        "etf_universe": ETF_UNIVERSE,
        "data_quality": {
            "yfinance_success": yf_success,
            "sector_fallback": yf_fallback,
            "unavailable": len(ETF_UNIVERSE) - yf_success - yf_fallback,
        },
        "regime": regime_summary,
        "etfs": etf_entries,
    }

    _write_brief(brief)
    log.info(
        "Brief complete — %d ETFs | yf_success=%d fallback=%d",
        len(etf_entries),
        yf_success,
        yf_fallback,
    )
    return brief


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('ETFResearchAgent'):
        raise SystemExit(0)
    lock = SingleInstanceLock(LOCK_FILE)
    if not lock.acquire():
        log.error("Another instance of etf_research_agent is already running. Exiting.")
        sys.exit(1)

    try:
        brief = run()
        import pprint
        print("\n=== ETF RESEARCH BRIEF (regime) ===")
        pprint.pprint(brief["regime"])
        print("\n=== ETF ENTRIES (sample) ===")
        for etf, entry in list(brief["etfs"].items())[:3]:
            print(f"  {etf}: price={entry['current_price']:.2f} ret1m={entry['price_return_1m']:.1f}% rsi={entry['rsi_14']:.1f} vol={entry['volatility_20d']:.1f} geo={entry['geo_risk']}")
    finally:
        lock.release()
