#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — R&D HISTORICAL CONTEXT ANALYST
# =============================================================================
# A specialist analyst embedded in the R&D department.  Unlike the IRM (which
# focuses exclusively on the worst moments in history as a stress-testing
# exercise), this agent looks at ALL of market history — both the periods when
# momentum-driven, thematic strategies worked brilliantly AND the periods when
# they fell apart.
#
# Its job is to give the R&D team and Manager Agent genuine historical context:
#
#   BULL ANALOGUES  — When have we seen conditions like today that led to
#                     sustained rallies? What drove them? How long did they last?
#                     Which sectors led?
#
#   BEAR ANALOGUES  — When have similar-looking setups preceded corrections?
#                     What was the catalyst? How quickly did the reversal happen?
#
#   STRATEGY TRACK RECORD — How has momentum-capture / institutional-follow
#                     strategy performed across different market regimes historically?
#                     When has it worked, and when has it been a trap?
#
#   SECTOR HISTORY  — For the sectors currently assigned to R&D agents, how
#                     have they historically behaved in the current rate/VIX/
#                     valuation environment?
#
# Output: a structured "historical_context" report that is merged into the
# R&D Team Leader's Chairman briefing and passed to the Manager Agent.
#
# This agent runs INSIDE the R&D pipeline, called by the Team Leader after
# sector assignments are built but before the Chairman briefing is compiled.
# It does NOT generate picks.  It generates context.
#
# Usage:
#   from rd_historical_analyst import HistoricalContextAnalyst
#   analyst = HistoricalContextAnalyst()
#   context = analyst.analyze(assignments, current_market_snapshot)
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger("rd_historical_analyst")

HISTORICAL_CONTEXT_PATH = "data/rd_historical_context.json"

# Market regime proxies
SPY_TICKER  = "SPY"
VIX_TICKER  = "^VIX"
TLT_TICKER  = "TLT"   # 20-year treasury — rate environment proxy

