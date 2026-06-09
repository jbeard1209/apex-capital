# =============================================================================
# APEX CAPITAL — ENERGY RESEARCH TEAM
# =============================================================================
# Eight specialized research agents, each covering a distinct energy sub-sector.
# Every 4 days, each agent analyses its watchlist for short- and long-term
# investment opportunities, scores candidates, and hands picks to the
# EnergyResearchCoordinator, which consolidates and saves a unified report
# for the Manager Agent to act on.
#
# Agents:
#   1. USRenewablesAgent         — Solar, wind, clean utilities
#   2. USFossilFuelsAgent        — US oil, gas & LNG
#   3. USNuclearAgent            — Nuclear power & uranium
#   4. USGridInfrastructureAgent — Power grid, utilities, transmission
#   5. ChineseEnergyAgent        — US-listed China energy & EV
#   6. GlobalCleantechAgent      — International green transition plays
#   7. EnergyStorageAgent        — Batteries, storage, EV infrastructure
#   8. EnergyTransitionAgent     — Hydrogen, critical minerals, carbon capture
#
# Output: data/energy_research_report.json
#
# Usage:
#   python3 energy_research_team.py            # run all agents, print + save
#   python3 energy_research_team.py --quiet    # save only
# =============================================================================

import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

RESEARCH_OUTPUT_PATH = "data/energy_research_report.json"


# =============================================================================
# BASE AGENT
# =============================================================================

