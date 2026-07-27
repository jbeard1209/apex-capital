# =============================================================================
# APEX CAPITAL — RESEARCH & DEVELOPMENT TEAM
# =============================================================================
# Eight sector-agnostic Research Agents who receive their assignments from the
# R&D Team Leader. Each agent is handed a sector, a watchlist, and a momentum
# context brief; they then score every stock on the watchlist and return their
# top picks to the ResearchTeamCoordinator.
#
# The energy sector is EXCLUDED from all research per firm policy.
# Sector assignments change every 4 days based on the R&D Leader's scan.
#
# Output: data/research_team_report.json
#         (read by Manager Agent and R&D Team Leader)
#
# Usage:
#   python3 research_team.py              # load assignments + run + save
#   python3 research_team.py --quiet      # save only
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import time
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Suppress yfinance's noisy internal logging ────────────────────────────────
# yfinance logs individual ticker download failures as ERROR/WARNING to the root
# logger, which inflates our pipeline error counts (CIT flags 5000+ "errors").
# Our _fetch() already handles failures gracefully with try/except → None.
# We only want CRITICAL messages from yfinance (e.g., auth failure).
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)   # yfinance SQLite ORM

# ── Fix yfinance SQLite cache path ────────────────────────────────────────────
# Default cache lives in the current working directory; when cron runs from a
# different directory the database file can't be opened.  Point it at a
# stable user-home path so it always resolves correctly.
try:
    import os as _os
    _yf_cache = _os.path.expanduser("~/.cache/yfinance")
    _os.makedirs(_yf_cache, exist_ok=True)
    yf.set_tz_cache_location(_yf_cache)
except Exception:
    pass  # non-fatal — yfinance works without a cache

ASSIGNMENTS_PATH  = "data/sector_assignments.json"
REPORT_PATH       = "data/research_team_report.json"

# Energy-related keywords — block any watchlist ticker that sneaks in
ENERGY_BLOCK = {
    "XLE", "XOP", "AMLP", "OIH", "FUELCELL",
    # well-known energy tickers explicitly blocked
    "XOM", "CVX", "COP", "OXY", "DVN", "EOG", "SLB", "HAL",
    "LNG", "AR", "FANG", "PSX", "MPC", "VLO",
    "NEE", "AEP", "SO", "DUK", "XEL", "ETR", "PCG", "SRE", "EIX",
    "FSLR", "ENPH", "RUN", "SEDG", "BEP", "CWEN", "ARRY", "NOVA",
    "CEG", "VST", "CCJ", "BWXT", "SMR", "OKLO", "NNE", "UEC", "LEU",
    "CSIQ", "JKS", "DQ", "GNE",
    "PLUG", "BE", "CLNE", "GTLS",
    "TSLA",   # in Energy Storage context — blocked; allowed in Consumer Disc context
    "LIN", "APD",  # industrial gases — allowed in Materials but not if assigned as "energy transition"
}


# =============================================================================
# SECTOR RESEARCH AGENT
# =============================================================================