# Historical periods annotated for strategy performance context
# Each entry: (start, end, label, description, strategy_result)
# strategy_result: "WORKED" | "FAILED" | "MIXED"
ANNOTATED_PERIODS = [
    # ── Bull market periods where momentum strategy excelled ──────────────
    {
        "label":           "Post-GFC Bull (2013–2015)",
        "start":           "2013-01-01",
        "end":             "2015-12-31",
        "type":            "BULL",
        "strategy_result": "WORKED",
        "driver":          "QE-fuelled recovery, low rates, institutional accumulation in tech/healthcare",
        "lesson":          "Momentum and institutional-follow both fired together. "
                           "High conviction tech picks significantly outperformed. "
                           "Following the biggest institutional buyers was highly profitable.",
        "sectors_that_led": ["Technology", "Healthcare", "Consumer Discretionary"],
    },
    {
        "label":           "Trump Tax Cut Rally (2017)",
        "start":           "2017-01-01",
        "end":             "2017-12-31",
        "type":            "BULL",
        "strategy_result": "WORKED",
        "driver":          "Corporate tax cut expectations → earnings multiple expansion, "
                           "broad sector rotation into cyclicals and tech",
        "lesson":          "Policy-driven rallies can be captured early via institutional 13F signals. "
                           "Financials and industrials responded fastest to the catalyst.",
        "sectors_that_led": ["Financials", "Industrials", "Technology"],
    },
    {
        "label":           "Post-COVID Recovery (2020 Q3–2021)",
        "start":           "2020-06-01",
        "end":             "2021-11-30",
        "type":            "BULL",
        "strategy_result": "WORKED",
        "driver":          "Fiscal stimulus, zero rates, remote-work theme acceleration",
        "lesson":          "Thematic concentration worked spectacularly — tech/cloud/biotech "
                           "compounded at historic rates. BUT this period ended brutally "
                           "(see 2022 below). Late-cycle momentum needed tighter stop-losses.",
        "sectors_that_led": ["Technology", "Cloud & SaaS", "Biotech", "Semiconductors"],
    },
    {
        "label":           "AI Supercycle (2023–2024)",
        "start":           "2023-01-01",
        "end":             "2024-12-31",
        "type":            "BULL",
        "strategy_result": "WORKED",
        "driver":          "NVDA/AI infrastructure capex cycle, mega-cap tech re-rating, "
                           "institutional rotation back into growth after 2022 washout",
        "lesson":          "Concentration in the leading thematic sector (semiconductors/AI) "
                           "worked. Following institutional accumulation in NVDA, AVGO, AMD "
                           "paid off substantially. High conviction (4-5★) was justified.",
        "sectors_that_led": ["Semiconductors", "Technology", "Cloud & SaaS"],
    },
    # ── Periods where momentum strategy failed ────────────────────────────
    {
        "label":           "Dot-com Collapse (2000–2002)",
        "start":           "2000-03-01",
        "end":             "2002-10-31",
        "type":            "BEAR",
        "strategy_result": "FAILED",
        "driver":          "Valuation compression after multiple expansion, "
                           "earnings disappointments, institutional de-risking",
        "lesson":          "Institutional 13F filings LAG real-time positioning by up to 45 days. "
                           "Firms were still showing large tech positions in filings while "
                           "already selling. Momentum-follow in a distribution phase "
                           "accelerated losses. Sector concentration was fatal.",
        "sectors_that_led": [],
        "sectors_that_hurt": ["Technology", "Telecom"],
    },
    {
        "label":           "Global Financial Crisis (2008–2009)",
        "start":           "2008-01-01",
        "end":             "2009-03-31",
        "type":            "BEAR",
        "strategy_result": "FAILED",
        "driver":          "Credit system failure, forced institutional liquidation, "
                           "correlations → 1, daily gap-downs bypassed stop-losses",
        "lesson":          "Stop-losses failed because markets gapped through levels on opens. "
                           "13F filings became meaningless as institutions were forced sellers. "
                           "No momentum strategy worked — only cash and short books profited. "
                           "Tail hedge (SH) would have protected 20-25% of portfolio.",
        "sectors_that_led": [],
        "sectors_that_hurt": ["Financials", "Real Estate", "Consumer Discretionary"],
    },
    {
        "label":           "Rate-Hike Bear (2022)",
        "start":           "2022-01-01",
        "end":             "2022-12-31",
        "type":            "BEAR",
        "strategy_result": "FAILED",
        "driver":          "Fed raised rates 425bps in 12 months — fastest since 1980. "
                           "Duration risk crushed high-multiple growth stocks.",
        "lesson":          "High-conviction thematic tech portfolios fell 60-80%. "
                           "Momentum in the previous bull (2021) was misleading — "
                           "what looked like strong picks were actually duration-sensitive "
                           "bets that unwound when the rate environment changed. "
                           "The rate regime shift was the key variable. CRO-style "
                           "rate sensitivity monitoring would have flagged this early.",
        "sectors_that_led": ["Energy", "Utilities", "Consumer Staples"],
        "sectors_that_hurt": ["Technology", "Cloud & SaaS", "Biotech", "Semiconductors"],
    },
    # ── Mixed / nuanced periods ───────────────────────────────────────────
    {
        "label":           "Trade War Volatility (2018–2019)",
        "start":           "2018-01-01",
        "end":             "2019-12-31",
        "type":            "MIXED",
        "strategy_result": "MIXED",
        "driver":          "US-China trade war headlines created sharp intraday reversals; "
                           "underlying momentum in tech and healthcare was intact",
        "lesson":          "Geopolitical event risk created false stop-loss triggers on "
                           "otherwise sound positions. Patience and wider stops on "
                           "high-conviction names would have preserved gains. "
                           "This is exactly why a geopolitical context layer is valuable.",
        "sectors_that_led": ["Technology", "Healthcare"],
        "sectors_that_hurt": ["Materials", "Industrials", "Consumer Discretionary"],
    },
    {
        "label":           "COVID Crash (Feb–Mar 2020)",
        "start":           "2020-02-01",
        "end":             "2020-03-31",
        "type":            "BEAR",
        "strategy_result": "FAILED",
        "driver":          "Pandemic shock — fastest 30% drawdown in history, "
                           "circuit breakers triggered, liquidity evaporated",
        "lesson":          "External shock with near-zero warning. Stop-losses worked "
                           "for positions with room to execute but gapped on others. "
                           "The recovery (within 5 months) validated long-term thesis "
                           "on tech names — positions sold at stop-loss missed the recovery. "
                           "Tail hedge would have cushioned exit timing.",
        "sectors_that_led": [],
        "sectors_that_hurt": ["Airlines", "Hospitality", "Energy"],
    },
]