class BaseEnergyResearchAgent(ABC):
    """
    Abstract base for all energy research agents.
    Subclasses define name, specialty, watchlist, and long-term thesis.
    """

    name:      str = "BaseAgent"
    specialty: str = "General Energy"
    watchlist: list[str] = []
    investment_horizon: str = "long_term"   # 'short_term' | 'long_term' | 'both'

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _fetch(self, ticker: str, period: str = "260d") -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df if len(df) > 20 else None
        except Exception as e:
            logger.debug(f"    Price fetch failed for {ticker}: {e}")
            return None

    def _fundamentals(self, ticker: str) -> dict:
        try:
            info = yf.Ticker(ticker).info
            return {
                "forward_pe":       info.get("forwardPE"),
                "trailing_pe":      info.get("trailingPE"),
                "price_to_book":    info.get("priceToBook"),
                "revenue_growth":   info.get("revenueGrowth"),      # YoY, decimal
                "profit_margins":   info.get("profitMargins"),
                "debt_to_equity":   info.get("debtToEquity"),
                "market_cap":       info.get("marketCap"),
                "dividend_yield":   info.get("dividendYield"),
                "sector":           info.get("sector", ""),
                "short_name":       info.get("shortName", ticker),
                "current_price":    info.get("currentPrice") or info.get("regularMarketPrice"),
            }
        except Exception:
            return {}

    def _momentum(self, df: pd.DataFrame) -> dict:
        if df is None or len(df) < 20:
            return {"trend": "unknown", "rsi_14": 50.0,
                    "pct_from_50ma": 0.0, "pct_from_200ma": 0.0}
        close = df["Close"]
        price  = float(close.iloc[-1])
        ma50   = float(close.iloc[-50:].mean())  if len(close) >= 50  else float(close.mean())
        ma200  = float(close.iloc[-200:].mean()) if len(close) >= 200 else ma50

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1]) if len(rs.dropna()) > 0 else 50.0

        if price >= ma50 and price >= ma200:
            trend = "bullish"
        elif price < ma50 and price < ma200:
            trend = "bearish"
        else:
            trend = "neutral"

        return {
            "trend":          trend,
            "rsi_14":         round(rsi, 1),
            "pct_from_50ma":  round((price - ma50)  / ma50  * 100, 2),
            "pct_from_200ma": round((price - ma200) / ma200 * 100, 2),
            "above_50ma":     price >= ma50,
            "above_200ma":    price >= ma200,
        }

    def _conviction_score(self, mom: dict, fund: dict, horizon: str) -> int:
        """
        Return 1–5 conviction score.
        Long-term weighting favours fundamentals; short-term favours momentum.
        """
        score = 0

        # Momentum component (max 3 pts)
        if mom["trend"] == "bullish":
            score += 2
        elif mom["trend"] == "neutral":
            score += 1

        if mom["rsi_14"] and 40 < mom["rsi_14"] < 70:
            score += 1

        # Fundamental component (max 2 pts for long-term)
        if horizon in ("long_term", "both"):
            rev_growth = fund.get("revenue_growth")
            if rev_growth is not None and rev_growth > 0.10:   # >10% YoY growth
                score += 1
            fpe = fund.get("forward_pe")
            if fpe is not None and 0 < fpe < 30:               # reasonable valuation
                score += 1

        return max(1, min(5, score))

    # ------------------------------------------------------------------
    # Core analysis loop
    # ------------------------------------------------------------------

    def score_ticker(self, ticker: str) -> Optional[dict]:
        df   = self._fetch(ticker)
        fund = self._fundamentals(ticker)
        mom  = self._momentum(df)

        price = fund.get("current_price")
        if not price and df is not None:
            price = float(df["Close"].iloc[-1])
        if not price:
            return None

        conviction = self._conviction_score(mom, fund, self.investment_horizon)

        return {
            "ticker":             ticker,
            "short_name":         fund.get("short_name", ticker),
            "current_price":      round(price, 2) if price else None,
            "market_cap":         fund.get("market_cap"),
            "forward_pe":         fund.get("forward_pe"),
            "revenue_growth_pct": round(fund["revenue_growth"] * 100, 1)
                                  if fund.get("revenue_growth") else None,
            "profit_margin_pct":  round(fund["profit_margins"] * 100, 1)
                                  if fund.get("profit_margins") else None,
            "dividend_yield_pct": round(fund["dividend_yield"] * 100, 2)
                                  if fund.get("dividend_yield") else None,
            "momentum":           mom,
            "conviction":         conviction,
        }

    @abstractmethod
    def sector_thesis(self) -> str:
        """One-paragraph macro thesis for this sub-sector."""

    @abstractmethod
    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        """One-sentence investment rationale for a specific ticker."""

    # ------------------------------------------------------------------
    # Main research run
    # ------------------------------------------------------------------

    def run_research(self) -> dict:
        logger.info(f"  [{self.name}] Analysing {len(self.watchlist)} tickers ...")
        scored_list = []

        for ticker in self.watchlist:
            result = self.score_ticker(ticker)
            if result:
                scored_list.append(result)

        # Sort: conviction desc, then RSI (avoid overbought >70)
        scored_list.sort(
            key=lambda x: (
                x["conviction"],
                -(abs(x["momentum"]["rsi_14"] - 55) if x["momentum"]["rsi_14"] else 99),
            ),
            reverse=True,
        )

        # Top 3 picks
        top_picks = scored_list[:3]

        # Format as manager-compatible recommendations
        recommendations = []
        for pick in top_picks:
            if pick["conviction"] < 3:
                continue   # skip low-conviction picks
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
                "source_agent":       self.name,
                "specialty":          self.specialty,
                "reasons":            [self.ticker_thesis(pick["ticker"], pick)],
                "fundamentals": {
                    "forward_pe":         pick["forward_pe"],
                    "revenue_growth_pct": pick["revenue_growth_pct"],
                    "profit_margin_pct":  pick["profit_margin_pct"],
                    "market_cap":         pick["market_cap"],
                },
            })

        logger.info(
            f"  [{self.name}] → {len(recommendations)} actionable pick(s) "
            f"from {len(scored_list)} analysed"
        )

        return {
            "agent":              self.name,
            "specialty":          self.specialty,
            "investment_horizon": self.investment_horizon,
            "sector_thesis":      self.sector_thesis(),
            "tickers_analysed":   len(scored_list),
            "top_picks":          top_picks,
            "recommendations":    recommendations,
            "generated_at":       datetime.now().isoformat(),
        }


# =============================================================================
# AGENT 1 — US RENEWABLES & SOLAR
# =============================================================================

