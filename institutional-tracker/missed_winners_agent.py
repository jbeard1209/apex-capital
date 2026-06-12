#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MISSED WINNERS AGENT (R&D Division)
# =============================================================================
# Chairman directive 2026-06-12: monitor the market's biggest winners and
# answer "did our pipeline ever see this ticker, and if so why didn't we own it?"
#
# For each top performer over the lookback window, classifies coverage as:
#   NEVER_SEEN     — ticker not in any watchlist or research output (blind spot)
#   SEEN_NO_PICK   — research agents covered it but never produced a pick
#   PICKED_REJECTED— a pick existed but Manager/board rejected it (with reason)
#   TRADED         — we actually traded it (captured)
#
# Learning loop:
#   1. data/missed_winners_report.json   — full report
#   2. NEVER_SEEN tickers → appended to data/missed_winners_watchlist.json,
#      which research_team reads to expand coverage
#   3. ML: each missed winner becomes a labeled example suggestion in
#      data/ml_suggestions.json (features of stocks we systematically miss)
#   4. Fable 5 synthesis: the pattern across misses (sector? size? momentum
#      profile?) and one concrete process change
#
# Usage:  python3 missed_winners_agent.py [--months 6] [--top 20]
# Cron:   weekly (Sunday evening)
# =============================================================================

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("missed_winners")

DATA_DIR    = Path(__file__).parent / "data"
REPORT_PATH = DATA_DIR / "missed_winners_report.json"
WATCH_PATH  = DATA_DIR / "missed_winners_watchlist.json"
ML_SUGG     = DATA_DIR / "ml_suggestions.json"

# Liquid US large/mid-cap universe to rank (S&P 500 core + high-momentum names)
SCAN_UNIVERSE = [
    "NVDA","MSFT","AAPL","GOOGL","AMZN","META","AVGO","TSLA","AMD","NFLX",
    "CRM","ORCL","ADBE","PLTR","ARM","MRVL","TSM","SMCI","CRWD","PANW",
    "LLY","UNH","JNJ","ABBV","MRK","TMO","ISRG","VRTX","REGN","AMGN",
    "JPM","GS","MS","BAC","WFC","V","MA","BLK","SPGI","AXP","C","SCHW",
    "HD","MCD","NKE","SBUX","LOW","TJX","BKNG","CMG","DIS","UBER",
    "CAT","DE","HON","GE","RTX","LMT","BA","UNP","ETN","EMR",
    "XOM","CVX","COP","SLB","EOG","OXY","PSX","MPC",
    "LIN","APD","FCX","NEM","SHW","NUE",
    "PG","KO","PEP","COST","WMT","TGT","CL","MDLZ",
    "NEE","DUK","SO","D","SRE",
    "PLD","AMT","EQIX","CCI",
    "T","VZ","TMUS","CMCSA",
    "RKLB","IONQ","RGTI","COIN","HOOD","SOFI","SHOP","SQ","SNOW","NET",
    "DDOG","MDB","ABNB","DASH","RDDT","APP","VRT","ANET","CEG","VST",
]

# Pipeline files that prove the firm "saw" a ticker
RESEARCH_SOURCES = [
    ("research_team_report.json",  "R&D research"),
    ("rd_ml_bridge_signals.json",  "RD-ML bridge"),
    ("energy_research_report.json","Energy team"),
    ("fe_picks_queue.json",        "FE pipeline"),
    ("fi_picks_queue.json",        "FI pipeline"),
    ("etf_research_brief.json",    "ETF research"),
    ("sector_assignments.json",    "Sector watchlists"),
]