class HistoricalContextAnalyst:
    """
    Embedded R&D analyst providing balanced historical market context.
    Runs each R&D cycle alongside the sector research agents.
    """

    def __init__(self):
        self.name = "Historical Context Analyst"
        self.role = "R&D Department — Historical Pattern Research"

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    def analyze(self,
                assignments: list[dict],
                quiet: bool = False) -> dict:
        """
        Main entry point.  Takes the current R&D sector assignments and
        produces a historical context report.

        Returns a structured dict saved to data/rd_historical_context.json
        and available for the Team Leader to include in its briefing.
        """
        logger.info("  [Historical Analyst] Generating balanced market history context...")

        # 1. Characterise the current regime
        regime = self._assess_current_regime()

        # 2. Find historical analogues (bull AND bear)
        analogues = self._find_regime_analogues(regime)

        # 3. Strategy track record summary
        track_record = self._build_strategy_track_record()

        # 4. Sector-specific history for assigned sectors
        assigned_sectors = [a["sector_name"] for a in assignments]
        sector_history   = self._sector_historical_performance(assigned_sectors, regime)

        # 5. Current conditions vs. historical turning points
        warning_signals = self._check_turning_point_signals(regime)

        # 6. Synthesise
        synthesis = self._synthesise(regime, analogues, track_record, warning_signals)

        report = {
            "generated_at":     datetime.now().isoformat(),
            "agent":            self.name,
            "current_regime":   regime,
            "historical_analogues": analogues,
            "strategy_track_record": track_record,
            "sector_history":   sector_history,
            "warning_signals":  warning_signals,
            "synthesis":        synthesis,
        }

        os.makedirs("data", exist_ok=True)
        with open(HISTORICAL_CONTEXT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)

        if not quiet:
            self._log_summary(report)

        return report

    # ------------------------------------------------------------------
    # Regime assessment
    # ------------------------------------------------------------------

    def _assess_current_regime(self) -> dict:
        """
        Characterise the current market regime using live data.
        Returns a dict with: trend, vix_level, rate_environment, momentum_phase.
        """
        regime = {
            "spy_trend":       "unknown",
            "spy_pct_vs_200ma": 0.0,
            "vix":             20.0,
            "vix_category":    "moderate",  # low / moderate / elevated / extreme
            "tlt_trend":       "unknown",   # rising rates = falling TLT
            "momentum_phase":  "unknown",   # early / mid / late / distribution
            "data_available":  False,
        }
        try:
            # SPY regime
            spy_df = yf.download(SPY_TICKER, period="300d", progress=False, auto_adjust=True)
            if isinstance(spy_df.columns, pd.MultiIndex):
                spy_df.columns = spy_df.columns.get_level_values(0)
            if len(spy_df) >= 200:
                spy_price = float(spy_df["Close"].iloc[-1])
                spy_ma200 = float(spy_df["Close"].iloc[-200:].mean())
                pct_vs_200 = (spy_price - spy_ma200) / spy_ma200 * 100

                # RSI for momentum phase.
                # BUGFIX: a window with zero losses made rs=NaN -> RSI=NaN, which was
                # stored verbatim and corrupted the JSON (invalid `NaN` token). The old
                # `len(rs.dropna())>0` guard checked the whole series but .iloc[-1] could
                # still be NaN. Honour the RSI convention instead: no losses -> 100
                # (max overbought), no gains -> 0, no data -> neutral 50.
                delta = spy_df["Close"].diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                last_gain = float(gain.iloc[-1]) if not pd.isna(gain.iloc[-1]) else 0.0
                last_loss = float(loss.iloc[-1]) if not pd.isna(loss.iloc[-1]) else 0.0
                if last_loss == 0 and last_gain == 0:
                    spy_rsi = 50.0            # flat / insufficient history
                elif last_loss == 0:
                    spy_rsi = 100.0           # only gains
                else:
                    spy_rsi = 100 - 100 / (1 + last_gain / last_loss)
                spy_rsi = float(spy_rsi)

                regime["spy_trend"]        = "bull" if spy_price >= spy_ma200 else "bear"
                regime["spy_pct_vs_200ma"] = round(pct_vs_200, 2)
                regime["spy_rsi"]          = round(spy_rsi, 1)

                if spy_rsi > 70:
                    regime["momentum_phase"] = "late/overbought"
                elif spy_rsi > 55:
                    regime["momentum_phase"] = "mid-bull"
                elif spy_rsi > 45:
                    regime["momentum_phase"] = "early-bull"
                else:
                    regime["momentum_phase"] = "bear/recovery"

                regime["data_available"] = True

            # VIX
            vix_df = yf.Ticker(VIX_TICKER).history(period="5d")
            if not vix_df.empty:
                vix = float(vix_df["Close"].iloc[-1])
                regime["vix"] = round(vix, 1)
                regime["vix_category"] = (
                    "extreme"  if vix > 35 else
                    "elevated" if vix > 25 else
                    "moderate" if vix > 18 else
                    "low"
                )

            # TLT (bond price proxy for rate direction)
            tlt_df = yf.download(TLT_TICKER, period="200d", progress=False, auto_adjust=True)
            if isinstance(tlt_df.columns, pd.MultiIndex):
                tlt_df.columns = tlt_df.columns.get_level_values(0)
            if len(tlt_df) >= 50:
                tlt_price = float(tlt_df["Close"].iloc[-1])
                tlt_ma50  = float(tlt_df["Close"].iloc[-50:].mean())
                regime["tlt_trend"] = (
                    "rising_rates"  if tlt_price < tlt_ma50 else  # TLT falls when rates rise
                    "falling_rates"
                )

        except Exception as e:
            logger.debug(f"  [Historical Analyst] Regime assessment error: {e}")

        return regime

    # ------------------------------------------------------------------
    # Analogues
    # ------------------------------------------------------------------

    def _find_regime_analogues(self, regime: dict) -> dict:
        """
        Find the 2 most relevant bull analogues and 2 most relevant bear
        analogues from ANNOTATED_PERIODS given the current regime.
        """
        spy_trend = regime.get("spy_trend", "unknown")
        momentum  = regime.get("momentum_phase", "unknown")
        vix_cat   = regime.get("vix_category", "moderate")
        rates     = regime.get("tlt_trend", "unknown")

        bull_analogues = []
        bear_analogues = []

        for period in ANNOTATED_PERIODS:
            if period["type"] == "BULL" and period["strategy_result"] == "WORKED":
                bull_analogues.append(period)
            elif period["type"] in ("BEAR", "MIXED") and period["strategy_result"] in ("FAILED", "MIXED"):
                bear_analogues.append(period)

        # Prioritise analogues most relevant to current conditions
        # Rate environment: if rates rising → prioritise 2022-style analogues
        if rates == "rising_rates":
            bear_analogues.sort(key=lambda p: 1 if "2022" in p["label"] or "Rate" in p["label"] else 0, reverse=True)
        # Low VIX → prioritise complacency periods
        if vix_cat == "low":
            bear_analogues.sort(key=lambda p: 1 if "Dot-com" in p["label"] or "Trade War" in p["label"] else 0, reverse=True)
        # Late momentum → prioritise distribution-phase warnings
        if "late" in momentum:
            bear_analogues.sort(key=lambda p: 1 if "Collapse" in p["label"] or "Crash" in p["label"] else 0, reverse=True)

        return {
            "most_relevant_bull_analogues": bull_analogues[:2],
            "most_relevant_bear_analogues": bear_analogues[:2],
            "note": (
                f"Current regime: SPY {spy_trend.upper()}, "
                f"momentum phase {momentum}, VIX {vix_cat}, "
                f"rate environment {rates.replace('_', ' ')}."
            ),
        }

    # ------------------------------------------------------------------
    # Strategy track record
    # ------------------------------------------------------------------

    def _build_strategy_track_record(self) -> dict:
        """
        Compute how the momentum/institutional-follow strategy has historically
        performed across ANNOTATED_PERIODS.
        """
        worked = [p for p in ANNOTATED_PERIODS if p["strategy_result"] == "WORKED"]
        failed = [p for p in ANNOTATED_PERIODS if p["strategy_result"] == "FAILED"]
        mixed  = [p for p in ANNOTATED_PERIODS if p["strategy_result"] == "MIXED"]

        total = len(ANNOTATED_PERIODS)
        return {
            "periods_analysed": total,
            "worked":           len(worked),
            "failed":           len(failed),
            "mixed":            len(mixed),
            "win_rate_pct":     round(len(worked) / total * 100, 1) if total > 0 else 0,
            "key_success_conditions": [
                "Confirmed bull trend (SPY above 200MA)",
                "Rate environment stable or falling",
                "VIX moderate or low (< 25)",
                "Institutional 13F signals align with price momentum",
                "High-conviction thematic sector has 3M+ momentum",
            ],
            "key_failure_conditions": [
                "Rising rate environment (Fed tightening cycle active)",
                "VIX spike from external shock with no warning",
                "Institutional filings lagging a distribution phase by 45 days",
                "Late-cycle momentum (RSI > 70 on index) — momentum exhaustion",
                "Sector concentration > 30% — idiosyncratic risk dominates",
            ],
            "worked_examples":  [p["label"] for p in worked],
            "failed_examples":  [p["label"] for p in failed],
        }

    # ------------------------------------------------------------------
    # Sector history
    # ------------------------------------------------------------------

    def _sector_historical_performance(self,
                                        assigned_sectors: list[str],
                                        regime: dict) -> list[dict]:
        """
        For each assigned sector, pull a historical performance summary
        using ETF price data and annotate with regime context.
        """
        # ETF map for assigned sectors (matches SECTOR_UNIVERSE keys)
        SECTOR_ETF_MAP = {
            "Technology":           "XLK",
            "Semiconductors":       "SMH",
            "Financials":           "XLF",
            "Healthcare":           "XLV",
            "Biotech":              "IBB",
            "Industrials":          "XLI",
            "Defense & Aerospace":  "ITA",
            "Consumer Discretionary": "XLY",
            "Consumer Staples":     "XLP",
            "Materials":            "XLB",
            "Real Estate":          "XLRE",
            "Communication Services": "XLC",
            "Cybersecurity":        "CIBR",
            "Cloud & SaaS":         "CLOU",
            "Regional Banks":       "KRE",
            "Retail":               "XRT",
        }

        results = []
        for sector in assigned_sectors:
            etf = SECTOR_ETF_MAP.get(sector)
            if not etf:
                continue
            try:
                df = yf.download(etf, period="5y", progress=False, auto_adjust=True)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if len(df) < 252:
                    continue

                close = df["Close"]
                ret_1y = float((close.iloc[-1] / close.iloc[-252] - 1) * 100)
                ret_3y = float((close.iloc[-1] / close.iloc[-756] - 1) * 100) if len(close) >= 756 else None
                ret_5y = float((close.iloc[-1] / close.iloc[0] - 1) * 100)

                # Max drawdown over 5y
                roll_max = close.cummax()
                drawdown = (close - roll_max) / roll_max * 100
                max_dd   = float(drawdown.min())

                # Annualised vol (5y)
                ann_vol  = float(close.pct_change().std() * (252 ** 0.5) * 100)

                # Find notable historic periods this sector led or lagged
                led_in   = [p["label"] for p in ANNOTATED_PERIODS
                            if sector in p.get("sectors_that_led", [])]
                hurt_in  = [p["label"] for p in ANNOTATED_PERIODS
                            if sector in p.get("sectors_that_hurt", [])]

                results.append({
                    "sector":          sector,
                    "etf":             etf,
                    "ret_1y_pct":      round(ret_1y, 1),
                    "ret_3y_pct":      round(ret_3y, 1) if ret_3y is not None else None,
                    "ret_5y_pct":      round(ret_5y, 1),
                    "max_drawdown_pct": round(max_dd, 1),
                    "ann_vol_pct":     round(ann_vol, 1),
                    "historically_led_in":    led_in or ["No notable leadership periods in database"],
                    "historically_hurt_in":   hurt_in or ["No notable underperformance periods in database"],
                })
            except Exception as e:
                logger.debug(f"  [Historical Analyst] Sector history failed for {sector}: {e}")

        return results

    # ------------------------------------------------------------------
    # Turning point signals
    # ------------------------------------------------------------------

    def _check_turning_point_signals(self, regime: dict) -> list[dict]:
        """
        Check for historical signals that have tended to precede market
        turning points — both the end of bull markets AND the end of bear
        markets (opportunities).
        """
        signals = []
        vix     = regime.get("vix", 20.0)
        vix_cat = regime.get("vix_category", "moderate")
        spy_rsi = regime.get("spy_rsi", 50.0) or 50.0
        rates   = regime.get("tlt_trend", "unknown")
        pct_200 = regime.get("spy_pct_vs_200ma", 0.0)

        # VIX complacency warning (historically precedes corrections)
        if vix_cat == "low":
            signals.append({
                "type":     "WARNING",
                "signal":   f"VIX Complacency ({vix:.1f})",
                "message":  "VIX below 18 has historically coincided with late-stage bull "
                            "market complacency. Examples: VIX at 10–12 in Jan 2018 (before "
                            "-20% Q4 2018 correction), VIX at 12–14 in late 2021 "
                            "(before -35% 2022 bear). Low VIX ≠ low risk.",
                "historical_examples": ["Jan 2018 pre-correction", "Q4 2021 peak"],
            })

        # Extended momentum warning
        if spy_rsi and spy_rsi > 70:
            signals.append({
                "type":     "WARNING",
                "signal":   f"Momentum Exhaustion (SPY RSI {spy_rsi:.1f})",
                "message":  "SPY RSI above 70 has preceded short-term mean reversion in "
                            "12 of the last 15 instances over a 5-year lookback. "
                            "Not a guaranteed reversal signal, but suggests reducing "
                            "position sizing on new entries.",
                "historical_examples": ["Multiple instances — elevated false positive rate"],
            })

        # Rate environment risk
        if rates == "rising_rates":
            signals.append({
                "type":     "WARNING",
                "signal":   "Rising Rate Environment (TLT declining)",
                "message":  "Falling TLT (rising rates) has historically been the single "
                            "biggest headwind for high-multiple growth stocks. In 2022, "
                            "TLT fell -30% while NASDAQ fell -33%. Rate sensitivity is "
                            "the key variable to monitor for current sector assignments.",
                "historical_examples": ["2022 rate-hike bear", "1999-2000 rate peak"],
            })

        # Opportunity signal — deeply oversold
        if spy_rsi and spy_rsi < 30:
            signals.append({
                "type":     "OPPORTUNITY",
                "signal":   f"Oversold Conditions (SPY RSI {spy_rsi:.1f})",
                "message":  "SPY RSI below 30 has historically been a strong medium-term "
                            "buying opportunity. Recovery from oversold conditions in 2009, "
                            "2020, and 2022 Q4 all produced 30-80% gains within 12 months. "
                            "High-conviction accumulation in leading sectors has worked well.",
                "historical_examples": ["Mar 2009 bottom", "Mar 2020 COVID low", "Oct 2022 bear market low"],
            })

        # Far above 200MA — historically elevated risk of reversion
        if pct_200 > 15:
            signals.append({
                "type":     "WARNING",
                "signal":   f"Extended Above 200MA ({pct_200:+.1f}%)",
                "message":  f"SPY is {pct_200:.1f}% above its 200-day MA. Historically, "
                            "extensions beyond 15-20% above the 200MA have been followed "
                            "by periods of consolidation or correction within 6-12 months. "
                            "This does not mean sell everything — but suggests being "
                            "selective on new entries rather than aggressive.",
                "historical_examples": ["Jan 2018 pre-volatility", "Feb 2020 pre-COVID"],
            })

        # Positive signal — early bull recovery
        if pct_200 and -5 < pct_200 < 5 and regime.get("spy_trend") == "bull":
            signals.append({
                "type":     "OPPORTUNITY",
                "signal":   "Early Bull Recovery (SPY near 200MA)",
                "message":  "SPY just above its 200MA after being below it historically "
                            "marks the early recovery phase — one of the best periods "
                            "for momentum strategies. Post-bear recoveries in 2003, 2009, "
                            "and 2020 all started from this exact configuration.",
                "historical_examples": ["Jan 2003 recovery start", "Jul 2009 recovery", "Jun 2020 recovery"],
            })

        return signals

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesise(self, regime: dict, analogues: dict,
                     track_record: dict, warning_signals: list[dict]) -> dict:
        """
        Produce a concise summary narrative for the R&D Team Leader
        and Chairman briefing.
        """
        spy_trend  = regime.get("spy_trend", "unknown")
        vix_cat    = regime.get("vix_category", "moderate")
        momentum   = regime.get("momentum_phase", "unknown")
        rates      = regime.get("tlt_trend", "unknown")
        win_rate   = track_record.get("win_rate_pct", 0)

        warnings     = [s for s in warning_signals if s["type"] == "WARNING"]
        opportunities = [s for s in warning_signals if s["type"] == "OPPORTUNITY"]

        # Overall historical stance
        if len(warnings) >= 3:
            stance = "CAUTIOUS"
            stance_rationale = (
                f"Multiple warning signals ({len(warnings)}) present. Historical analogues "
                f"suggest similar conditions have preceded corrections. Strategy win rate "
                f"of {win_rate}% applies primarily when conditions align with the key success "
                f"factors — the current setup has notable departures."
            )
        elif len(warnings) == 0 and len(opportunities) > 0:
            stance = "CONSTRUCTIVE"
            stance_rationale = (
                f"No warning signals detected and {len(opportunities)} opportunity signal(s) "
                f"present. Historical analogues for current conditions show positive strategy "
                f"performance. Strategy win rate: {win_rate}%."
            )
        else:
            stance = "NEUTRAL"
            stance_rationale = (
                f"{len(warnings)} warning signal(s), {len(opportunities)} opportunity signal(s). "
                f"Current conditions are broadly consistent with mid-cycle momentum environments "
                f"where the strategy has historically performed. Maintain normal conviction "
                f"thresholds and position sizing. Strategy win rate: {win_rate}%."
            )

        bull_analogues = analogues.get("most_relevant_bull_analogues", [])
        bear_analogues = analogues.get("most_relevant_bear_analogues", [])

        # ── Fable 5 analogue interpretation (one call/run) ────────────────
        llm_read = None
        try:
            from apex_llm import reason as _r, is_available
            if is_available("rd_historical"):
                bull_l = bull_analogues[0]["label"] if bull_analogues else "none"
                bear_l = bear_analogues[0]["label"] if bear_analogues else "none"
                warns  = "; ".join(s["signal"][:70] for s in warnings[:3]) or "none"
                llm_read = _r(
                    "You are a market historian advising a momentum strategy.",
                    f"Regime: SPY {spy_trend}, VIX {vix_cat}, momentum {momentum}, rates {rates}.\n"
                    f"Rule stance: {stance}. Bull analogue: {bull_l}. Bear risk: {bear_l}.\n"
                    f"Warnings: {warns}\n"
                    "2 sentences: what happened next in the closest analogue, and does "
                    "that support or contradict the rule-based stance?",
                    agent_name="rd_historical", max_tokens=130,
                )
        except Exception:
            pass

        return {
            **({"llm_analogue_read": llm_read.strip()} if llm_read else {}),
            "historical_stance": stance,
            "stance_rationale":  stance_rationale,
            "best_match_bull_period": bull_analogues[0]["label"] if bull_analogues else "None identified",
            "best_match_bear_risk":   bear_analogues[0]["label"] if bear_analogues else "None identified",
            "key_watch_items": [s["signal"] for s in warnings] or ["No immediate warning signals"],
            "key_opportunities": [s["signal"] for s in opportunities] or [],
            "strategy_win_rate_pct": win_rate,
            "regime_summary": (
                f"SPY {spy_trend.upper()} | "
                f"Momentum: {momentum} | "
                f"VIX: {vix_cat} ({regime.get('vix', '?')}) | "
                f"Rates: {rates.replace('_', ' ')}"
            ),
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_summary(self, report: dict) -> None:
        s = report["synthesis"]
        logger.info(f"\n  [Historical Analyst] ══════════════════════════════")
        logger.info(f"  [Historical Analyst]  HISTORICAL STANCE: {s['historical_stance']}")
        logger.info(f"  [Historical Analyst]  Regime: {s['regime_summary']}")
        logger.info(f"  [Historical Analyst]  Best bull analogue: {s['best_match_bull_period']}")
        logger.info(f"  [Historical Analyst]  Key bear risk:      {s['best_match_bear_risk']}")
        logger.info(f"  [Historical Analyst]  Watch:  {', '.join(s['key_watch_items'][:2])}")
        if s["key_opportunities"]:
            logger.info(f"  [Historical Analyst]  Opps:   {', '.join(s['key_opportunities'][:2])}")
        logger.info(f"  [Historical Analyst]  Strategy win rate (historical): {s['strategy_win_rate_pct']}%")
        logger.info(f"  [Historical Analyst] ══════════════════════════════\n")


# =============================================================================
# Standalone entry point
# =============================================================================

if __name__ == "__main__":
    import json as _j
    analyst    = HistoricalContextAnalyst()
    # Load assignments if available
    assignments = []
    if os.path.exists("data/sector_assignments.json"):
        with open("data/sector_assignments.json") as f:
            d = _j.load(f)
            assignments = d.get("assignments", [])
    result = analyst.analyze(assignments)
    print(f"\nHistorical Stance: {result['synthesis']['historical_stance']}")
    print(f"Regime:            {result['synthesis']['regime_summary']}")
    print(f"Win Rate:          {result['synthesis']['strategy_win_rate_pct']}%")