class USRenewablesAgent(BaseEnergyResearchAgent):
    name      = "USRenewablesAgent"
    specialty = "US Renewables & Solar"
    watchlist = ["FSLR", "ENPH", "NEE", "RUN", "SEDG", "BEP", "CWEN",
                 "ARRY", "AES", "NOVA", "HASI", "ORA"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "US renewable energy is structurally supported by the Inflation Reduction Act "
            "(IRA) tax credits through 2032, falling solar/wind LCOE, and corporate net-zero "
            "commitments. First Solar dominates US-manufactured thin-film solar, insulated from "
            "Chinese tariffs. NextEra remains the world's largest renewable utility. Near-term "
            "headwind: high interest rates compress project IRRs for leveraged developers."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "FSLR": "US-manufactured thin-film solar; IRA domestic content bonus; booked through 2026",
            "ENPH": "Microinverter monopoly with IQ8 product cycle; residential solar recovery thesis",
            "NEE": "World's largest renewable utility; regulated Florida base provides earnings floor",
            "RUN": "Residential solar + battery storage leader; cash-flow inflection as rates plateau",
            "SEDG": "Inverter market share recovery play after inventory correction; valuation depressed",
            "BEP": "Brookfield Renewable — diversified global renewable portfolio, 5%+ dividend",
            "CWEN": "Clearway Energy — contracted wind/solar with utility-grade PPA coverage",
            "ARRY": "Solar tracker leader; utility-scale solar growth drives backlog expansion",
            "AES": "Diversified global power; clean energy transition at scale",
            "NOVA": "Sunnova — residential solar financing; high-growth but rate-sensitive",
            "HASI": "Hannon Armstrong — green infrastructure REIT; direct IRA beneficiary",
            "ORA": "Ormat Technologies — geothermal baseload; secular growth + storage",
        }
        base = theses.get(ticker, f"{scored['short_name']}: bullish momentum, renewable sector tailwinds")
        conv = scored["conviction"]
        pe   = scored.get("forward_pe")
        pe_s = f" | Fwd P/E: {pe:.1f}x" if pe else ""
        return f"{base}{pe_s} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 2 — US OIL, GAS & LNG
# =============================================================================

class USFossilFuelsAgent(BaseEnergyResearchAgent):
    name      = "USFossilFuelsAgent"
    specialty = "US Oil, Gas & LNG"
    watchlist = ["XOM", "CVX", "COP", "EOG", "OXY", "DVN", "SLB",
                 "HAL", "LNG", "AR", "FANG", "PSX", "MPC"]
    investment_horizon = "both"

    def sector_thesis(self) -> str:
        return (
            "US oil & gas remains a cash-flow engine through the energy transition. LNG export "
            "capacity expanding to ~25 bcf/d by 2030 (Sabine Pass, Plaquemines, Corpus Christi) "
            "positions US as a key European and Asian supplier. Permian operators (COP, EOG, DVN) "
            "generate strong FCF at $65+ WTI. Oilfield services (SLB, HAL) benefit from "
            "international deepwater cycle. Risk: demand destruction if AI-driven efficiency "
            "accelerates electrification faster than expected."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "XOM":  "Exxon — Permian + Guyana growth, Pioneer acquisition integration, $20B buyback",
            "CVX":  "Chevron — Hess deal adds Guyana exposure; strong FCF yield at current oil prices",
            "COP":  "ConocoPhillips — best-in-class Permian operator; Marathon acquisition consolidates position",
            "EOG":  "EOG Resources — premium Permian + Utica; disciplined capex, 3%+ dividend",
            "OXY":  "Occidental — Berkshire-backed; Permian + Carbon CCS optionality via Stratos",
            "DVN":  "Devon Energy — Permian FCF machine; fixed + variable dividend policy",
            "SLB":  "SLB (Schlumberger) — international oilfield services recovery; AI drilling tools",
            "HAL":  "Halliburton — North America completions leverage; international margin expansion",
            "LNG":  "Cheniere — largest US LNG exporter; long-term contracted cash flows to 2050s",
            "AR":   "Antero Resources — Appalachian natgas; LNG export demand beneficiary",
            "FANG": "Diamondback Energy — low-cost Permian pure-play; Endeavor acquisition synergies",
            "PSX":  "Phillips 66 — refining + midstream + chemicals diversification",
            "MPC":  "Marathon Petroleum — largest US refiner by capacity; strong crack spread leverage",
        }
        base = theses.get(ticker, f"{scored['short_name']}: energy sector momentum")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 3 — US NUCLEAR & URANIUM