def _load(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _collect_json_tickers(obj, found: set) -> None:
    """Recursively pull plausible ticker strings out of any pipeline JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("ticker", "symbol", "etf") and isinstance(v, str):
                found.add(v.upper())
            elif k == "watchlist" and isinstance(v, list):
                found.update(str(t).upper() for t in v if isinstance(t, str))
            else:
                _collect_json_tickers(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_tickers(item, found)


def _pipeline_coverage() -> dict[str, set]:
    """Map: source label → set of tickers that source has ever surfaced."""
    coverage = {}
    for fname, label in RESEARCH_SOURCES:
        data = _load(DATA_DIR / fname)
        if data is None:
            continue
        seen: set = set()
        _collect_json_tickers(data, seen)
        coverage[label] = seen
    return coverage


def _traded_tickers() -> dict[str, str]:
    """Tickers we actually traded or hold, with context."""
    out = {}
    state = _load(DATA_DIR / "state.json", {})
    for t in state.get("entry_dates", {}):
        out[t.upper()] = "currently held"
    tr = _load(DATA_DIR / "trade_review_report.json", {})
    for t in tr.get("trades", []):
        tk = str(t.get("ticker", "")).upper()
        if tk and tk not in out:
            out[tk] = f"closed {t.get('outcome','?')} {t.get('pnl_pct',0):+.1f}%"
    return out


def _rejected_picks() -> dict[str, str]:
    """Tickers that reached the Manager but were rejected/HELD, with reason."""
    out = {}
    mgr = _load(DATA_DIR / "manager_decisions.json", [])
    if not isinstance(mgr, list):
        return out
    for cycle in mgr[-60:]:
        for d in (cycle.get("decisions", []) if isinstance(cycle, dict) else []):
            tk = str(d.get("ticker", "")).upper()
            if not tk:
                continue
            action = d.get("action", "")
            mgr_action = d.get("manager_action", "")
            if action in ("BUY_NEW", "ADD") and mgr_action in ("HOLD", "WATCH"):
                reason = "; ".join(d.get("manager_rationale", [])[:1])[:120]
                out[tk] = reason or "buy gated by manager"
    return out


def _returns(tickers: list[str], months: int) -> dict[str, float]:
    """Total return % over the window for each ticker (batch download)."""
    import yfinance as yf
    period = f"{months * 31}d"
    rets = {}
    # Batch in chunks of 50 to be polite to yfinance
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            df = yf.download(chunk, period=period, auto_adjust=True,
                             progress=False)["Close"]
            for t in chunk:
                try:
                    s = df[t].dropna() if len(chunk) > 1 else df.dropna()
                    if len(s) >= 2 and s.iloc[0] > 0:
                        rets[t] = round((s.iloc[-1] / s.iloc[0] - 1) * 100, 1)
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Batch download failed: %s", e)
    return rets


def run(months: int = 6, top_n: int = 20) -> dict:
    logger.info("=" * 60)
    logger.info("  MISSED WINNERS AGENT — %dM lookback, top %d", months, top_n)
    logger.info("=" * 60)

    rets = _returns(SCAN_UNIVERSE, months)
    if not rets:
        logger.error("No return data — aborting.")
        return {}
    winners = sorted(rets.items(), key=lambda x: -x[1])[:top_n]

    coverage = _pipeline_coverage()
    all_seen = set().union(*coverage.values()) if coverage else set()
    traded   = _traded_tickers()
    rejected = _rejected_picks()

    results, never_seen = [], []
    for ticker, ret in winners:
        if ticker in traded:
            status, detail = "TRADED", traded[ticker]
        elif ticker in rejected:
            status, detail = "PICKED_REJECTED", rejected[ticker]
        elif ticker in all_seen:
            srcs = [lbl for lbl, s in coverage.items() if ticker in s]
            status, detail = "SEEN_NO_PICK", f"covered by {', '.join(srcs[:3])} but no pick generated"
        else:
            status, detail = "NEVER_SEEN", "not in any watchlist or research output"
            never_seen.append(ticker)
        results.append({"ticker": ticker, "return_pct": ret,
                        "status": status, "detail": detail})
        logger.info("  %-6s %+7.1f%%  %-16s %s", ticker, ret, status, detail[:60])

    n_by = {}
    for r in results:
        n_by[r["status"]] = n_by.get(r["status"], 0) + 1
    capture_rate = (n_by.get("TRADED", 0) / len(results) * 100) if results else 0

    report = {
        "generated_at":  datetime.now().isoformat(),
        "agent":         "Missed Winners Agent (R&D)",
        "lookback_months": months,
        "universe_size": len(SCAN_UNIVERSE),
        "top_n":         top_n,
        "capture_rate_pct": round(capture_rate, 1),
        "status_counts": n_by,
        "winners":       results,
    }

    # ── Fable 5 pattern synthesis (one call/run) ──────────────────────────
    try:
        from apex_llm import reason as _r, is_available
        if is_available("missed_winners"):
            rows = "\n".join(f"{r['ticker']} {r['return_pct']:+.0f}% [{r['status']}]"
                             for r in results[:15])
            raw = _r(
                "You audit why a momentum fund missed the market's biggest winners.",
                f"Top winners last {months}M and our coverage status:\n{rows}\n"
                f"Capture rate: {capture_rate:.0f}%. Never-seen: {never_seen or 'none'}\n"
                "3 sentences: the common pattern in what we miss (sector/theme/profile), "
                "the single process change that would have caught the most, and which "
                "ONE never-seen ticker most deserves research coverage now.",
                agent_name="missed_winners", max_tokens=200,
            )
            if raw:
                report["llm_pattern_read"] = raw.strip()
                logger.info("  [LLM] %s", raw.strip()[:120])
    except Exception:
        pass

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

    # ── Learning loop 1: expand research watchlist ────────────────────────
    wl = _load(WATCH_PATH, {"tickers": [], "history": []})
    added = [t for t in never_seen if t not in wl["tickers"]]
    wl["tickers"] = (wl["tickers"] + added)[-60:]
    wl["history"].append({"ts": datetime.now().isoformat(), "added": added})
    wl["history"] = wl["history"][-26:]
    wl["updated_at"] = datetime.now().isoformat()
    WATCH_PATH.write_text(json.dumps(wl, indent=2))
    if added:
        logger.info("  Watchlist: +%d never-seen winners → %s", len(added), added)

    # ── Learning loop 2: ML suggestion (study what we systematically miss) ─
    if never_seen:
        sugg = _load(ML_SUGG, [])
        if not isinstance(sugg, list):
            sugg = []
        sid = f"missed_winners_{datetime.now().strftime('%Y%m%d')}"
        if not any(s.get("id") == sid for s in sugg):
            sugg.append({
                "id": sid, "source": "missed_winners_agent",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "category": "DATA", "priority": "REVIEW",
                "title": f"Label and study {len(never_seen)} never-seen winners",
                "description": (
                    f"Top-{top_n} performers absent from all pipelines: {never_seen}. "
                    "Add these as positive examples to the label set and check which "
                    "model features they share — the model can learn the profile of "
                    "winners our watchlists structurally exclude."),
                "status": "PENDING",
            })
            ML_SUGG.write_text(json.dumps(sugg, indent=2))
            logger.info("  ML suggestion queued: study never-seen winner profile")

    # ── Learning loop 3: notify the firm ──────────────────────────────────
    try:
        from firm_channel import post
        post("Missed Winners Agent", "RESEARCH_NOTE",
             f"📊 MISSED WINNERS ({months}M): capture rate {capture_rate:.0f}% of top {top_n}. "
             f"Never seen: {', '.join(never_seen[:6]) or 'none'}. "
             + (report.get("llm_pattern_read", "")[:200]),
             priority="HIGH" if capture_rate < 25 else "NORMAL")
    except Exception:
        pass

    logger.info("  Report → %s | capture rate %.0f%%", REPORT_PATH, capture_rate)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--top",    type=int, default=20)
    args = ap.parse_args()
    run(months=args.months, top_n=args.top)
