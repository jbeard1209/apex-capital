#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — INTERNATIONAL MARKETS INTELLIGENCE (IMI) DESK
# Sub-team of the R&D Department, reporting to the R&D Team Leader.
#
# Monitors European and Asian markets as leading signals for US positioning:
#   · Major indices (Nikkei, DAX, Hang Seng, FTSE, CAC 40, etc.)
#   · Key currency pairs (EUR/USD, USD/JPY, GBP/USD, DXY, CNH)
#   · International ETFs trading on US exchanges (EWJ, EWG, FXI, etc.)
#   · Commodities with international demand drivers (Brent, copper, gold)
#
# Output: data/international_report.json
# Called by: rd_team_leader.py (Phase 2c)
# Also callable standalone: python3 international_analyst.py
# =============================================================================

import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

try:
    from config import STOCK_UNIVERSE
except ImportError:
    STOCK_UNIVERSE = []

try:
    from firm_channel import post as _fc_post
except ImportError:
    _fc_post = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("intl_analyst")

DATA_DIR   = "data"
REPORT_PATH = os.path.join(DATA_DIR, "international_report.json")
ET = ZoneInfo("America/New_York")

# ── Tracked instruments ───────────────────────────────────────────────────────

INDICES = {
    # Asia-Pacific
    "Nikkei 225":       "^N225",
    "Hang Seng":        "^HSI",
    "Shanghai Comp":    "000001.SS",
    "KOSPI":            "^KS11",
    "Taiwan Weighted":  "^TWII",
    "ASX 200":          "^AXJO",
    # Europe
    "DAX":              "^GDAXI",
    "FTSE 100":         "^FTSE",
    "CAC 40":           "^FCHI",
    "Euro Stoxx 50":    "^STOXX50E",
}

CURRENCIES = {
    "EUR/USD":  "EURUSD=X",
    "GBP/USD":  "GBPUSD=X",
    "USD/JPY":  "USDJPY=X",
    "USD/CNH":  "USDCNH=X",
    "AUD/USD":  "AUDUSD=X",
    "DXY":      "DX-Y.NYB",
}

# International ETFs (US-traded, liquid — cleaner intraday data than raw indices)
INTL_ETFS = {
    "Japan (EWJ)":      "EWJ",
    "Germany (EWG)":    "EWG",
    "China LC (FXI)":   "FXI",
    "UK (EWU)":         "EWU",
    "Europe (VGK)":     "VGK",
    "EM broad (EEM)":   "EEM",
    "EAFE (EFA)":       "EFA",
    "Korea (EWY)":      "EWY",
    "Taiwan (EWT)":     "EWT",
}

COMMODITIES = {
    "Brent Crude":  "BZ=F",
    "WTI Crude":    "CL=F",
    "Gold":         "GC=F",
    "Copper":       "HG=F",
    "Silver":       "SI=F",
}

# Which US universe tickers have material exposure to each region
REGION_EXPOSURE = {
    "Japan":   ["NVDA", "AMD", "QCOM", "AVGO", "TSM", "SONY", "TM"],
    "China":   ["AAPL", "NVDA", "QCOM", "AVGO", "TSM", "BABA", "JD", "PDD"],
    "Europe":  ["MSFT", "GOOGL", "META", "AMZN", "JPM", "GS", "MS", "SAP"],
    "EM":      ["AAPL", "NVDA", "QCOM", "AVGO"],
    "Energy":  ["XOM", "CVX", "COP", "PSX", "VLO"],
    "Gold":    ["GLD", "SLV", "NEM", "GOLD"],
}


# =============================================================================
# DATA COLLECTION
# =============================================================================