class SectorResearchAgent:
    """
    A single research agent assigned to one sector by the R&D Team Leader.
    Receives a sector_assignment dict and scores every stock on its watchlist.
    """

    def __init__(self, assignment: dict):
        self.agent_id           = assignment["agent_id"]
        self.sector_name        = assignment["sector_name"]
        # FE pipeline agents pass 'etf_symbol'; R&D assignments pass 'sector_etf'
        self.sector_etf         = assignment.get("sector_etf") or assignment.get("etf_symbol", "")
        self.description        = assignment["description"]
        self.watchlist          = [
            t for t in assignment["watchlist"]
            if t not in ENERGY_BLOCK
        ]
        self.momentum_data      = assignment.get("momentum_data", {})
        self.investment_horizon = assignment.get("investment_horizon", "both")
        self.leader_rationale   = assignment.get("leader_rationale", "")

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _fetch(self, ticker: str, _retries: int = 2) -> Optional[pd.DataFrame]:
        """Download price history with silent retry on transient failures."""
        for attempt in range(_retries + 1):
            try:
                df = yf.download(ticker, period="260d", progress=False,
                                 auto_adjust=True, timeout=15)
                if df is None or df.empty:
                    return None
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df if len(df) > 20 else None
            except Exception:
                if attempt < _retries:
                    time.sleep(1)   # brief pause before retry
                continue
        return None

    def _fundamentals(self, ticker: str) -> dict:
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            result = {
                "forward_pe":     info.get("forwardPE"),
                "revenue_growth": info.get("revenueGrowth"),
                "profit_margins": info.get("profitMargins"),
                "market_cap":     info.get("marketCap"),
                "dividend_yield": info.get("dividendYield"),
                "short_name":     info.get("shortName", ticker),
                "current_price":  info.get("currentPrice") or info.get("regularMarketPrice"),
                "beta":           info.get("beta"),
            }
            # Explicitly release the Ticker object so its internal SQLite cache
            # connection is closed promptly rather than waiting for GC.
            del t
            return result
        except Exception:
            return {"short_name": ticker}

    def _momentum(self, df: pd.DataFrame) -> dict:
        if df is None or len(df) < 20:
            return {"trend": "unknown", "rsi_14": 50.0,
                    "pct_from_50ma": 0.0, "pct_from_200ma": 0.0,
                    "above_50ma": False, "above_200ma": False}
        close = df["Close"]
        price  = float(close.iloc[-1])
        ma50   = float(close.iloc[-50:].mean())  if len(close) >= 50  else float(close.mean())
        ma200  = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50

        from technical_indicators import rsi_last
        rsi = rsi_last(close)

        if price >= ma50 and price >= ma200:
            trend = "bullish"
        elif price < ma50 and price < ma200:
            trend = "bearish"
        else:
            trend = "neutral"

        return {
            "trend":          trend,
            "rsi_14":         round(rsi,                               1),
            "pct_from_50ma":  round((price - ma50)  / ma50  * 100,    2),
            "pct_from_200ma": round((price - ma200) / ma200 * 100,    2),
            "above_50ma":     price >= ma50,
            "above_200ma":    price >= ma200,
        }

    def _conviction(self, mom: dict, fund: dict) -> int:
        """Score 1–5. Weights momentum more for short-term, fundamentals for long-term."""
        score = 0

        # Trend (max 2)
        if mom["trend"] == "bullish":
            score += 2
        elif mom["trend"] == "neutral":
            score += 1

        # RSI sweet spot 45–65 (max 1)
        rsi = mom.get("rsi_14", 50)
        if 45 <= rsi <= 65:
            score += 1

        # Fundamentals — long-term bonus (max 2)
        if self.investment_horizon in ("long_term", "both"):
            rev = fund.get("revenue_growth")
            if rev and rev > 0.08:
                score += 1
            fpe = fund.get("forward_pe")
            if fpe and 0 < fpe < 35:
                score += 1

        return max(1, min(5, score))

    # ------------------------------------------------------------------
    # Per-ticker analysis
    # ------------------------------------------------------------------

    def _analyse_ticker(self, ticker: str) -> Optional[dict]:
        df   = self._fetch(ticker)
        fund = self._fundamentals(ticker)
        mom  = self._momentum(df)

        price = fund.get("current_price")
        if not price and df is not None and len(df):
            price = float(df["Close"].iloc[-1])
        if not price:
            return None

        conviction = self._conviction(mom, fund)

        # Investment thesis: combine sector context + momentum + valuation
        thesis_parts = []
        trend_str = mom["trend"]
        rsi_val   = mom.get("rsi_14", 50)

        if trend_str == "bullish":
            thesis_parts.append(
                f"Bullish trend ({mom['pct_from_50ma']:+.1f}% vs 50MA, "
                f"{mom['pct_from_200ma']:+.1f}% vs 200MA)"
            )
        elif trend_str == "neutral":
            thesis_parts.append("At trend inflection — momentum building")
        else:
            thesis_parts.append("Under pressure — contrarian or recovery thesis required")

        if 45 <= rsi_val <= 65:
            thesis_parts.append(f"RSI {rsi_val:.0f} — momentum sweet spot")
        elif rsi_val > 70:
            thesis_parts.append(f"RSI {rsi_val:.0f} — overbought; wait for pullback entry")
        else:
            thesis_parts.append(f"RSI {rsi_val:.0f}")

        fpe = fund.get("forward_pe")
        if fpe and 0 < fpe < 40:
            thesis_parts.append(f"Fwd P/E {fpe:.1f}x")

        rev = fund.get("revenue_growth")
        if rev:
            thesis_parts.append(f"Rev growth {rev*100:+.0f}% YoY")

        thesis = " | ".join(thesis_parts)
        full_reason = (
            f"[{self.sector_name}] {fund.get('short_name', ticker)}: {thesis} "
            f"| Conviction: {'★'*conviction}"
        )

        # Fable 5 investment thesis
        llm_thesis = None
        try:
            from apex_llm import reason as _r, is_available
            if conviction >= 3 and is_available("review_agent"):
                _ctx = (
                    f"Ticker: {ticker} | Sector: {self.sector_name}\n"
                    f"Price: ${price:.2f} | Fwd P/E: {fpe:.1f}x | "
                    f"Rev growth: {rev*100:+.0f}% YoY\n" if (fpe and rev) else
                    f"Ticker: {ticker} | Sector: {self.sector_name} | Price: ${price:.2f}\n"
                )
                _ctx += (
                    f"Momentum: {mom.get('trend','?')}, RSI={mom.get('rsi_14','?')}, "
                    f"vs 50MA: {mom.get('pct_from_50ma',0):+.1f}%, "
                    f"vs 200MA: {mom.get('pct_from_200ma',0):+.1f}%\n"
                    f"Conviction: {conviction}★\n\n"
                    "Write a 1-2 sentence investment thesis for this pick. "
                    "Be specific about WHY this stock is interesting right now."
                )
                raw = _r(
                    f"You are an Apex Capital research analyst covering {self.sector_name}. "
                    "Write precise, specific investment theses for stock picks. "
                    "Lead with the most compelling catalyst or signal.",
                    _ctx,
                    agent_name="review_agent",
                    max_tokens=150,
                )
                if raw:
                    llm_thesis = raw.strip()
        except Exception:
            pass

        reasons = [full_reason]
        if llm_thesis:
            reasons.append(f"[THESIS] {llm_thesis}")

        return {
            "ticker":             ticker,
            "short_name":         fund.get("short_name", ticker),
            "current_price":      round(price, 2),
            "market_cap":         fund.get("market_cap"),
            "forward_pe":         fpe,
            "revenue_growth_pct": round(rev * 100, 1) if rev else None,
            "beta":               fund.get("beta"),
            "momentum":           mom,
            "conviction":         conviction,
            "reasons":            reasons,
        }

    # ------------------------------------------------------------------
    # Main research run
    # ------------------------------------------------------------------

    def run(self) -> dict:
        # Read coaching note from HR/performance ledger at cycle start
        _coaching_note = None
        try:
            from performance_ledger import get_coaching_note
            _coaching_note = get_coaching_note(f"Agent {self.agent_id}")
            if not _coaching_note:
                # Also try sector-based agent name
                _coaching_note = get_coaching_note(self.sector_name)
            if _coaching_note:
                logger.info(
                    f"  [Agent {self.agent_id}] HR coaching note received: "
                    f"{_coaching_note[:80]}..."
                )
        except Exception:
            pass

        logger.info(
            f"  [Agent {self.agent_id} — {self.sector_name}] "
            f"Analysing {len(self.watchlist)} tickers..."
        )
        scored = []
        for ticker in self.watchlist:
            result = self._analyse_ticker(ticker)
            if result:
                scored.append(result)

        # Sort: conviction desc, then RSI proximity to sweet spot
        scored.sort(
            key=lambda x: (
                x["conviction"],
                -(abs(x["momentum"]["rsi_14"] - 55) if x["momentum"]["rsi_14"] else 99),
            ),
            reverse=True,
        )

        top_picks = scored[:3]

        # Build manager-compatible recommendation dicts
        recommendations = []
        for pick in top_picks:
            if pick["conviction"] < 3:
                continue
            recommendations.append({
                "ticker":             pick["ticker"],
                "side":               "long",
                "qty":                None,
                "avg_entry":          None,
                "current_price":      pick["current_price"],
                "unrealized_pnl_pct": 0.0,
                "hold_days":          0,
                "weight_pct":         0.0,
                "momentum":           pick["momentum"],
                "action":             "BUY_NEW",
                "priority":           3 if pick["conviction"] >= 4 else 4,
                "conviction":         pick["conviction"],
                "investment_horizon": self.investment_horizon,
                "source_agent":       f"Agent {self.agent_id}",
                "specialty":          self.sector_name,
                "reasons":            pick["reasons"],
                "fundamentals": {
                    "forward_pe":         pick.get("forward_pe"),
                    "revenue_growth_pct": pick.get("revenue_growth_pct"),
                    "market_cap":         pick.get("market_cap"),
                },
            })

        logger.info(
            f"  [Agent {self.agent_id} — {self.sector_name}] "
            f"→ {len(recommendations)} pick(s) from {len(scored)} analysed"
        )

        # Record picks in performance ledger
        try:
            from performance_ledger import record_pick
            for _rec in recommendations:
                record_pick(
                    agent=f"Agent {self.agent_id} ({self.sector_name})",
                    ticker=_rec["ticker"],
                    conviction=_rec["conviction"],
                    prediction_type=_rec.get("action", "BUY"),
                )
        except Exception:
            pass

        return {
            "agent_id":           self.agent_id,
            "sector_name":        self.sector_name,
            "sector_etf":         self.sector_etf,
            "description":        self.description,
            "investment_horizon": self.investment_horizon,
            "tickers_analysed":   len(scored),
            "top_picks":          top_picks,
            "recommendations":    recommendations,
            "generated_at":       datetime.now().isoformat(),
        }


