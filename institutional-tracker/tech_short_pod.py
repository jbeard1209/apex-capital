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
  Watch   : thesis_watch() re-tests the two conditions that would overturn the
            DO-NOT-SHORT verdict, as DELTAS vs data/kr_thesis_baseline.json (the
            verdict-day state). Escalates to the Chairman queue, idempotently.
            It alerts; it never trades.

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


# ---------------------------------------------------------------------------
# Thesis watch — keep the DO-NOT-SHORT verdict under review instead of filing it.
#
# The 2026-07-30 research verdict was DO NOT SHORT, but it named two specific
# conditions that would flip it. A verdict with named flip conditions and nobody
# watching them is just a stale opinion, so the pod re-tests both on every run
# (it is already scheduled 3x/weekday — no new cron, no new orphan file).
#
#   1. ENTRY_WINDOW — the whole complex was excluded as TOO EXTENDED (squeeze
#      risk). The documented entry is a rally back TOWARD the 50MA while the
#      downtrend still holds. That is exactly what a non-empty `candidates`
#      list now means, so the screen itself is the detector.
#   2. CREDIT_CHANNEL — the real risk migrated from AI-capex DEMAND to how it is
#      FUNDED (debt share of capex went 9% -> 32%). Demand is not rolling over;
#      credit is the channel that would actually crack. Proxy: high-yield (HYG)
#      underperforming investment-grade (LQD) — the classic risk-repricing tell —
#      plus drawdown in ORCL, the named concentrated-credit story.
#
# Escalates into data/chairman_action_required.json (read by 20 modules incl.
# the app inbox and the decision-SLA escalation). Deduped per condition per
# month so a persisting condition informs without flooding. This ALERTS; it
# never trades — opening shorts still requires the live KR-stress gate in
# load_candidates(), which stays fail-closed.
# ---------------------------------------------------------------------------
CHAIRMAN_PATH  = DATA / "chairman_action_required.json"
BASELINE_PATH  = DATA / "kr_thesis_baseline.json"

# Thresholds are DELTAS FROM THE VERDICT-TIME BASELINE, not absolute levels.
# This distinction is the whole design. The first version of this watch used
# absolute triggers and fired both conditions on its very first run — because
# ORCL was already -61.5% off its high and ORCL/PLTR/SMCI were already the
# candidate set ON THE DAY THE VERDICT WAS WRITTEN. A fact the verdict was made
# from cannot be evidence against the verdict. Only movement is news.
CREDIT_SPREAD_DETERIORATION = -3.0   # HY-minus-IG must fall this many pts vs baseline
CREDIT_SPREAD_MUST_BE_NEG   = True   # ...and actually turn negative (HY underperforming)
ORCL_FURTHER_DRAWDOWN_PCT   = -10.0  # ORCL this much FURTHER off its high than baseline
ENTRY_PCTILE_RECOVERY       = 15.0   # an excluded name back to this pctile == the entry


def _baseline() -> dict:
    """Verdict-time state. Missing baseline => fail-SILENT (no alerts), never
    fail-loud: an unknown baseline means every reading looks like a change."""
    try:
        return json.loads(BASELINE_PATH.read_text())
    except Exception:
        return {}


def _credit_channel(base: dict | None = None) -> dict:
    """Has the AI-capex CREDIT channel deteriorated SINCE THE VERDICT? Returns a
    measured reading, or measurable=False when data is unavailable — a data
    outage must never manufacture an alarm."""
    out = {"measurable": False, "cracking": False, "detail": "data unavailable"}
    try:
        rets = {}
        for tk in ("HYG", "LQD"):
            df = _hist(tk, period="90d")
            if df is None or len(df) < 60:
                return out
            rets[tk] = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-60]) - 1) * 100
        spread = rets["HYG"] - rets["LQD"]

        orcl_dd = 0.0
        df = _hist("ORCL")
        if df is not None and len(df) >= 60:
            m = _metrics(df)
            if m:
                orcl_dd = m["pct_from_high"]

        out = {
            "measurable": True, "cracking": False,
            "hy_minus_ig_60d_pct": round(spread, 2),
            "orcl_drawdown_pct": round(orcl_dd, 1),
        }

        base = base if base is not None else _baseline()
        if not base:
            out["detail"] = "no verdict baseline on file — watch is inert"
            return out

        b_spread = float(base.get("hy_minus_ig_60d_pct", spread))
        b_orcl   = float(base.get("orcl_drawdown_pct", orcl_dd))
        d_spread = spread - b_spread
        d_orcl   = orcl_dd - b_orcl

        hy_cracking = (d_spread <= CREDIT_SPREAD_DETERIORATION
                       and (spread < 0 or not CREDIT_SPREAD_MUST_BE_NEG))
        orcl_cracking = d_orcl <= ORCL_FURTHER_DRAWDOWN_PCT

        out["cracking"] = bool(hy_cracking or orcl_cracking)
        out["detail"] = (
            f"HY-IG 60d {spread:+.2f}pts vs baseline {b_spread:+.2f} ({d_spread:+.2f}pts; "
            f"trigger {CREDIT_SPREAD_DETERIORATION}pts AND negative) | "
            f"ORCL {orcl_dd:+.1f}% off high vs baseline {b_orcl:+.1f}% ({d_orcl:+.1f}pts; "
            f"trigger {ORCL_FURTHER_DRAWDOWN_PCT}pts)"
        )
    except Exception as e:      # pragma: no cover - defensive
        out["detail"] = f"error: {e}"
    return out


