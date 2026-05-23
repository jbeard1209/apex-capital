"""
APEX CAPITAL — POD 3 MACRO AGENT
==================================
Daily sector ETF rotation strategy for Pod 3 (Macro / ETF Rotation).

Runs AFTER the R&D team. Reads sector scores + regime context, picks the top
3 sector ETFs by momentum, compares to current Pod 3 holdings, and writes
BUY/SELL signals to data/pod3_macro_signals.json.

Does NOT execute trades. Signals are reviewed and executed by the Manager Agent.

Allowed ETF universe: XLK, XLE, XLF, XLU, XLP, XLV, XLI, GLD, TLT, QQQ
Bear mode overrides selection to: XLU, XLP, GLD, TLT

Bear mode triggers when:
  rd_regime_bull == 0  AND  (rd_spy_rsi > 70  OR  rd_vix > 25)
"""

import json
import fcntl
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
SECTOR_PATH = BASE_DIR / "data" / "sector_assignments.json"
BRIDGE_PATH = BASE_DIR / "data" / "rd_ml_bridge_signals.json"
SIGNALS_PATH = BASE_DIR / "data" / "pod3_macro_signals.json"
LOCK_FILE = BASE_DIR / "data" / ".pod_macro_agent.lock"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ALLOWED_ETFS = {"XLK", "XLE", "XLF", "XLU", "XLP", "XLV", "XLI", "GLD", "TLT", "QQQ"}
DEFENSIVE_ETFS = {"XLU", "XLP", "GLD", "TLT"}
TOP_N = 3  # number of ETFs to hold

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MACRO_AGENT] %(levelname)s %(message)s",
)
log = logging.getLogger("pod_macro_agent")


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

class SingleInstanceLock:
    """Prevents multiple instances of the macro agent from running simultaneously."""

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

    def release(self):
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_sector_assignments() -> dict:
    with open(SECTOR_PATH, "r") as fh:
        return json.load(fh)


def _load_bridge_signals() -> dict:
    with open(BRIDGE_PATH, "r") as fh:
        return json.load(fh)