# =============================================================================
# RESEARCH TEAM COORDINATOR
# =============================================================================

class ResearchTeamCoordinator:
    """
    Runs all 8 SectorResearchAgents with their assignments from the R&D Leader,
    consolidates picks, and saves to data/research_team_report.json.
    """

    def run(self, assignments: Optional[list] = None,
            quiet: bool = False) -> dict:

        if assignments is None:
            # Load from disk (written by R&D Team Leader)
            if not os.path.exists(ASSIGNMENTS_PATH):
                logger.error("No sector assignments found. Run rd_team_leader.py first.")
                return {}
            with open(ASSIGNMENTS_PATH) as f:
                assignments = json.load(f).get("assignments", [])

        if not assignments:
            logger.warning("Sector assignments list is empty.")
            return {}

        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — RESEARCH & DEVELOPMENT TEAM")
        logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"  Running {len(assignments)} sector research agent(s)...")
        logger.info("=" * 60)

        agent_reports   = []
        all_picks       = []
        seen_tickers    = set()

        for asgn in assignments:
            agent = SectorResearchAgent(asgn)
            try:
                report = agent.run()
                agent_reports.append(report)

                for rec in report["recommendations"]:
                    ticker = rec["ticker"]
                    if ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_picks.append(rec)
                    else:
                        for i, existing in enumerate(all_picks):
                            if existing["ticker"] == ticker:
                                if rec["conviction"] > existing["conviction"]:
                                    all_picks[i] = rec
                                break
            except Exception as e:
                logger.error(f"  [Agent {asgn['agent_id']}] FAILED: {e}")

        # ── Load and run Chief-of-Staff spawned registry agents ──────────
        # These agents were generated by the AgentFactory and run alongside
        # the standard R&D assignments every research cycle.
        REGISTRY_PATH = "data/agent_registry.json"
        if os.path.exists(REGISTRY_PATH):
            try:
                import importlib.util
                with open(REGISTRY_PATH) as f:
                    registry = json.load(f)
                active_agents = [
                    a for a in registry.get("agents", [])
                    if a.get("status") in ("active", "reassigned")
                    # CIT/governance agents live in the registry for HR
                    # visibility but are not research agents — don't run them
                    # here (they have .analyze(), not .run(), and crash).
                    and a.get("division", "research") == "research"
                ]
                if active_agents:
                    logger.info(
                        f"  Loading {len(active_agents)} CoS-spawned registry agent(s) "
                        f"({sum(1 for a in active_agents if a.get('status')=='active')} active, "
                        f"{sum(1 for a in active_agents if a.get('status')=='reassigned')} reassigned)..."
                    )
                for entry in active_agents:
                    try:
                        fpath      = entry["file"]
                        class_name = entry["class_name"]
                        if not os.path.exists(fpath):
                            continue
                        spec   = importlib.util.spec_from_file_location(class_name, fpath)
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        AgentClass = getattr(module, class_name)
                        reg_agent  = AgentClass()
                        reg_report = reg_agent.run()
                        agent_reports.append(reg_report)
                        for rec in reg_report["recommendations"]:
                            ticker = rec["ticker"]
                            if ticker not in seen_tickers:
                                seen_tickers.add(ticker)
                                all_picks.append(rec)
                            else:
                                for i, existing in enumerate(all_picks):
                                    if existing["ticker"] == ticker:
                                        if rec["conviction"] > existing["conviction"]:
                                            all_picks[i] = rec
                                        break
                    except Exception as e:
                        logger.warning(
                            f"  [CoS Agent {entry.get('class_name','?')}] FAILED: {e}"
                        )
            except Exception as e:
                logger.warning(f"  Could not load agent registry: {e}")

        # Sort all picks by conviction desc
        all_picks.sort(key=lambda r: r["conviction"], reverse=True)

        summary = {
            # Use .get() — CoS-spawned agents may use "tickers_scanned" or omit the key entirely
            "total_tickers_analysed": sum(r.get("tickers_analysed", 0) for r in agent_reports),
            "total_picks":            len(all_picks),
            "high_conviction":        sum(1 for r in all_picks if r.get("conviction", 0) >= 4),
            "agents_run":             len(agent_reports),
            "sectors_covered":        [r.get("sector_name") or r.get("segment", "unknown")
                                       for r in agent_reports],
        }

        report = {
            "generated_at":    datetime.now().isoformat(),
            "team":            "Research & Development Team",
            "summary":         summary,
            "agent_reports":   agent_reports,
            "recommendations": all_picks,
        }

        os.makedirs("data", exist_ok=True)
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"\n  Research report saved → {REPORT_PATH}")
        logger.info(
            f"  Sectors: {', '.join(summary['sectors_covered'][:4])}..."
            if len(summary['sectors_covered']) > 4
            else f"  Sectors: {', '.join(summary['sectors_covered'])}"
        )
        logger.info(
            f"  Picks: {summary['total_picks']} total, "
            f"{summary['high_conviction']} high-conviction (4★+)"
        )

        if not quiet:
            _print_report(report)

        return report