def _pct_change(ticker: str) -> float | None:
    """Return today's % change for a ticker. Returns None on failure."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="2d", interval="1d")
        if len(hist) < 2:
            # Try fast_info for indices that don't have 2-day history readily
            info = t.fast_info
            prev = getattr(info, "previous_close", None)
            last = getattr(info, "last_price", None)
            if prev and last and prev > 0:
                return (last - prev) / prev * 100
            return None
        prev_close = float(hist["Close"].iloc[-2])
        last_close = float(hist["Close"].iloc[-1])
        if prev_close == 0:
            return None
        return (last_close - prev_close) / prev_close * 100
    except Exception:
        return None


def _last_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        return getattr(info, "last_price", None)
    except Exception:
        return None


def _collect(instruments: dict) -> list[dict]:
    results = []
    for name, ticker in instruments.items():
        pct = _pct_change(ticker)
        price = _last_price(ticker)
        results.append({
            "name":   name,
            "ticker": ticker,
            "pct":    round(pct, 2) if pct is not None else None,
            "price":  round(price, 4) if price is not None else None,
        })
    return results


# =============================================================================
# SENTIMENT & IMPLICATIONS
# =============================================================================

def _sentiment(indices: list, currencies: list) -> tuple[str, int]:
    """
    Returns (label, score) where score is -3 (panic) to +3 (euphoria).
    Uses index breadth and DXY direction as primary signals.
    """
    valid = [r for r in indices if r["pct"] is not None]
    if not valid:
        return "NEUTRAL", 0

    up   = sum(1 for r in valid if r["pct"] >  0.3)
    down = sum(1 for r in valid if r["pct"] < -0.3)
    big_up   = sum(1 for r in valid if r["pct"] >  1.5)
    big_down = sum(1 for r in valid if r["pct"] < -1.5)

    score = (up - down) + (big_up * 2 - big_down * 2)

    # DXY strengthening significantly → risk-off adjustment
    dxy = next((c for c in currencies if c["name"] == "DXY"), None)
    if dxy and dxy["pct"] is not None:
        if dxy["pct"] > 0.5:
            score -= 1
        elif dxy["pct"] < -0.5:
            score += 1

    score = max(-3, min(3, score))

    if score >= 2:
        label = "RISK_ON"
    elif score <= -2:
        label = "RISK_OFF"
    else:
        label = "NEUTRAL"

    return label, score


def _notable(data: list, threshold: float = 0.8) -> list[dict]:
    return sorted(
        [r for r in data if r["pct"] is not None and abs(r["pct"]) >= threshold],
        key=lambda r: abs(r["pct"]),
        reverse=True,
    )


def _us_implications(
    indices: list,
    currencies: list,
    commodities: list,
    sentiment: str,
    universe: list,
) -> list[str]:
    """Generate plain-English US market implications from international moves."""
    implications = []
    universe_set = set(universe)

    def _note(msg): implications.append(msg)

    # Index-level signals
    for idx in indices:
        if idx["pct"] is None:
            continue
        p = idx["pct"]
        name = idx["name"]
        if name == "Nikkei 225":
            if p <= -2.0:
                _note(f"Nikkei down {p:+.1f}% — Asia risk-off; watch semis/tech (NVDA, AVGO, QCOM).")
            elif p >= 2.0:
                _note(f"Nikkei up {p:+.1f}% — Asia optimism supportive for US tech open.")
        if name == "Hang Seng":
            if p <= -1.5:
                _note(f"Hang Seng down {p:+.1f}% — China risk-off; AAPL supply-chain exposure flagged.")
            elif p >= 1.5:
                _note(f"Hang Seng up {p:+.1f}% — China demand optimism; EM-exposed names may benefit.")
        if name == "DAX":
            if abs(p) >= 1.5:
                _note(f"DAX {p:+.1f}% — European tone sets US futures direction pre-open.")

    # Currency signals
    usdjpy = next((c for c in currencies if c["name"] == "USD/JPY"), None)
    eurusd = next((c for c in currencies if c["name"] == "EUR/USD"), None)
    dxy    = next((c for c in currencies if c["name"] == "DXY"), None)

    if usdjpy and usdjpy["pct"] is not None:
        if usdjpy["pct"] >= 0.5:
            _note(f"USD/JPY +{usdjpy['pct']:.1f}% (yen weakening) — historically tailwind for US equities; watch Japan carry trade unwind risk.")
        elif usdjpy["pct"] <= -0.5:
            _note(f"USD/JPY {usdjpy['pct']:+.1f}% (yen strengthening) — may signal global risk-off or carry unwind pressure.")

    if eurusd and eurusd["pct"] is not None:
        if eurusd["pct"] >= 0.5:
            _note(f"EUR/USD {eurusd['pct']:+.1f}% — stronger euro = FX headwind for US multinationals reporting in USD (MSFT, GOOGL, META).")
        elif eurusd["pct"] <= -0.5:
            _note(f"EUR/USD {eurusd['pct']:+.1f}% — weaker euro = tailwind for USD-denominated exports, headwind for European revenue translations.")

    if dxy and dxy["pct"] is not None:
        if dxy["pct"] >= 0.7:
            _note(f"DXY {dxy['pct']:+.1f}% — broad dollar strength; typically headwind for commodities and EM assets.")
        elif dxy["pct"] <= -0.7:
            _note(f"DXY {dxy['pct']:+.1f}% — dollar weakness; supportive for gold, commodities, and EM equities.")

    # Commodity signals
    brent = next((c for c in commodities if c["name"] == "Brent Crude"), None)
    copper = next((c for c in commodities if c["name"] == "Copper"), None)
    gold  = next((c for c in commodities if c["name"] == "Gold"), None)

    if brent and brent["pct"] is not None:
        if brent["pct"] >= 2.0:
            exp = [t for t in REGION_EXPOSURE["Energy"] if t in universe_set]
            _note(f"Brent +{brent['pct']:.1f}% — energy sector tailwind" +
                  (f"; watch {', '.join(exp[:3])}" if exp else "") + ".")
        elif brent["pct"] <= -2.0:
            _note(f"Brent {brent['pct']:+.1f}% — energy sector headwind; consumer discretionary/airlines may benefit.")

    if copper and copper["pct"] is not None:
        if copper["pct"] >= 1.5:
            _note(f"Copper +{copper['pct']:.1f}% — global growth signal (Dr. Copper); supportive for industrials and materials.")
        elif copper["pct"] <= -1.5:
            _note(f"Copper {copper['pct']:+.1f}% — global growth concern; watch industrials/materials downside.")

    if gold and gold["pct"] is not None:
        if gold["pct"] >= 1.0:
            _note(f"Gold +{gold['pct']:.1f}% — flight-to-safety bid; consistent with {sentiment} reading.")
        elif gold["pct"] <= -1.0:
            _note(f"Gold {gold['pct']:+.1f}% — risk appetite improving; less defensive positioning needed.")

    if not implications:
        implications.append("International session shows no outsized moves. US open likely to be driven by domestic catalysts.")

    return implications


def _watch_tickers(indices: list, commodities: list, currencies: list, universe: list) -> list[str]:
    """Return STOCK_UNIVERSE tickers most likely affected by today's international moves."""
    universe_set = set(universe)
    watch = set()

    for idx in indices:
        if idx["pct"] is None or abs(idx["pct"]) < 1.0:
            continue
        name = idx["name"]
        if "Nikkei" in name or "KOSPI" in name or "Taiwan" in name:
            watch.update(t for t in REGION_EXPOSURE.get("Japan", []) if t in universe_set)
        if "Hang Seng" in name or "Shanghai" in name:
            watch.update(t for t in REGION_EXPOSURE.get("China", []) if t in universe_set)
        if "DAX" in name or "FTSE" in name or "CAC" in name or "Stoxx" in name:
            watch.update(t for t in REGION_EXPOSURE.get("Europe", []) if t in universe_set)

    brent = next((c for c in commodities if c["name"] == "Brent Crude"), None)
    if brent and brent["pct"] is not None and abs(brent["pct"]) >= 2.0:
        watch.update(t for t in REGION_EXPOSURE.get("Energy", []) if t in universe_set)

    gold = next((c for c in commodities if c["name"] == "Gold"), None)
    if gold and gold["pct"] is not None and gold["pct"] >= 1.0:
        watch.update(t for t in REGION_EXPOSURE.get("Gold", []) if t in universe_set)

    return sorted(watch)