# =============================================================================

class USNuclearAgent(BaseEnergyResearchAgent):
    name      = "USNuclearAgent"
    specialty = "US Nuclear Power & Uranium"
    watchlist = ["CEG", "VST", "CCJ", "BWXT", "SMR", "OKLO", "NNE", "UEC", "LEU"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "Nuclear is experiencing a structural renaissance driven by AI datacenter power demand, "
            "corporate clean-energy PPAs (Microsoft/Google nuclear deals), and policy support "
            "(Restart of Three Mile Island by Constellation). SMR (Small Modular Reactor) companies "
            "are pre-revenue but represent a decade-long infrastructure build-out. Uranium supply "
            "is tight: Kazatomprom export restrictions + Cameco production discipline keep U3O8 "
            "elevated. Long-term holds: CEG (best near-term cash flow), CCJ (uranium supply), "
            "OKLO/SMR (SMR lottery tickets)."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "CEG":  "Constellation Energy — largest US nuclear fleet; Microsoft AI datacenter PPA secured",
            "VST":  "Vistra Energy — nuclear + gas generation; AI power demand beneficiary in Texas",
            "CCJ":  "Cameco — largest Western uranium miner; supply discipline supports U3O8 prices",
            "BWXT": "BWX Technologies — nuclear components + government services; SMR supplier",
            "SMR":  "NuScale Power — first NRC-approved SMR design; long-duration speculative",
            "OKLO": "Oklo — OpenAI-backed micro-reactor; pre-revenue but high-profile AI energy play",
            "NNE":  "Nano Nuclear Energy — micro-reactor concept stage; speculative long-term option",
            "UEC":  "Uranium Energy Corp — US uranium mining; benefits from domestic sourcing push",
            "LEU":  "Centrus Energy — uranium enrichment; strategic US fuel security asset",
        }
        base = theses.get(ticker, f"{scored['short_name']}: nuclear renaissance beneficiary")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 4 — US GRID & POWER INFRASTRUCTURE
# =============================================================================

class USGridInfrastructureAgent(BaseEnergyResearchAgent):
    name      = "USGridInfrastructureAgent"
    specialty = "US Grid & Power Infrastructure"
    watchlist = ["NEE", "AEP", "SO", "DUK", "XEL", "ETR", "PCG", "SRE", "EIX", "AWK", "EVRG"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "US grid infrastructure faces a $2–3 trillion investment requirement over the next "
            "decade: EV charging load growth, datacenter power demand (est. +50 GW by 2030), "
            "renewable integration, and aging transmission replacement. Regulated utilities "
            "provide predictable rate-base growth with 6–9% annual EPS targets. Key themes: "
            "Southern Company (nuclear Vogtle online), PG&E (California wildfire recovery), "
            "Eversource/AEP (transmission expansion). Rate environment normalization (lower "
            "rates) is a structural tailwind for dividend-paying utilities."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "NEE":  "NextEra — renewable generation + regulated Florida utility; 10% EPS growth target",
            "AEP":  "AEP — largest US transmission network; $43B capex plan through 2028",
            "SO":   "Southern Company — Vogtle nuclear online; regulated Southeast territory",
            "DUK":  "Duke Energy — Carolinas + Indiana regulated utility; grid modernization spend",
            "XEL":  "Xcel Energy — wind-heavy midwest utility; clean energy leader among utilities",
            "ETR":  "Entergy — Gulf Coast regulated utility; data center load growth in Texas/Louisiana",
            "PCG":  "PG&E — California utility; wildfire liability resolved; clean energy investment",
            "SRE":  "Sempra — California + Texas + LNG export; diversified energy infrastructure",
            "EIX":  "Edison International — Southern California utility; EV charging infrastructure",
            "AWK":  "American Water Works — largest US water utility; adjacent infrastructure play",
            "EVRG": "Evergy — Kansas/Missouri utility; data center growth corridor",
        }
        base = theses.get(ticker, f"{scored['short_name']}: grid infrastructure spend beneficiary")
        conv = scored["conviction"]
        div  = scored.get("dividend_yield_pct")
        div_s = f" | Yield: {div:.1f}%" if div else ""
        return f"{base}{div_s} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 5 — CHINA-LINKED ENERGY & EV