def _load_current_pod3_positions() -> Dict[str, dict]:
    """Load current Pod 3 positions from the ledger."""
    try:
        # Import here to avoid circular dependency issues at module level
        import pod_ledger
        summary = pod_ledger.get_pod_summary("pod3_macro")
        return summary.get("positions", {})
    except Exception as exc:
        log.warning("Could not load Pod 3 positions from ledger: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_etf(score_data: dict) -> float:
    """
    Momentum score formula:
      score = (ret_1m_pct * 0.4) + (ret_3m_pct * 0.3) + (pct_from_50ma * 0.2) - (volatility_20d * 0.1)
      +2 if trend == "bullish"
      -2 if trend == "bearish"
    """
    ret_1m = score_data.get("ret_1m_pct", 0.0)
    ret_3m = score_data.get("ret_3m_pct", 0.0)
    pct_50ma = score_data.get("pct_from_50ma", 0.0)
    vol = score_data.get("volatility_20d", 0.0)
    trend = score_data.get("trend", "neutral")

    raw = (ret_1m * 0.4) + (ret_3m * 0.3) + (pct_50ma * 0.2) - (vol * 0.1)

    if trend == "bullish":
        raw += 2.0
    elif trend == "bearish":
        raw -= 2.0

    return round(raw, 4)


def _is_bear_mode(regime: dict) -> bool:
    """
    Bear mode = rd_regime_bull == 0  AND  (rd_spy_rsi > 70  OR  rd_vix > 25)
    """
    bull = regime.get("rd_regime_bull", 1)
    spy_rsi = regime.get("rd_spy_rsi", 50.0)
    vix = regime.get("rd_vix", 20.0)
    return (bull == 0) and (spy_rsi > 70 or vix > 25)


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def _write_signals(signals: dict) -> None:
    tmp = SIGNALS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            json.dump(signals, fh, indent=2)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    tmp.replace(SIGNALS_PATH)


def run() -> dict:
    """
    Main entry point. Runs the full macro rotation logic and writes signals.
    Returns the signals dict.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    log.info("Pod 3 Macro Agent starting — %s", now)

    # -----------------------------------------------------------------------
    # 1. Load inputs
    # -----------------------------------------------------------------------
    try:
        sector_data = _load_sector_assignments()
    except FileNotFoundError:
        log.error("sector_assignments.json not found at %s", SECTOR_PATH)
        raise

    try:
        bridge_data = _load_bridge_signals()
    except FileNotFoundError:
        log.error("rd_ml_bridge_signals.json not found at %s", BRIDGE_PATH)
        raise

    regime = bridge_data.get("regime", {})
    bear_mode = _is_bear_mode(regime)

    log.info(
        "Regime context: bull=%s, VIX=%.1f, SPY_RSI=%.1f, warnings=%d | bear_mode=%s",
        regime.get("rd_regime_bull"),
        regime.get("rd_vix", 0.0),
        regime.get("rd_spy_rsi", 0.0),
        regime.get("rd_warning_count", 0),
        bear_mode,
    )

    # -----------------------------------------------------------------------
    # 2. Score eligible ETFs from sector_assignments
    # -----------------------------------------------------------------------
    all_scored_sectors = sector_data.get("all_scored", [])
    scored_etfs: List[Dict] = []

    for sector_entry in all_scored_sectors:
        score_data = sector_entry.get("score_data", {})
        etf = score_data.get("etf", "")

        if not etf or etf not in ALLOWED_ETFS:
            continue

        if bear_mode and etf not in DEFENSIVE_ETFS:
            log.info("Bear mode: skipping non-defensive ETF %s", etf)
            continue

        momentum_score = _score_etf(score_data)
        scored_etfs.append({
            "etf": etf,
            "sector_name": sector_entry.get("sector_name", "Unknown"),
            "momentum_score": momentum_score,
            "score_data": score_data,
        })

        log.info(
            "  Scored %s (%s): momentum=%.4f | ret1m=%.2f%% ret3m=%.2f%% pct50ma=%.2f%% vol=%.1f trend=%s",
            etf,
            sector_entry.get("sector_name", ""),
            momentum_score,
            score_data.get("ret_1m_pct", 0),
            score_data.get("ret_3m_pct", 0),
            score_data.get("pct_from_50ma", 0),
            score_data.get("volatility_20d", 0),
            score_data.get("trend", "neutral"),
        )

    # -----------------------------------------------------------------------
    # 3. Select top N ETFs
    # -----------------------------------------------------------------------
    scored_etfs.sort(key=lambda x: x["momentum_score"], reverse=True)
    top_etfs = scored_etfs[:TOP_N]
    top_etf_tickers = {e["etf"] for e in top_etfs}

    log.info("Top %d ETFs selected: %s", TOP_N, [e["etf"] for e in top_etfs])

    # -----------------------------------------------------------------------
    # 4. Compare to current Pod 3 positions
    # -----------------------------------------------------------------------
    current_positions = _load_current_pod3_positions()
    current_etf_tickers = set(current_positions.keys())

    to_buy = top_etf_tickers - current_etf_tickers
    to_sell = current_etf_tickers - top_etf_tickers
    to_hold = current_etf_tickers & top_etf_tickers

    log.info("Current positions: %s", list(current_etf_tickers) or "none")
    log.info("BUY signals: %s", list(to_buy) or "none")
    log.info("SELL signals: %s", list(to_sell) or "none")
    log.info("HOLD: %s", list(to_hold) or "none")

    # -----------------------------------------------------------------------
    # 5. Build signal objects
    # -----------------------------------------------------------------------
    buy_signals = []
    for entry in top_etfs:
        etf = entry["etf"]
        if etf in to_buy:
            buy_signals.append({
                "action": "BUY",
                "ticker": etf,
                "pod_id": "pod3_macro",
                "sector": entry["sector_name"],
                "momentum_score": entry["momentum_score"],
                "reason": (
                    f"Top {TOP_N} ETF by momentum score ({entry['momentum_score']:.2f}). "
                    f"ret_1m={entry['score_data'].get('ret_1m_pct', 0):.1f}%, "
                    f"ret_3m={entry['score_data'].get('ret_3m_pct', 0):.1f}%, "
                    f"pct_from_50ma={entry['score_data'].get('pct_from_50ma', 0):.1f}%, "
                    f"trend={entry['score_data'].get('trend', 'neutral')}"
                ),
                "signal_source": "pod3_macro_agent",
                "bear_mode": bear_mode,
            })

    sell_signals = []
    for etf in to_sell:
        pos = current_positions.get(etf, {})
        sell_signals.append({
            "action": "SELL",
            "ticker": etf,
            "pod_id": "pod3_macro",
            "qty": pos.get("qty", "ALL"),
            "reason": (
                f"{etf} dropped out of top {TOP_N} ETF selection. "
                + ("Bear mode override active." if bear_mode else "Rotation to higher-momentum ETF.")
            ),
            "signal_source": "pod3_macro_agent",
            "bear_mode": bear_mode,
        })

    hold_signals = []
    for entry in top_etfs:
        etf = entry["etf"]
        if etf in to_hold:
            hold_signals.append({
                "action": "HOLD",
                "ticker": etf,
                "pod_id": "pod3_macro",
                "sector": entry["sector_name"],
                "momentum_score": entry["momentum_score"],
                "reason": f"Already held and still in top {TOP_N}.",
                "signal_source": "pod3_macro_agent",
                "bear_mode": bear_mode,
            })

    # -----------------------------------------------------------------------
    # 6. Write output
    # -----------------------------------------------------------------------
    signals = {
        "generated_at": now,
        "agent": "Pod 3 Macro Agent",
        "regime": regime,
        "bear_mode": bear_mode,
        "scoring_method": (
            "score = (ret_1m*0.4) + (ret_3m*0.3) + (pct_from_50ma*0.2) - (vol*0.1) "
            "+2 if bullish, -2 if bearish"
        ),
        "top_etfs_ranked": [
            {
                "rank": i + 1,
                "etf": e["etf"],
                "sector": e["sector_name"],
                "momentum_score": e["momentum_score"],
            }
            for i, e in enumerate(top_etfs)
        ],
        "all_scored": [
            {"etf": e["etf"], "sector": e["sector_name"], "momentum_score": e["momentum_score"]}
            for e in scored_etfs
        ],
        "signals": {
            "buy": buy_signals,
            "sell": sell_signals,
            "hold": hold_signals,
        },
        "summary": {
            "n_buy": len(buy_signals),
            "n_sell": len(sell_signals),
            "n_hold": len(hold_signals),
        },
    }

    _write_signals(signals)
    log.info(
        "Signals written to %s — BUY:%d SELL:%d HOLD:%d",
        SIGNALS_PATH,
        len(buy_signals),
        len(sell_signals),
        len(hold_signals),
    )

    return signals


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lock = SingleInstanceLock(LOCK_FILE)
    if not lock.acquire():
        log.error("Another instance of pod_macro_agent is already running. Exiting.")
        sys.exit(1)

    try:
        signals = run()
        import pprint
        print("\n=== POD 3 MACRO AGENT SIGNALS ===")
        pprint.pprint(signals)
    finally:
        lock.release()
