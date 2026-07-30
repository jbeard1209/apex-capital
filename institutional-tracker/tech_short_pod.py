#!/usr/bin/env python3
"""
Tech-Short Pod — Korea/AI-hardware regime shorts
================================================================================
Thesis (2026-07-30): South Korea's KOSPI fell ~33% in July 2026 — its worst
month on record — triggered July 28 when China began mass-producing homegrown
DUV chip-making tools, gutting the memory/foundry moat of Samsung & SK Hynix
after a 116% AI-driven run-up. That is a SEMICONDUCTOR / AI-HARDWARE shock, and
it transmits to the US names most levered to the same AI-hardware capex cycle.

This pod does NOT presuppose which US names crack — it SCREENS a KR/AI-hardware
exposed universe against real prices and RANKS the most vulnerable short
candidates, gated by a live Korea-stress signal (so it only fires when the KR
channel is actually under stress, not on a static hunch).

CORRECTED 2026-07-30 after measuring it: the original design ranked the MOST
extended names as the BEST shorts. The 5-year evidence says that is backwards —
from this much extension the average forward 20-day return is +6.99% and shorts
win only 39.1% of the time. Extension is now a hard DISQUALIFIER, not a bonus
(see MIN_EXTENSION_PCTILE). The pod screens for structural damage and relative
weakness instead, and will legitimately return an EMPTY list when the whole
complex is too stretched to short — an empty slate is a valid, honest answer.

  Signal  : composite of EWY (iShares MSCI South Korea) + SMH (semis ETF)
            distance below their 50/200-day MAs -> NONE / ELEVATED / STRESS / ACUTE
  Screen  : confirmed-downtrend rule (price < 50MA AND < 200MA), then EXCLUDE
            anything below MIN_EXTENSION_PCTILE (mean-reversion/squeeze risk)
  Rank    : structural damage (drop from 52w high) + relative weakness vs SMH
  Output  : data/tech_short_candidates.json — consumed by the short book in
            alpaca_trader.get_momentum_short_candidates() when KR stress is on.

Paper trading only. This is firm research output, not investment advice.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except Exception:      # pragma: no cover
    yf = None

try:
    from config import KR_SHORT_UNIVERSE
except Exception:
    # Fallback universe if config not yet extended — US names most exposed to the
    # KR memory / AI-hardware capex channel.
    KR_SHORT_UNIVERSE = [
        "NVDA", "AMD", "MU", "AVGO", "MRVL", "QCOM", "TXN", "ARM",
        "SMCI", "DELL", "ANET", "VRT", "LRCX", "AMAT", "KLAC", "INTC",
        "ORCL", "PLTR", "TSM", "ASML",
    ]

logger = logging.getLogger("tech_short_pod")

DATA = Path(__file__).resolve().parent / "data"
OUTPUT_PATH = DATA / "tech_short_candidates.json"

# Korea-stress signal proxies and thresholds (distance below 50-day MA, %).
KR_SIGNAL_PROXIES = ("EWY", "SMH")
_STRESS_50MA_PCT = {          # avg % below 50MA across proxies -> level
    "ACUTE":    -12.0,
    "STRESS":   -6.0,
    "ELEVATED": -2.0,
}
MAX_CANDIDATES = 8            # ranked short list size the short book may draw from


def _hist(ticker: str, period: str = "300d"):
    if yf is None:
        return None
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df if len(df) >= 50 else None
    except Exception as e:
        logger.warning("history fetch failed for %s: %s", ticker, e)
        return None


def _metrics(df) -> dict | None:
    """Downtrend + blow-off-top metrics from a price frame."""
    try:
        close = df["Close"]
        price = float(close.iloc[-1])
        ma50  = float(close.iloc[-50:].mean())
        ma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50
        hi_52w = float(close.iloc[-252:].max()) if len(close) >= 60 else float(close.max())
        return {
            "price": price, "ma50": ma50, "ma200": ma200,
            "pct_from_50":  (price - ma50)  / ma50  * 100 if ma50 else 0.0,
            "pct_from_200": (price - ma200) / ma200 * 100 if ma200 else 0.0,
            "pct_from_high": (price - hi_52w) / hi_52w * 100 if hi_52w else 0.0,
        }
    except Exception:
        return None


def korea_stress_signal() -> dict:
    """Live Korea/AI-hardware stress read. Returns
    {level, score, proxies:{ticker:pct_from_50}, ts}."""
    proxies, vals = {}, []
    for t in KR_SIGNAL_PROXIES:
        df = _hist(t, period="120d")
        m = _metrics(df) if df is not None else None
        if m:
            proxies[t] = round(m["pct_from_50"], 2)
            vals.append(m["pct_from_50"])
    avg = sum(vals) / len(vals) if vals else 0.0
    level = "NONE"
    for lvl in ("ELEVATED", "STRESS", "ACUTE"):   # ascending threshold order
        if avg <= _STRESS_50MA_PCT[lvl]:
            level = lvl
    return {"level": level, "score": round(avg, 2), "proxies": proxies,
            "ts": datetime.now(timezone.utc).isoformat()}


# --- ANTI-CHASING GUARD (empirical, 2026-07-30) ------------------------------
# Measured over 5y of history for every name in this universe: when price closed
# at or below its CURRENT distance beneath the 50MA, the average forward 20-day
# return was **+6.99%** and shorts won only **39.1%** of the time (need >50% to
# pay, before borrow). Every single candidate mean-reverted; MU failed 75% of the
# time. Shorting maximum downside extension is therefore fighting mean reversion,
# not riding a trend. So extension is a DISQUALIFIER past a threshold, never a
# reward. Do not "improve" the ranking by re-adding an extension bonus without
# re-running scratch study extension_study.py and beating these numbers.
MIN_EXTENSION_PCTILE = 5.0    # below this percentile == historically unprecedented -> squeeze risk


def _extension_pctile(close, ext_today: float) -> float:
    """Where today's distance-below-50MA sits in this name's own 5y history.
    Low percentile == extremely stretched to the downside == squeeze-prone."""
    try:
        ma50 = close.rolling(50).mean()
        ext = ((close / ma50 - 1) * 100).dropna()
        if len(ext) < 100:
            return 50.0          # not enough history to judge -> neutral
        return float((ext < ext_today).mean() * 100)
    except Exception:
        return 50.0


def _rank_score(m: dict, smh_ret: float, name_ret: float) -> float:
    """Lower (more negative) == better short candidate, so it sorts worst-first.

    Ranks on STRUCTURAL damage and relative weakness — NOT on how far price has
    already fallen below its moving averages (see the empirical note above; that
    is a mean-reversion trap). Extreme extension is handled as a hard exclusion
    in screen(), not as a score bonus.
    """
    rel_weak = name_ret - smh_ret        # negative == underperforming semis
    return 0.5 * m["pct_from_high"] + rel_weak


def screen(signal: dict | None = None) -> dict:
    """Screen KR_SHORT_UNIVERSE for confirmed downtrend and rank the most
    vulnerable. Always returns a full report; the `active` flag reflects whether
    the Korea-stress signal is high enough to warrant deploying the shorts."""
    sig = signal or korea_stress_signal()
    active = sig["level"] in ("STRESS", "ACUTE")

    # semis benchmark return over ~60d for relative-weakness scoring
    smh = _hist("SMH", period="90d")
    smh_ret = 0.0
    if smh is not None and len(smh) >= 60:
        smh_ret = (float(smh["Close"].iloc[-1]) / float(smh["Close"].iloc[-60]) - 1) * 100

    ranked, excluded = [], []
    for ticker in KR_SHORT_UNIVERSE:
        df = _hist(ticker)
        if df is None:
            continue
        m = _metrics(df)
        if not m:
            continue
        # confirmed downtrend gate — same rule the short book already uses
        if not (m["price"] < m["ma50"] and m["price"] < m["ma200"]):
            continue
        # anti-chasing gate — a name already at an historically unprecedented
        # distance below its 50MA is a squeeze risk, not a short (see the
        # empirical note on MIN_EXTENSION_PCTILE). Excluded, and recorded so the
        # rejection is auditable rather than silent.
        pctile = _extension_pctile(df["Close"], m["pct_from_50"])
        if pctile < MIN_EXTENSION_PCTILE:
            excluded.append({
                "ticker": ticker, "extension_pctile": round(pctile, 1),
                "reason": (f"{m['pct_from_50']:+.1f}% vs 50MA is the {pctile:.1f}th percentile "
                           f"of its own history — mean-reversion/squeeze risk, not a short"),
            })
            continue
        name_ret = 0.0
        if len(df) >= 60:
            name_ret = (m["price"] / float(df["Close"].iloc[-60]) - 1) * 100
        score = _rank_score(m, smh_ret, name_ret)
        ranked.append({
            "ticker": ticker,
            "score": round(score, 2),
            "extension_pctile": round(pctile, 1),
            "pct_from_50": round(m["pct_from_50"], 1),
            "pct_from_200": round(m["pct_from_200"], 1),
            "pct_from_high": round(m["pct_from_high"], 1),
            "rel_vs_smh": round(name_ret - smh_ret, 1),
            "rationale": (f"{m['pct_from_50']:+.1f}% vs 50MA, {m['pct_from_200']:+.1f}% vs 200MA, "
                          f"{m['pct_from_high']:+.1f}% off 52w high, "
                          f"{name_ret - smh_ret:+.1f}% vs SMH"),
        })
    ranked.sort(key=lambda x: x["score"])   # worst (most vulnerable) first
    ranked = ranked[:MAX_CANDIDATES]

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "thesis": "Korea/AI-hardware regime short (KOSPI crash 2026-07, China DUV shock)",
        "signal": sig,
        "active": active,
        "smh_60d_return_pct": round(smh_ret, 1),
        "candidates": ranked,
        "excluded_too_extended": excluded,
        "tickers": [c["ticker"] for c in ranked],
    }


def run(write: bool = True) -> dict:
    report = screen()
    if write:
        OUTPUT_PATH.write_text(json.dumps(report, indent=2))
    return report


def load_candidates(max_age_hours: float = 30.0) -> list:
    """Consumer helper for the short book: return the ranked tickers IFF the
    Korea-stress signal is active AND the report is fresh. Empty otherwise —
    fail-closed, so a stale or inactive signal never opens shorts."""
    try:
        rep = json.loads(OUTPUT_PATH.read_text())
    except Exception:
        return []
    if not rep.get("active"):
        return []
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(rep["ts"])).total_seconds() / 3600.0
        if age > max_age_hours:
            return []
    except Exception:
        return []
    return list(rep.get("tickers", []))


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Tech-Short Pod (Korea/AI-hardware)")
    p.add_argument("--report", action="store_true", help="screen without writing output")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    rep = run(write=not args.report)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        s = rep["signal"]
        print(f"[tech-short] Korea-stress={s['level']} (score {s['score']} vs 50MA) "
              f"active={rep['active']} | candidates:")
        for c in rep["candidates"]:
            print(f"    {c['ticker']:6s} score={c['score']:7.1f}  {c['rationale']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