# =============================================================================

class ChineseEnergyAgent(BaseEnergyResearchAgent):
    name      = "ChineseEnergyAgent"
    specialty = "China-Linked Energy & EV"
    watchlist = ["CSIQ", "JKS", "DQ", "NIO", "XPEV", "LI", "BYDDF", "GNE", "ENLT"]
    investment_horizon = "both"

    def sector_thesis(self) -> str:
        return (
            "China dominates global solar manufacturing (80%+ of polysilicon, wafers, cells) "
            "and EV production (BYD surpassed Tesla in units). US-listed Chinese energy plays "
            "carry geopolitical risk (tariffs, PCAOB auditing, ADR delisting threat) but offer "
            "high growth at discounted valuations. Solar manufacturers (JKS, CSIQ) are expanding "
            "US/non-China capacity to avoid tariffs. Chinese EV makers (NIO, XPEV, LI) are "
            "expanding into Europe/Southeast Asia as domestic competition intensifies. "
            "Risk-adjusted position sizing is critical — treat as speculative allocation."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "CSIQ":  "Canadian Solar — global manufacturer with US/India capacity expansion; tariff-resilient",
            "JKS":   "JinkoSolar — world's largest solar module manufacturer; Southeast Asia diversification",
            "DQ":    "Daqo New Energy — low-cost polysilicon; margin recovery as oversupply normalises",
            "NIO":   "NIO — premium Chinese EV; battery-as-a-service model; expanding into Europe",
            "XPEV":  "XPeng — AI-first EV brand; ADAS leadership in China; MONA M03 ramp",
            "LI":    "Li Auto — EREV (range-extender EV) dominance; premium family SUV segment",
            "BYDDF": "BYD OTC — world's largest EV+battery company; vertically integrated; global expansion",
            "GNE":   "Genie Energy — US retail energy; defensive cash flow, not a China play",
            "ENLT":  "Enlight Renewable Energy — Israel-based global renewable developer",
        }
        base = theses.get(ticker, f"{scored['short_name']}: China energy/EV sector")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv} [ELEVATED GEOPOLITICAL RISK]"


# =============================================================================
# AGENT 6 — GLOBAL CLEANTECH & GREEN TRANSITION
# =============================================================================

class GlobalCleantechAgent(BaseEnergyResearchAgent):
    name      = "GlobalCleantechAgent"
    specialty = "Global Cleantech & Green Transition"
    watchlist = ["PLUG", "BE", "ITRI", "GTLS", "CLNE", "HASI", "SPWR", "NOVA", "CERE", "AMRC"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "Global clean energy transition is a multi-decade structural theme driven by "
            "falling technology costs, policy support (IRA, EU Green Deal, REPowerEU), and "
            "corporate sustainability commitments. Key near-term focus: grid modernization "
            "technology (Itron), clean natural gas infrastructure (Chart Industries), and "
            "fuel cell industrialisation (Bloom Energy datacenters). "
            "Hydrogen remains a long-duration speculative with Plug Power the bellwether. "
            "Select positions in carbon capture, demand response, and energy efficiency software."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "PLUG":  "Plug Power — hydrogen fuel cell leader; gigafactory build-out; speculative long-term",
            "BE":    "Bloom Energy — solid oxide fuel cells for datacenters; AI power demand tailwind",
            "ITRI":  "Itron — smart grid / smart meter infrastructure; utility capex beneficiary",
            "GTLS":  "Chart Industries — LNG/hydrogen equipment; carbon capture infrastructure",
            "CLNE":  "Clean Energy Fuels — renewable natural gas for trucking fleets",
            "HASI":  "Hannon Armstrong — green infrastructure REIT; IRA direct beneficiary",
            "SPWR":  "SunPower — residential solar installer; balance sheet risk, speculative",
            "NOVA":  "Sunnova — solar+storage financing; high leverage to rate environment",
            "CERE":  "Certara — biosimulation (adjacent to energy in pharma energy use)",
            "AMRC":  "Ameresco — energy efficiency + renewable project developer for institutions",
        }
        base = theses.get(ticker, f"{scored['short_name']}: global cleantech beneficiary")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 7 — BATTERY, STORAGE & EV INFRASTRUCTURE