def _session_label() -> str:
    now_et = datetime.now(ET)
    h = now_et.hour
    if 18 <= h or h < 3:
        return "asia_open"
    if 3 <= h < 8:
        return "europe_open"
    if 8 <= h < 9:
        return "pre_market"
    if 9 <= h < 16:
        return "us_session"
    return "after_hours"


# =============================================================================
# MAIN RUN
# =============================================================================

def run(universe: list | None = None) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if universe is None:
        universe = STOCK_UNIVERSE

    session = _session_label()
    logger.info(f"  [IMI] Starting international scan — session: {session}")

    logger.info("  [IMI] Collecting indices...")
    indices = _collect(INDICES)

    logger.info("  [IMI] Collecting currencies...")
    currencies = _collect(CURRENCIES)

    logger.info("  [IMI] Collecting international ETFs...")
    etfs = _collect(INTL_ETFS)

    logger.info("  [IMI] Collecting commodities...")
    commodities = _collect(COMMODITIES)

    sentiment, score = _sentiment(indices, currencies)
    notable_indices   = _notable(indices, threshold=0.8)
    notable_currencies = _notable(currencies, threshold=0.4)
    notable_commodities = _notable(commodities, threshold=1.0)
    implications      = _us_implications(indices, currencies, commodities, sentiment, universe)
    watch             = _watch_tickers(indices, commodities, currencies, universe)

    report = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "session":             session,
        "sentiment":           sentiment,
        "sentiment_score":     score,
        "indices":             indices,
        "currencies":          currencies,
        "etfs":                etfs,
        "commodities":         commodities,
        "notable_indices":     notable_indices,
        "notable_currencies":  notable_currencies,
        "notable_commodities": notable_commodities,
        "us_implications":     implications,
        "watch_tickers":       watch,
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(
        f"  [IMI] Done. Sentiment: {sentiment} (score {score:+d}) | "
        f"{len(notable_indices)} notable index moves | "
        f"{len(watch)} US tickers flagged"
    )

    # Post to firm channel
    if _fc_post:
        try:
            notable_str = ", ".join(
                f"{r['name']} {r['pct']:+.1f}%" for r in notable_indices[:4]
            ) or "no major moves"
            _fc_post(
                "International Analyst (IMI)", "INTL_MARKETS",
                f"Global session [{session}] | Sentiment: {sentiment} ({score:+d}) | "
                f"Notable: {notable_str}. "
                f"Watch: {', '.join(watch[:5]) or 'none flagged'}.",
                priority="HIGH" if sentiment == "RISK_OFF" and score <= -2 else "NORMAL",
            )
            # Post each implication to firm channel if significant
            for impl in implications[:2]:
                if len(impl) > 30:
                    _fc_post("International Analyst (IMI)", "INTL_MARKETS", impl)
        except Exception as e:
            logger.debug(f"  [IMI] Firm channel post failed: {e}")

    return report


if __name__ == "__main__":
    import sys
    report = run()
    print(f"\n  Sentiment:  {report['sentiment']} ({report['sentiment_score']:+d})")
    print(f"  Session:    {report['session']}")
    print(f"\n  Notable moves:")
    for r in report["notable_indices"][:6]:
        print(f"    {r['name']:20s}  {r['pct']:+.1f}%")
    if report["notable_currencies"]:
        print(f"\n  Currency moves:")
        for r in report["notable_currencies"][:4]:
            print(f"    {r['name']:20s}  {r['pct']:+.1f}%")
    if report["notable_commodities"]:
        print(f"\n  Commodities:")
        for r in report["notable_commodities"][:4]:
            print(f"    {r['name']:20s}  {r['pct']:+.1f}%")
    print(f"\n  US implications:")
    for impl in report["us_implications"]:
        print(f"    · {impl}")
    if report["watch_tickers"]:
        print(f"\n  Watch tickers: {', '.join(report['watch_tickers'])}")
    print(f"\n  Report saved → {REPORT_PATH}")