def _append_deduped(path: Path, entry: dict) -> bool:
    """Append only if no existing item shares the id. Same idempotency contract
    the Problem Lifecycle Sentinel uses — a persisting condition must not flood."""
    try:
        items = json.loads(path.read_text())
        if not isinstance(items, list):
            return False
    except Exception:
        items = []
    if any(isinstance(i, dict) and i.get("id") == entry["id"] for i in items):
        return False
    items.append(entry)
    path.write_text(json.dumps(items, indent=2))
    return True


def thesis_watch(report: dict, escalate: bool = True) -> dict:
    """Re-test the two conditions that would overturn the DO-NOT-SHORT verdict.

    Every test is a DELTA vs the verdict-time baseline. With no baseline on file
    the watch is inert by design — it would otherwise read the status quo as
    breaking news."""
    period   = datetime.now(timezone.utc).strftime("%Y-%m")
    base     = _baseline()
    credit   = _credit_channel(base)
    cands    = report.get("candidates", [])
    triggers = []

    # ENTRY_WINDOW — the documented entry is a name that was excluded as TOO
    # EXTENDED rallying back toward its 50MA into tradeable range, or a name
    # entering the candidate set that was not in it at verdict time. Names
    # already on the baseline list were judged NOT a trade on those same
    # readings, so their continued presence is not a signal.
    b_cands    = set(base.get("candidates", []))
    b_excluded = base.get("excluded_too_extended", {})
    new_names, recovered = [], []
    for c in cands:
        tk, pct = c["ticker"], c.get("extension_pctile", 50.0)
        if tk in b_excluded:
            # A name excluded at verdict time only becomes news once it has
            # genuinely climbed out of the squeeze zone. Still-extended is the
            # status quo, NOT a "newly qualifying" name — do not fall through.
            if pct >= ENTRY_PCTILE_RECOVERY:
                recovered.append(f"{tk} ({b_excluded[tk]:.0f}th -> {pct:.0f}th pctile)")
        elif tk not in b_cands:
            new_names.append(f"{tk} ({pct:.0f}th pctile)")

    entry = bool(base) and bool(recovered or new_names)
    if entry:
        bits = []
        if recovered:
            bits.append("recovered out of the squeeze zone: " + ", ".join(recovered))
        if new_names:
            bits.append("newly qualifying: " + ", ".join(new_names))
        triggers.append({
            "condition": "ENTRY_WINDOW",
            "id": f"kr-short-entry-{period}",
            "severity": "MEDIUM",
            "detail": (f"The rally-back-toward-the-50MA entry the 2026-07-30 verdict named has "
                       f"appeared — {'; '.join(bits)}. KR-stress signal is "
                       f"{report.get('signal', {}).get('level', '?')}."),
        })
    if credit.get("cracking"):
        triggers.append({
            "condition": "CREDIT_CHANNEL",
            "id": f"kr-short-credit-{period}",
            "severity": "HIGH",
            "detail": (f"AI-capex CREDIT channel is repricing — the risk the verdict flagged as "
                       f"the one that would actually crack. {credit['detail']}"),
        })

    raised = []
    if escalate:
        for t in triggers:
            entry_rec = {
                "id": t["id"],
                "source": "Tech-Short Pod (thesis watch)",
                "severity": t["severity"],
                "description": (
                    f"[{t['condition']}] The DO-NOT-SHORT verdict on the Korea/AI-hardware "
                    f"thesis (STRAT-2026-0730-kr-tech-short) has a flip condition firing. "
                    f"{t['detail']} Re-examine the thesis; this is an alert, not a trade — "
                    f"the short book still requires the live KR-stress gate."
                ),
                "status": "awaiting_chairman",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if _append_deduped(CHAIRMAN_PATH, entry_rec):
                raised.append(t["id"])

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "verdict_under_review": "STRAT-2026-0730-kr-tech-short (DO NOT SHORT)",
        "baseline_as_of": base.get("as_of", "MISSING — watch inert"),
        "entry_window_open": entry,
        "credit_channel": credit,
        "triggers": triggers,
        "escalated": raised,
        "status": ("VERDICT STANDS — no flip condition has moved since "
                   f"{base.get('as_of', '?')}") if not triggers else "FLIP CONDITION FIRING",
    }


def run(write: bool = True) -> dict:
    report = screen()
    # The watch reads the finished screen, so a non-empty candidate list is
    # itself the ENTRY_WINDOW signal — one definition, no duplicated logic.
    report["thesis_watch"] = thesis_watch(report, escalate=write)
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