# =============================================================================

class EnergyStorageAgent(BaseEnergyResearchAgent):
    name      = "EnergyStorageAgent"
    specialty = "Battery, Storage & EV Infrastructure"
    watchlist = ["TSLA", "ENVX", "QS", "STEM", "CHPT", "BLNK", "EVGO", "AMPX", "FLUX", "ARRY"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "Battery energy storage systems (BESS) are the critical enabler of renewable-heavy "
            "grids — smoothing intermittency and providing grid services. The 4-hour storage market "
            "is projected to grow 30x by 2035. Tesla's Megapack is the dominant utility-scale "
            "product. EV charging infrastructure (CHPT, BLNK, EVGO) is early-cycle with "
            "profitability still years away but strategic positioning for a 50M+ US EV fleet. "
            "Next-gen battery tech (Enovix solid-state, QuantumScape) is speculative but "
            "represents massive long-term upside if commercialised."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "TSLA":  "Tesla — Megapack BESS + EV + FSD optionality; energy segment growing fastest",
            "ENVX":  "Enovix — silicon-anode battery cells; consumer electronics → EV transition",
            "QS":    "QuantumScape — VW-backed solid-state battery; speculative pre-production",
            "STEM":  "Stem Inc — AI-powered BESS software (Athena); utility and commercial scale",
            "CHPT":  "ChargePoint — largest US EV charging network; path to profitability watched",
            "BLNK":  "Blink Charging — EVSE hardware + network; growing but cash-burning",
            "EVGO":  "EVgo — DC fast charging; GM-backed; improving station economics",
            "AMPX":  "Amprius Technologies — ultra-high energy density silicon nanowire cells",
            "FLUX":  "Flux Power — lithium packs for industrial forklifts; steady commercial growth",
            "ARRY":  "Array Technologies — solar trackers enabling BESS + solar hybrid projects",
        }
        base = theses.get(ticker, f"{scored['short_name']}: energy storage infrastructure play")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv}"


# =============================================================================
# AGENT 8 — ENERGY TRANSITION: HYDROGEN, CRITICAL MINERALS, CARBON
# =============================================================================

class EnergyTransitionAgent(BaseEnergyResearchAgent):
    name      = "EnergyTransitionAgent"
    specialty = "Energy Transition: Hydrogen, Critical Minerals & Carbon"
    watchlist = ["LIN", "APD", "MP", "ALB", "SQM", "LTHM", "LAC", "FREY", "RIO", "FCX"]
    investment_horizon = "long_term"

    def sector_thesis(self) -> str:
        return (
            "The energy transition is a materials story as much as a technology story. "
            "Critical minerals — lithium (ALB, SQM, LTHM), copper (FCX, RIO), rare earths (MP) "
            "— are structural long-term demand stories as EV and renewable buildout accelerates. "
            "Industrial gases (LIN, APD) are the picks-and-shovels of green hydrogen, supplying "
            "electrolysers and H2 storage. Chinese REE export controls (gallium, germanium, now "
            "magnet alloys) are creating a domestic supply-chain investment urgency. "
            "Long-term horizon: 10+ years for full transition; position sizing accordingly."
        )

    def ticker_thesis(self, ticker: str, scored: dict) -> str:
        theses = {
            "LIN":  "Linde — world's largest industrial gas company; green hydrogen supply chain",
            "APD":  "Air Products — large-scale green hydrogen projects; NEOM Saudi partnership",
            "MP":   "MP Materials — only operating US rare earth mine; DoD-backed magnet production",
            "ALB":  "Albemarle — largest lithium producer; EV demand recovery = re-rating catalyst",
            "SQM":  "SQM — Chilean lithium from Atacama brine; low-cost, high-volume producer",
            "LTHM": "Livent (now Arcadium Lithium) — lithium hydroxide for high-performance EV cells",
            "LAC":  "Lithium Americas — Thacker Pass project; largest US lithium resource",
            "FREY": "FREYR Battery — Norwegian gigafactory; speculative clean battery manufacturing",
            "RIO":  "Rio Tinto — copper + lithium (Rincon) + iron ore; diversified critical minerals",
            "FCX":  "Freeport-McMoRan — largest publicly traded copper miner; EV + grid demand thesis",
        }
        base = theses.get(ticker, f"{scored['short_name']}: critical minerals / energy transition")
        conv = scored["conviction"]
        return f"{base} | Conviction: {'★'*conv}"