def _print_report(report: dict) -> None:
    s = report["summary"]
    print(f"\n{'═'*66}")
    print("  APEX CAPITAL — R&D TEAM RESEARCH REPORT")
    print(f"  {report['generated_at']}")
    print(f"{'═'*66}")
    print(f"  Sectors covered   : {', '.join(s['sectors_covered'])}")
    print(f"  Tickers analysed  : {s['total_tickers_analysed']}")
    print(f"  Total picks       : {s['total_picks']}")
    print(f"  High conviction   : {s['high_conviction']}")
    print(f"\n  {'TICKER':<7} {'SECTOR':<25} {'CONV':<6} {'TREND':<10} {'RSI':>5}")
    print("  " + "─" * 58)
    for rec in report["recommendations"]:
        mom  = rec.get("momentum", {})
        conv = "★" * rec["conviction"]
        print(
            f"  {rec['ticker']:<7} {rec.get('specialty','?'):<25} "
            f"{conv:<6} {mom.get('trend','?'):<10} {mom.get('rsi_14', 0):>5.1f}"
        )
        if rec.get("reasons"):
            print(f"    → {rec['reasons'][0][:70]}")
    print(f"{'═'*66}\n")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    quiet_flag = "--quiet" in sys.argv
    coordinator = ResearchTeamCoordinator()
    coordinator.run(quiet=quiet_flag)
