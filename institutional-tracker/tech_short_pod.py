#!/usr/bin/env python3
"""
Tech-Short Pod — Korea/AI-hardware regime shorts
================================================================================
Thesis (2026-07-30): South Korea's KOSPI fell ~33% in July 2026 — its worst
month on record — triggered July 28 when China began mass-producing homegrown
DUV chip-making tools, gutting the memory/foundry moat of Samsung & SK Hynix
after a 116% AI-driven run-up. That is a SEMICONDUCTOR / AI-HARDWARE shock, and
it transmits to the US names most levered to the same AI-hardware capex cycle.
Blow-off-top unwinds punish the most-stretched names hardest.

This pod does NOT presuppose which US names crack — it SCREENS a KR/AI-hardware
exposed universe against real prices and RANKS the most vulnerable short
candidates, gated by a live Korea-stress signal (so it only fires when the KR
channel is actually under stress, not on a static hunch).

  Signal  : composite of EWY (iShares MSCI South Korea) + SMH (semis ETF)
            distance below their 50/200-day MAs -> NONE / ELEVATED / STRESS / ACUTE
  Screen  : reuse the firm's confirmed-downtrend rule (price < 50MA AND < 200MA)
  Rank    : downtrend depth + blow-off-top unwind (drop from 52w high) +
            relative weakness vs SMH. Worst (most vulnerable) first.
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


def _rank_score(m: dict, smh_ret: float, name_ret: float) -> float:
    """Lower (more negative) == more vulnerable, so it sorts worst-first.
      - downtrend depth: how far below the moving averages
      - blow-off-top: distance from the 52-week high (stretched names fall more)
      - relative weakness: underperformance vs the semis ETF over the window
    """
    rel_weak = name_ret - smh_ret        # negative == underperforming semis
    return (m["pct_from_50"] + m["pct_from_200"]) + 0.5 * m["pct_from_high"] + rel_weak


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

    ranked = []
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
        name_ret = 0.0
        if len(df) >= 60:
            name_ret = (m["price"] / float(df["Close"].iloc[-60]) - 1) * 100
        score = _rank_score(m, smh_ret, name_ret)
        ranked.append({
            "ticker": ticker,
            "score": round(score, 2),
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