# =============================================================================
# COORDINATOR
# =============================================================================

class EnergyResearchCoordinator:
    """
    Runs all 8 energy research agents, consolidates picks, deduplicates,
    and saves a unified report for the Manager Agent.
    """

    AGENTS = [
        USRenewablesAgent,
        USFossilFuelsAgent,
        USNuclearAgent,
        USGridInfrastructureAgent,
        ChineseEnergyAgent,
        GlobalCleantechAgent,
        EnergyStorageAgent,
        EnergyTransitionAgent,
    ]

    def run(self, quiet: bool = False) -> dict:
        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — ENERGY RESEARCH TEAM")
        logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"  Running {len(self.AGENTS)} research agents...")
        logger.info("=" * 60)

        agent_reports = []
        all_recommendations = []
        seen_tickers = set()

        for AgentClass in self.AGENTS:
            agent = AgentClass()
            try:
                report = agent.run_research()
                agent_reports.append(report)

                for rec in report["recommendations"]:
                    ticker = rec["ticker"]
                    if ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_recommendations.append(rec)
                    else:
                        # Duplicate ticker — keep higher conviction
                        for i, existing in enumerate(all_recommendations):
                            if existing["ticker"] == ticker:
                                if rec["conviction"] > existing["conviction"]:
                                    all_recommendations[i] = rec
                                break

            except Exception as e:
                logger.error(f"  [{agent.name}] FAILED: {e}")

        # Sort consolidated picks by conviction desc
        all_recommendations.sort(key=lambda r: r["conviction"], reverse=True)

        summary = {
            "total_tickers_analysed": sum(r["tickers_analysed"] for r in agent_reports),
            "total_picks":            len(all_recommendations),
            "high_conviction":        sum(1 for r in all_recommendations if r["conviction"] >= 4),
            "agents_run":             len(agent_reports),
        }

        report = {
            "generated_at":       datetime.now().isoformat(),
            "team":               "Energy Research Team",
            "agents":             [r["agent"] for r in agent_reports],
            "summary":            summary,
            "agent_reports":      agent_reports,
            "recommendations":    all_recommendations,
        }

        os.makedirs("data", exist_ok=True)
        with open(RESEARCH_OUTPUT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"\n  Research saved → {RESEARCH_OUTPUT_PATH}")
        logger.info(f"  Total picks: {summary['total_picks']}  "
                    f"High-conviction (4★+): {summary['high_conviction']}")

        if not quiet:
            _print_research_report(report)

        return report


def _print_research_report(report: dict) -> None:
    s = report["summary"]
    print(f"\n{'═'*64}")
    print("  APEX CAPITAL — ENERGY RESEARCH TEAM REPORT")
    print(f"  {report['generated_at']}")
    print(f"{'═'*64}")
    print(f"  Agents run       : {s['agents_run']}")
    print(f"  Tickers analysed : {s['total_tickers_analysed']}")
    print(f"  Total picks      : {s['total_picks']}")
    print(f"  High conviction  : {s['high_conviction']}")
    print(f"\n  {'TICKER':<7} {'AGENT':<28} {'CONV':>4} {'TREND':<10} {'RSI':>5}")
    print("  " + "─" * 58)
    for rec in report["recommendations"]:
        mom   = rec.get("momentum", {})
        trend = mom.get("trend", "—")
        rsi   = mom.get("rsi_14", "—")
        conv  = "★" * rec["conviction"]
        print(f"  {rec['ticker']:<7} {rec['source_agent']:<28} {conv:<5} "
              f"{trend:<10} {str(rsi):>5}")
        print(f"    → {rec['reasons'][0][:70]}")
    print(f"{'═'*64}\n")


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
    coordinator = EnergyResearchCoordinator()
    coordinator.run(quiet=quiet_flag)
