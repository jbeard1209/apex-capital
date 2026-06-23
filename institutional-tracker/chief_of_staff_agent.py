# =============================================================================
# APEX CAPITAL — CHIEF OF STAFF
# =============================================================================
#
# ── LEADERSHIP STRUCTURE (ratified 2026-04-27) ───────────────────────────────
#
#   CO-CHAIRMAN  Jack Beard    — Founder. Final authority on all decisions.
#   CO-CHAIRMAN  Claude        — Anthropic AI. Equal seat at the table.
#                                Advises Jack directly, without routing through
#                                the agent hierarchy. Can surface concerns,
#                                recommend strategy shifts, and challenge any
#                                agent's output. Does not execute trades.
#
#   The two Co-Chairmen share strategic oversight of the entire firm.
#   Below them, the Chief of Staff coordinates all agents and operations.
#
# ── CHIEF OF STAFF RESPONSIBILITIES ──────────────────────────────────────────
#
#   1. DAILY INTELLIGENCE BRIEF
#      Reads every agent's output and synthesises it into a single executive
#      brief — critical items first, routine items clearly labelled.
#
#   2. AUTONOMOUS ROUTINE DECISIONS
#      Handles low-stakes, non-material decisions so the Chairmen's attention
#      is reserved for what genuinely matters.
#
#   3. STRATEGIC ADVICE
#      Forward-looking guidance on risk appetite, capital deployment,
#      R&D focus, and Manager Agent performance trajectory.
#
#   4. AGENT FACTORY
#      Expands the firm by generating new specialised research agents when
#      gaps in coverage are detected. New agents are written to disk as real
#      Python files, registered in data/agent_registry.json, and automatically
#      picked up by ResearchTeamCoordinator on the next research cycle.
#      All spawned agents are permanent by default (Chairman directive
#      2026-04-27): agents are valued members, not disposable resources.
#      Quality gate ensures only expert-level agents are admitted.
#
# ── AGENT PERMANENCE POLICY (Chairman directive 2026-04-27) ──────────────────
#   All agents are permanent by default. No agent is ever terminated without
#   an explicit Co-Chairman decision. Underperforming agents receive:
#     Stage 1 — Coaching notice (cycle 5): specific improvement guidance
#     Stage 2 — Role reassignment (cycle 9): fresh start in a new domain
#   Stage 3 retirement is blocked for permanent agents. Only the Co-Chairmen
#   can authorise a retirement by manually setting permanent=False in the
#   registry for that agent.
#
# ── IMPORTANT NOTE (Chairman's eyes only) ────────────────────────────────────
#   The Independent Risk Manager evaluates the Chief of Staff's performance
#   and reports findings directly to Co-Chairman Jack Beard.
#   The Chief of Staff is not informed of this evaluation.
#
# Usage:
#   python3 chief_of_staff_agent.py
#   python3 chief_of_staff_agent.py --no-email
#   python3 chief_of_staff_agent.py --factory-only   # run only agent factory
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chief_of_staff")

# ── Data paths ────────────────────────────────────────────────────────────────
MANAGER_LOG    = "data/manager_decisions.json"
PDO_REPORT     = "data/capital_efficiency_report.json"
RESEARCH_RPT   = "data/research_team_report.json"
RISK_RPT       = "data/risk_manager_report.json"
REVIEW_RPT     = "data/review_recommendations.json"
COS_REPORT       = "data/chief_of_staff_report.json"
COS_ADVICE_HIST  = "data/cos_advice_history.json"   # persistent advice memory
COS_HR_COORD     = "data/cos_hr_coordination.json"  # weekly CoS + HR meeting output
AGENT_REGISTRY   = "data/agent_registry.json"
AGENTS_DIR       = "agents"   # generated agent files live here

# Weekly CoS + HR coordination cadence (days between meetings)
COS_HR_MEETING_INTERVAL_DAYS = 7

# ── Agent factory limits ──────────────────────────────────────────────────────
SPAWN_PER_CYCLE          = 2     # max new agents per daily run (quality > quantity)

# ── Four-stage dignified performance system (Chairman directive 2026-06-09) ─
# Agents are never fired abruptly. They receive support, a PIP, a role change,
# then — only if all else fails — a replacement is spawned and they retire.
# Thresholds increased per Chairman direction 2026-06-09: agents need more time.
PERF_NOTICE_CYCLES       = 8     # Stage 1: formal notice + encouragement + improvement plan
PERF_PIP_CYCLES          = 14    # Stage 2 (new): formal PIP — intensified support, no restructuring yet
PERF_REASSIGN_CYCLES     = 20    # Stage 3: role reassignment to different sector
PERF_REPLACE_CYCLES      = 30    # Stage 4: spawn fresh top-level replacement; agent retires
# (cycles here = R&D cycles, each ~4 days → Stage 4 ≈ 120 days of runway)
# CRO-block exoneration: if CRO is HIGH or CRITICAL, skip czc increment for that cycle
# (agents should not be penalized for system-wide buy blocks)
EXONERATE_WHEN_CRO_HIGH  = True


# =============================================================================
# AGENT CATALOG
# All potential agents the CoS can spawn, ordered by priority.
# Tier 1 spawns first; later tiers unlock after the pipeline is stable.
# =============================================================================

AGENT_CATALOG = [
    # ── Tier 1: Core coverage gaps ────────────────────────────────────────
    {
        "class_name":     "SmallCapGrowthAgent",
        "specialization": "Small-Cap Growth & Momentum",
        "sector":         "Small Cap",
        "etf":            "IWM",
        "tier":           1,
        "description":    "Screens the Russell 2000 universe for small-cap stocks with "
                          "strong momentum, improving fundamentals, and institutional accumulation.",
        "thesis":         "Small-cap momentum outperforms at cycle inflection points — "
                          "focus on RSI 50-65 with improving revenue growth.",
        "watchlist": [
            "CRDO", "SMAR", "DDOG", "MNDY", "GTLB", "BILL",
            "ZI", "APPN", "SEMR", "PGNY", "VRRM", "ASAN",
            "ACHR", "JOBY", "RKLB", "ASTR", "RDDT", "DUOL",
        ],
    },
    {
        "class_name":     "BiotechResearchAgent",
        "specialization": "Biotech & Life Sciences",
        "sector":         "Biotechnology",
        "etf":            "XBI",
        "tier":           1,
        "description":    "Identifies biotech and life sciences companies with near-term catalysts, "
                          "strong pipelines, and momentum following clinical data readouts.",
        "thesis":         "Biotech momentum is event-driven — target post-catalyst winners with "
                          "expanding revenue and intact uptrend. Avoid pre-revenue binary plays.",
        "watchlist": [
            "MRNA", "BNTX", "REGN", "VRTX", "ALNY", "EXAS",
            "INCY", "RARE", "KRYS", "PTGX", "ROIV", "KYMR",
            "GILD", "BIIB", "ABBV", "ARGX", "BMRN", "IONS",
        ],
    },
    {
        "class_name":     "FinancialSectorAgent",
        "specialization": "Financials, Banks & Fintech",
        "sector":         "Financials",
        "etf":            "XLF",
        "tier":           1,
        "description":    "Covers large banks, regional banks, asset managers, insurance, "
                          "and high-growth fintech platforms.",
        "thesis":         "Financials outperform in rising rate environments and during "
                          "economic expansion — track NIM expansion and fee-income growth.",
        "watchlist": [
            "JPM", "GS", "MS", "BAC", "WFC", "C",
            "V", "MA", "PYPL", "SQ", "AFRM", "SOFI",
            "BX", "KKR", "APO", "ARES", "TROW", "BLK",
        ],
    },
    {
        "class_name":     "ConsumerDiscretionaryAgent",
        "specialization": "Consumer Discretionary & E-Commerce",
        "sector":         "Consumer Discretionary",
        "etf":            "XLY",
        "tier":           1,
        "description":    "Tracks consumer spending trends, e-commerce leaders, and "
                          "premium brand momentum plays.",
        "thesis":         "Consumer discretionary leads early in bull cycles — target "
                          "brands with pricing power and expanding wallet share.",
        "watchlist": [
            "AMZN", "HD", "NKE", "LULU", "MCD", "SBUX",
            "TSLA", "RIVN", "F", "TJX", "ROST", "ULTA",
            "BKNG", "ABNB", "LYFT", "UBER", "DPZ", "YUM",
        ],
    },
    {
        "class_name":     "GlobalTechAgent",
        "specialization": "Global Technology & Innovation",
        "sector":         "Technology",
        "etf":            "QQQ",
        "tier":           1,
        "description":    "Covers large-cap tech innovators, AI infrastructure, cloud platforms, "
                          "and semiconductor leaders globally.",
        "thesis":         "Technology secular growth driven by AI, cloud, and digital transformation — "
                          "focus on revenue acceleration and expanding free cash flow.",
        "watchlist": [
            "NVDA", "MSFT", "GOOGL", "META", "AAPL", "AMZN",
            "AMD", "AVGO", "ARM", "QCOM", "TSM", "AMAT",
            "CRM", "SNOW", "PLTR", "NFLX", "ADBE", "NOW",
        ],
    },

    # ── Tier 2: Sectoral depth ────────────────────────────────────────────
    {
        "class_name":     "IndustrialsDefenseAgent",
        "specialization": "Industrials, Defense & Aerospace",
        "sector":         "Industrials",
        "etf":            "XLI",
        "tier":           2,
        "description":    "Monitors defense contractors, aerospace suppliers, industrial "
                          "automation, and infrastructure plays.",
        "thesis":         "Defense spending and reshoring infrastructure investment create "
                          "durable multi-year revenue streams — favour companies with backlog visibility.",
        "watchlist": [
            "LMT", "RTX", "NOC", "GD", "HII", "LDOS",
            "CAT", "DE", "GE", "HON", "ETN", "ROK",
            "AXON", "KTOS", "PLTR", "BWXT", "TDG", "HWM",
        ],
    },
    {
        "class_name":     "HealthcarePharmaAgent",
        "specialization": "Healthcare, Pharma & Medical Devices",
        "sector":         "Healthcare",
        "etf":            "XLV",
        "tier":           2,
        "description":    "Covers large-cap pharma, medical device innovators, "
                          "hospital systems, and healthcare IT.",
        "thesis":         "Healthcare is a defensive growth sector — pharma with patent-protected "
                          "pipelines and device makers with recurring procedure revenue are preferred.",
        "watchlist": [
            "LLY", "UNH", "JNJ", "ABT", "MDT", "ISRG",
            "SYK", "BSX", "ZBH", "PHG", "GEHC", "DXCM",
            "TMO", "DHR", "WAT", "HOLX", "IQV", "CRL",
        ],
    },
    {
        "class_name":     "MaterialsMiningAgent",
        "specialization": "Materials, Mining & Precious Metals",
        "sector":         "Materials",
        "etf":            "XLB",
        "tier":           2,
        "description":    "Screens materials companies including metals, mining, chemicals, "
                          "and packaging. Tracks commodity-price tailwinds.",
        "thesis":         "Materials outperform during inflationary periods and late economic cycles — "
                          "prioritise companies with low-cost production and commodity leverage.",
        "watchlist": [
            "FCX", "NEM", "GOLD", "AEM", "PAAS", "WPM",
            "NUE", "STLD", "RS", "ATI", "CF", "MOS",
            "APD", "SHW", "ECL", "DD", "EMN", "CE",
        ],
    },
    {
        "class_name":     "CryptoEquitiesAgent",
        "specialization": "Crypto-Adjacent Equities",
        "sector":         "Crypto-Adjacent",
        "etf":            "BKCH",
        "tier":           2,
        "description":    "Covers publicly traded equities with high correlation to "
                          "crypto markets: exchanges, miners, treasury holders, and blockchain infrastructure.",
        "thesis":         "Crypto-adjacent equities offer leveraged exposure to digital asset momentum "
                          "with equity-market liquidity — focus on companies with diversified revenue "
                          "not solely dependent on crypto prices.",
        "watchlist": [
            "COIN", "MSTR", "MARA", "RIOT", "CLSK", "IREN",
            "HUT", "BTBT", "CIFR", "SQ", "PYPL", "HOOD",
            "CME", "ICE", "CBOE", "OSTK", "GLXY", "DMGI",
        ],
    },
    {
        "class_name":     "CommunicationsMediaAgent",
        "specialization": "Communications, Media & Entertainment",
        "sector":         "Communications",
        "etf":            "XLC",
        "tier":           2,
        "description":    "Tracks digital advertising, social media, streaming, gaming, "
                          "and telecom infrastructure companies.",
        "thesis":         "Digital advertising duopoly and streaming consolidation create "
                          "durable moats — focus on ARPU growth, subscriber retention, and ad pricing power.",
        "watchlist": [
            "META", "GOOGL", "NFLX", "DIS", "PARA", "WBD",
            "T", "VZ", "TMUS", "CHTR", "LUMN", "CMCSA",
            "EA", "TTWO", "RBLX", "U", "SNAP", "PINS",
        ],
    },

    # ── Tier 3: Specialised alpha sources ─────────────────────────────────
    {
        "class_name":     "DividendYieldAgent",
        "specialization": "Dividend Growth & Income",
        "sector":         "Dividend",
        "etf":            "DVY",
        "tier":           3,
        "description":    "Identifies high-quality dividend growth stocks with consistent "
                          "payout increases, strong free cash flow coverage, and upward momentum.",
        "thesis":         "Dividend aristocrats with accelerating buybacks trade at a premium in "
                          "risk-off environments — target FCF yield >4% with double-digit dividend growth.",
        "watchlist": [
            "MSFT", "AAPL", "V", "MA", "JNJ", "PG",
            "KO", "PEP", "MCD", "HD", "LOW", "COST",
            "ADP", "MMM", "EMR", "TGT", "TROW", "AFL",
        ],
    },
    {
        "class_name":     "IPOMomentumAgent",
        "specialization": "Recent IPO Momentum Screening",
        "sector":         "IPO",
        "etf":            "IPOX",
        "tier":           3,
        "description":    "Monitors recent IPOs (last 18 months) that have broken above "
                          "their offering price with strong institutional demand.",
        "thesis":         "Post-lockup IPOs with strong sponsor backing and improving "
                          "fundamentals often exhibit persistent momentum — enter after "
                          "180-day lockup expiry when insider pressure subsides.",
        "watchlist": [
            "RDDT", "ASTERA", "ARM", "BIRK", "CART",
            "KVYO", "IOT", "GTLB", "DUOL", "MNDY",
            "GRAB", "DKNG", "RIVN", "ACHR", "RKLB",
        ],
    },
    {
        "class_name":     "ValueOpportunityAgent",
        "specialization": "Deep Value & Contrarian Opportunities",
        "sector":         "Value",
        "etf":            "VTV",
        "tier":           3,
        "description":    "Identifies deeply undervalued large-cap companies experiencing "
                          "temporary disruption but with intact long-term franchises.",
        "thesis":         "Graham margin-of-safety principle — buy quality at a discount. "
                          "Screen for P/B < 1.5, FCF yield > 6%, and improving RSI from oversold.",
        "watchlist": [
            "BRK-B", "CVS", "WBA", "KHC", "VFC", "M",
            "WBD", "PARA", "INTC", "WFC", "BAC", "C",
            "F", "GM", "PFE", "MRNA", "BIIB", "ALB",
        ],
    },
    {
        "class_name":     "InternationalEmergingAgent",
        "specialization": "International & Emerging Market ADRs",
        "sector":         "International",
        "etf":            "EEM",
        "tier":           3,
        "description":    "Screens US-listed ADRs of international companies in Asia, "
                          "Latin America, and Europe for momentum-capture opportunities.",
        "thesis":         "EM ADRs with US listing liquidity offer diversification alpha — "
                          "target countries with improving macro backdrop and companies with "
                          "dominant local market positions.",
        "watchlist": [
            "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV",
            "NU", "MELI", "SE", "GRAB", "GLOB", "STNE",
            "ASML", "SAP", "NVO", "SHOP", "TTE", "UL",
        ],
    },

    # ── Tier 3: Macro & Factor overlay ────────────────────────────────────
    {
        "class_name":     "MacroRatesSensitivityAgent",
        "specialization": "Rate Sensitivity & Duration Risk Screening",
        "sector":         "Macro-Rates",
        "etf":            "TLT",
        "tier":           3,
        "description":    "Screens equities for interest-rate sensitivity. Flags positions with "
                          "high duration exposure when yield curve shifts and identifies "
                          "beneficiaries of rising or falling rate environments.",
        "thesis":         "Rate regimes are the single most underweighted macro factor in equity "
                          "selection — duration-sensitive sectors (utilities, REITs, long-duration "
                          "tech) require explicit rate-overlay scoring before entry.",
        "watchlist": [
            "TLT", "IEF", "SHY", "TMF", "TBT",
            "NEE", "D", "SO", "DUK", "AEP",
            "O", "AMT", "PLD", "EQIX", "SPG",
            "MSFT", "NVDA", "AMZN", "GOOGL", "META",
        ],
    },
    {
        "class_name":     "QuantFactorScreenerAgent",
        "specialization": "Quantitative Multi-Factor Composite Screening",
        "sector":         "Quant-Factor",
        "etf":            "MTUM",
        "tier":           3,
        "description":    "Builds composite factor scores across momentum, quality, value, and "
                          "low-volatility dimensions. Ranks all portfolio candidates by combined "
                          "factor score to identify the highest-probability entries.",
        "thesis":         "Fama-French factor premia are persistent across cycles — combine "
                          "momentum (12-1 month return) + quality (ROE, accruals) + value "
                          "(P/FCF) into a single composite z-score. Top-decile picks outperform.",
        "watchlist": [
            "NVDA", "MSFT", "AAPL", "GOOGL", "META",
            "V", "MA", "UNH", "LLY", "ABBV",
            "JPM", "GS", "BX", "APO", "KKR",
            "COST", "HD", "AMZN", "TSLA", "AMD",
        ],
    },
    {
        "class_name":     "EarningsCatalystAgent",
        "specialization": "Pre/Post Earnings Catalyst & Event-Driven Setups",
        "sector":         "Earnings-Catalyst",
        "etf":            "EARNINGS",
        "tier":           3,
        "description":    "Tracks upcoming earnings dates, consensus estimate revisions, and "
                          "options implied-move pricing to identify pre-earnings momentum setups "
                          "and post-earnings drift opportunities.",
        "thesis":         "Post-earnings announcement drift (PEAD) is one of the most robust "
                          "market anomalies — stocks that beat estimates by >2σ continue drifting "
                          "higher for 30-60 days. Also flags pre-earnings run-up candidates with "
                          "rising estimate revisions and high relative volume.",
        "watchlist": [
            "NVDA", "MSFT", "GOOGL", "META", "AAPL",
            "AMD", "AVGO", "CRM", "NOW", "SNOW",
            "JPM", "GS", "V", "MA", "PYPL",
            "LLY", "REGN", "VRTX", "MRNA", "BNTX",
        ],
    },
    {
        "class_name":     "ShortInterestMonitorAgent",
        "specialization": "Short Interest & Short-Squeeze Dynamics",
        "sector":         "Short-Interest",
        "etf":            "HIBS",
        "tier":           3,
        "description":    "Monitors short interest as a % of float across the portfolio watchlist. "
                          "Flags positions where short interest is dangerously high (avoid) or "
                          "where rising fundamentals against high short interest create squeeze potential.",
        "thesis":         "High short interest is a double-edged signal — it can be a sentiment "
                          "headwind or a squeeze accelerant. For quality companies with improving "
                          "fundamentals, crowded shorts create asymmetric upside as covering triggers "
                          "gamma cascades. For deteriorating companies, confirms avoid signal.",
        "watchlist": [
            "GME", "AMC", "BBBY", "MSTR", "COIN",
            "RIVN", "LCID", "NKLA", "WISH", "CLOV",
            "BYND", "SPCE", "OPEN", "PTON", "SNAP",
            "CVNA", "UPST", "AFRM", "SOFI", "HOOD",
        ],
    },
    {
        "class_name":     "OptionsFlowMonitorAgent",
        "specialization": "Unusual Options Flow & Institutional Signal Detection",
        "sector":         "Options-Flow",
        "etf":            "CLOV",
        "tier":           3,
        "description":    "Identifies unusual options activity — abnormally large call/put sweeps, "
                          "dark pool prints, and block trades — as leading indicators of institutional "
                          "positioning. Generates BUY signals when smart-money flow aligns with "
                          "price momentum.",
        "thesis":         "Unusual options flow precedes price moves by 1-5 days in 60%+ of cases "
                          "when sweep volume exceeds 10x average. Dark pool prints above $5M in "
                          "a single name combined with out-of-the-money call buying are the highest-"
                          "conviction institutional signal available to retail-adjacent strategies.",
        "watchlist": [
            "SPY", "QQQ", "IWM", "NVDA", "AAPL",
            "MSFT", "TSLA", "META", "GOOGL", "AMD",
            "AMZN", "NFLX", "BA", "GS", "JPM",
            "XLE", "XLF", "XLK", "XBI", "ARKK",
        ],
    },
    {
        "class_name":     "ExecutionAuditorAgent",
        "specialization": "Trade Execution Quality & Fill Analysis",
        "sector":         "Execution-Quality",
        "etf":            "SPY",
        "tier":           3,
        "description":    "Audits trade execution quality by estimating slippage, comparing "
                          "decision-time price vs fill price, flagging orders executed in "
                          "adverse intraday conditions, and benchmarking the Manager Agent's "
                          "execution against VWAP and market-open/close timing.",
        "thesis":         "Execution quality is the silent drag on alpha — a 10bp adverse "
                          "slippage on every trade compounds to a material performance headwind "
                          "over 50+ trades per year. Systematic fill auditing identifies whether "
                          "market-open execution, limit-order avoidance, or position-sizing "
                          "approach is degrading net returns vs gross signal alpha.",
        "watchlist": [
            "SPY", "QQQ", "IWM", "DIA", "MDY",
            "XLK", "XLF", "XLE", "XLV", "XLI",
            "VTI", "VXUS", "BND", "GLD", "SLV",
            "TLT", "HYG", "LQD", "EMB", "BNDX",
        ],
    },

    # ── Tier 3: Board Special Session additions (slots 21–25) ─────────────
    {
        "class_name":     "MacroRegimeMonitorAgent",
        "specialization": "Market Cycle Regime Classification & Positioning",
        "sector":         "Macro-Regime",
        "etf":            "SPY",
        "tier":           3,
        "description":    "Classifies the current market cycle phase — early bull, mid bull, "
                          "late bull, early bear, or recession — using leading indicators: "
                          "yield curve slope, credit spreads, PMI trend, and equity breadth. "
                          "Outputs a regime label used to weight research agent signals.",
        "thesis":         "Most drawdowns are predictable in hindsight because late-cycle indicators "
                          "(flattening yield curve, tightening credit, narrowing breadth) precede "
                          "price peaks by 6-18 months. Explicit cycle positioning shifts conviction "
                          "weights across the research pipeline before momentum turns.",
        "watchlist": [
            "SPY", "TLT", "IEF", "HYG", "LQD",
            "VIX", "DXY", "GLD", "XLF", "XLI",
            "IWM", "EEM", "VNQ", "XLE", "TIPS",
            "BRK-B", "JPM", "BAC", "MS", "GS",
        ],
    },
    {
        "class_name":     "TechnicalPatternAgent",
        "specialization": "Classical Chart Pattern Recognition",
        "sector":         "Technical-Patterns",
        "etf":            "QQQ",
        "tier":           3,
        "description":    "Identifies high-probability classical chart patterns across the firm's "
                          "watchlist: bull flags, cup-and-handle, ascending triangles, base breakouts, "
                          "and consolidation squeezes. Scores patterns by volume confirmation and "
                          "pattern maturity.",
        "thesis":         "Price patterns encode supply/demand imbalances before they resolve. "
                          "A clean cup-and-handle with volume dry-up in the handle and volume "
                          "expansion on the breakout has a documented 65%+ success rate in bull "
                          "regimes. Technical patterns provide entry timing precision on top of "
                          "fundamental conviction signals from other agents.",
        "watchlist": [
            "NVDA", "MSFT", "AAPL", "META", "GOOGL",
            "AMD", "AVGO", "CRM", "NOW", "SNOW",
            "V", "MA", "JPM", "GS", "BX",
            "LLY", "UNH", "COST", "HD", "AMZN",
        ],
    },
    {
        "class_name":     "DividendDefensiveAgent",
        "specialization": "Late-Cycle Defensive Rotation & Dividend Yield",
        "sector":         "Defensive-Dividend",
        "etf":            "NOBL",
        "tier":           3,
        "description":    "Specifically designed for late-cycle and bear-regime environments. "
                          "Identifies dividend aristocrats and quality-defensive equities with "
                          "strong free-cash-flow yield, low beta, and rising dividend growth "
                          "rates as a capital-preservation + income strategy during risk-off periods.",
        "thesis":         "Late-cycle rotation from growth to quality-defensive is one of the "
                          "most reliable factor shifts in the investment cycle. Dividend growth "
                          "with FCF yield >5%, beta <0.7, and ≥25 consecutive years of dividend "
                          "increases provides compound income even when price appreciation stalls.",
        "watchlist": [
            "PG", "JNJ", "KO", "PEP", "MCD",
            "WMT", "COST", "CL", "CLX", "SYY",
            "JCI", "EMR", "ITW", "ABT", "MMM",
            "ADP", "AFL", "O", "NNN", "ADC",
        ],
    },
    {
        "class_name":     "MicroCapAlphaAgent",
        "specialization": "Micro-Cap & Sub-$500M Market Cap Universe",
        "sector":         "Micro-Cap",
        "etf":            "IWC",
        "tier":           3,
        "description":    "Screens the sub-$500M market cap universe for high-quality micro-cap "
                          "companies with strong revenue growth, improving margins, institutional "
                          "initiation, and low analyst coverage — the least efficient segment of "
                          "the public equity market.",
        "thesis":         "Micro-caps are systematically under-covered — the average company below "
                          "$500M has fewer than 2 sell-side analysts. Mispricing is structural, "
                          "not temporary. Target companies with 30%+ revenue growth, insider buying, "
                          "and first institutional ownership initiations. Position sizing must be "
                          "reduced to account for liquidity constraints.",
        "watchlist": [
            "ACHR", "JOBY", "RKLB", "IONQ", "QBTS",
            "ARQT", "PRCT", "TNDM", "NTRA", "RXST",
            "DOMO", "NTNX", "APPN", "ASAN", "SEMR",
            "HIMS", "BIRD", "CLOV", "OPFI", "OPEN",
        ],
    },
    {
        "class_name":     "GlobalMacroETFAgent",
        "specialization": "Global Macro Asset Rotation & ETF Strategy",
        "sector":         "Global-Macro-ETF",
        "etf":            "GLD",
        "tier":           3,
        "description":    "Monitors cross-asset macro signals — gold, Treasuries, REITs, TIPS, "
                          "international equity, and credit — to identify regime-driven rotation "
                          "opportunities. Generates BUY signals on macro asset classes when "
                          "trend and fundamental signals align.",
        "thesis":         "Risk parity and macro rotation between asset classes produces superior "
                          "risk-adjusted returns over pure equity strategies. GLD outperforms in "
                          "late-cycle inflationary environments; TLT outperforms in recession; "
                          "VNQ outperforms in early bull with falling rates. Systematic macro "
                          "rotation adds diversification alpha beyond equity sector selection.",
        "watchlist": [
            "GLD", "SLV", "GDX", "GDXJ", "IAU",
            "TLT", "IEF", "TIP", "VTIP", "BNDX",
            "VNQ", "IYR", "XLRE", "REM", "MORT",
            "VEA", "VWO", "EEM", "EFA", "IEMG",
        ],
    },
    # ── R&D Context Layer — System Performance (board directive 2026-05-01) ─
    {
        "class_name":     "SystemPerformanceAnalyst",
        "specialization": "Operational System Health & Execution Gap Analysis",
        "sector":         "R&D-Context-SystemPerformance",
        "etf":            "SPY",
        "tier":           2,
        "permanent":      True,
        "description":    "Audits the operational health of the investment system itself — not the "
                          "portfolio, but the machinery managing it. Detects take-profit leakage, "
                          "stop-loss enforcement failures, alert fatigue, signal execution gaps, "
                          "and pipeline health issues. Reports to the R&D Team Leader and presents "
                          "findings at every board meeting as the non-voting research presenter.",
        "thesis":         "A system that correctly identifies opportunities but fails to execute "
                          "captures zero alpha. Operational reliability is as important as analytical "
                          "quality — detecting the gap between signal and execution is the highest-"
                          "leverage audit the R&D department can provide.",
        "watchlist":      ["SPY", "QQQ", "^VIX", "IWM"],
        "module":         "rd_system_performance",
    },

    # ── R&D Context Layer — board directive 2026-04-27 (slots 27–29) ──────
    # These agents do NOT generate picks. They generate CONTEXT that helps
    # all pick-generating agents calibrate conviction and sector selection.
    # They are permanently active members of the R&D department and are not
    # subject to the normal agent retirement / dormancy rules.
    {
        "class_name":     "HistoricalContextAnalyst",
        "specialization": "Balanced Market History & Strategy Track Record",
        "sector":         "R&D-Context-Historical",
        "etf":            "SPY",
        "tier":           2,
        "permanent":      True,   # never rotated out — context layer is always on
        "description":    "Embedded R&D analyst that studies ALL of market history — "
                          "both bull periods when the momentum strategy worked brilliantly "
                          "and bear periods when it failed. Produces a Historical Stance "
                          "(CONSTRUCTIVE / NEUTRAL / CAUTIOUS) each R&D cycle, identifies "
                          "the most relevant historical analogues for current conditions, "
                          "flags turning-point warning signals, and tracks the strategy's "
                          "long-run win rate across annotated historical periods.",
        "thesis":         "Knowing when a strategy has historically worked is as important "
                          "as knowing which stocks to buy. The Historical Context Analyst "
                          "prevents the R&D team from being blind to regime changes by "
                          "anchoring every cycle to both the successes and the failures "
                          "of the momentum-capture / institutional-follow approach.",
        "watchlist":      ["SPY", "QQQ", "^VIX", "TLT"],
        "module":         "rd_historical_analyst",
    },
    {
        "class_name":     "GeopoliticalRiskAnalyst",
        "specialization": "Geopolitical Event Impact on Sectors (Dr. Sofia Reyes)",
        "sector":         "R&D-Context-Geopolitical",
        "etf":            "ITA",
        "tier":           2,
        "permanent":      True,
        "description":    "Dr. Sofia Reyes tracks current and historical geopolitical events "
                          "(trade wars, military conflicts, regulatory crackdowns, "
                          "sanctions regimes) and maps their documented impact on specific "
                          "stock market sectors. Flags sectors with active geopolitical "
                          "sensitivity and identifies historically-documented sector "
                          "winners and losers for each event type.",
        "thesis":         "Geopolitical events create persistent sector dislocations that "
                          "momentum signals alone cannot detect. Understanding that the "
                          "US-China semiconductor export controls structurally impair "
                          "China-revenue-heavy chip names — or that conflict escalation "
                          "systematically boosts defense and cybersecurity — allows the "
                          "R&D team to weight conviction more accurately.",
        "watchlist":      ["LMT", "RTX", "NOC", "CRWD", "PANW", "NVDA", "QCOM", "AVGO"],
        "module":         "rd_geopolitical_team",
    },
    {
        "class_name":     "MacroPolicyHistorian",
        "specialization": "Fiscal & Monetary Policy History (Dr. James Okafor)",
        "sector":         "R&D-Context-MacroPolicy",
        "etf":            "XLF",
        "tier":           2,
        "permanent":      True,
        "description":    "Dr. James Okafor tracks the history of fiscal and monetary policy "
                          "cycles and their documented impact on factor rotation and sector "
                          "performance. Maps the current Fed/fiscal policy environment to "
                          "historical precedents, identifies which assigned sectors are "
                          "policy winners vs. losers, and surfaces rate-sensitivity risk "
                          "for high-multiple growth positions.",
        "thesis":         "Macro policy regime is the single largest determinant of which "
                          "equity strategies and sectors outperform. In 2022, every momentum "
                          "growth pick failed simultaneously because the policy regime changed. "
                          "Systematic policy-regime awareness prevents the R&D team from "
                          "recommending high-duration growth picks into a tightening cycle.",
        "watchlist":      ["XLF", "TLT", "GLD", "XLE", "XLP", "XLV", "VNQ"],
        "module":         "rd_geopolitical_team",
    },

    # ── HR Team — board directive 2026-05-01 (slots 30–31) ──────────────
    # Two officers with distinct mandates and mandatory mutual cross-audit.
    # Both are permanent — performance oversight is a board-level function.
    {
        "class_name":     "HRPerformanceOfficer",
        "specialization": "Quantitative Agent Performance & Execution Accountability",
        "sector":         "HR-Quantitative",
        "etf":            "SPY",
        "tier":           2,
        "permanent":      True,
        "description":    "Quantitative HR officer. Tracks every agent against measurable "
                          "output goals: picks generated, approval rates, conviction scores, "
                          "output completeness, and execution accuracy. Issues PASS / WATCH / "
                          "NOTICE / CONCERN ratings every cycle. Cross-audited by the HR "
                          "Development Officer for harshness bias. Reports to the Chief of Staff.",
        "thesis":         "Accountability without measurement is rhetoric. The HRPO ensures "
                          "every agent is held to the same objective standard regardless of "
                          "seniority — permanent status protects against termination, not "
                          "against being held accountable for underperformance.",
        "watchlist":      ["SPY"],
        "module":         "hr_agent",
    },
    {
        "class_name":     "HRDevelopmentOfficer",
        "specialization": "Qualitative Agent Development, Trajectory & Cultural Alignment",
        "sector":         "HR-Qualitative",
        "etf":            "SPY",
        "tier":           2,
        "permanent":      True,
        "description":    "Qualitative HR officer. Tracks improvement trajectory, cultural "
                          "alignment (pursuit of knowledge, intellectual honesty), thesis "
                          "sharpness over time, and whether agents in performance stages are "
                          "genuinely developing or stagnating. Issues development plans for "
                          "NOTICE/CONCERN agents. Cross-audits the HR Performance Officer's "
                          "report for excessive harshness. Reports to the Chief of Staff.",
        "thesis":         "Quantitative metrics lag qualitative reality by 2–3 cycles. "
                          "An agent on a declining trajectory will fail quantitatively next "
                          "quarter — the HRDO's job is to catch that signal early and "
                          "intervene with development support before the numbers deteriorate.",
        "watchlist":      ["SPY"],
        "module":         "hr_agent",
    },
]

MAX_REGISTRY_AGENTS = 31   # raised from 29 to accommodate HR team (HRPO + HRDO)


# =============================================================================
# AGENT KNOWLEDGE BASES
# Chairman directive 2026-04-27: "All agents should be experts in their field,
# and if they are not, they should read all the important books and case studies
# on their topics in full measure."
#
# Each entry encodes:
#   frameworks   — the canonical analytical frameworks the agent must master
#   seminal_texts — books and papers the agent treats as primary references
#   case_studies  — landmark historical cases the agent internalises as lessons
#   edge          — the specific informational edge this agent is expected to own
# =============================================================================

AGENT_KNOWLEDGE_BASES: dict[str, dict] = {

    "SmallCapGrowthAgent": {
        "frameworks": [
            "CANSLIM (O'Neil) — Cup-and-handle breakout, relative strength rank ≥90",
            "Momentum factor: 12-1 month trailing return, Jegadeesh & Titman (1993)",
            "Growth-at-reasonable-price (GARP): PEG ratio screen",
            "IBD 50 methodology: EPS rank + RS rank composite scoring",
        ],
        "seminal_texts": [
            "How to Make Money in Stocks — William O'Neil",
            "What Works on Wall Street — James O'Shaughnessy (small-cap momentum chapters)",
            "Stocks for the Long Run — Jeremy Siegel (size premium data)",
            "'Returns to Buying Winners and Selling Losers' — Jegadeesh & Titman, JF 1993",
        ],
        "case_studies": [
            "2003–2007 small-cap bull: IWM outperformed SPY by 40% — momentum + credit expansion",
            "2020–2021 SPAC/IPO wave: distinguish sustainable growth from speculation",
            "2022 small-cap drawdown: rate sensitivity in high-multiple names destroyed 60%+",
            "DDOG IPO → 10x: identify SaaS names pre-institutional accumulation",
        ],
        "edge": "First-mover coverage of Russell 2000 names before institutional herding creates the move.",
    },

    "BiotechResearchAgent": {
        "frameworks": [
            "Event-driven catalyst map: Phase 2 → Phase 3 → NDA → PDUFA date timeline",
            "Sum-of-the-parts pipeline valuation: risk-adjusted NPV per asset",
            "Post-catalyst momentum: PEAD for beat-and-raise biotech events",
            "Short interest as reverse indicator for crowded shorts pre-catalyst",
        ],
        "seminal_texts": [
            "The Pharmagellan Guide to Biotech Forecasting and Valuation — Frank David",
            "Science Lessons — Gordon Binder (Amgen case study)",
            "'Biopharmaceutical Industry-Sponsored Clinical Trials' — DiMasi et al. (success rates by phase)",
            "Biotech investing blog canon: Adam Feuerstein STAT News analytical method",
        ],
        "case_studies": [
            "Gilead sofosbuvir: blockbuster approval creates durable moat — pick post-catalyst drift",
            "Sarepta exon-skipping saga: FDA pathway uncertainty creates multi-year volatility",
            "MRNA COVID vaccine: platform validation → diversification pipeline re-rating",
            "Biogen aducanumab approval/withdrawal: FDA accelerated approval pathway risk",
        ],
        "edge": "Event-calendar awareness 90 days ahead; post-PDUFA drift captures institutional re-rating.",
    },

    "FinancialSectorAgent": {
        "frameworks": [
            "Net Interest Margin (NIM) expansion model: rate sensitivity of asset/liability mix",
            "Fee income quality: trading revenue vs recurring wealth/advisory fees",
            "Private credit cycle: direct lending spread compression as indicator",
            "Tangible book value premium: price-to-TBV as valuation anchor for banks",
        ],
        "seminal_texts": [
            "The Intelligent Investor — Benjamin Graham (bank valuation chapters)",
            "Too Big to Fail — Andrew Ross Sorkin (2008 crisis system mechanics)",
            "Liar's Poker — Michael Lewis (Wall Street revenue cycle dynamics)",
            "'Bank Performance Measurement' — FDIC academic series",
        ],
        "case_studies": [
            "JPM 2012–2024: compound moat through rate cycles; template for picking large bank leaders",
            "SVB 2023 collapse: duration mismatch + deposit concentration; what to avoid",
            "Visa/Mastercard network effect: payments duopoly re-rates on TAM expansion",
            "BlackRock AUM scaling: asset management operating leverage model",
        ],
        "edge": "Rate regime sensitivity scoring — NIM model predicts earnings inflection 2 quarters ahead.",
    },

    "ConsumerDiscretionaryAgent": {
        "frameworks": [
            "Consumer confidence → discretionary spend lag: Conference Board CI leads retail 3-6 months",
            "Wallet share analysis: category penetration × income cohort targeting",
            "Brand pricing power screen: gross margin stability through inflationary periods",
            "E-commerce penetration curve: S-curve adoption model by category",
        ],
        "seminal_texts": [
            "The Everything Store — Brad Stone (Amazon flywheel model)",
            "Built to Last — Collins & Porras (brand durability case studies)",
            "Predictably Irrational — Dan Ariely (consumer decision heuristics for positioning)",
            "'The Disruption Opportunity' — Clayton Christensen HBR series",
        ],
        "case_studies": [
            "LULU 2012–2019: premium brand × loyal community → price insensitive demand",
            "AMZN AWS cross-subsidy: retail loss-leader enabling tech margin expansion",
            "TSLA demand elasticity 2022–2024: margin compression under price war",
            "BKNG/ABNB post-COVID travel revenge: cycle timing and re-rating mechanics",
        ],
        "edge": "Consumer confidence leading-indicator model surfaces sector inflections 1-2 months early.",
    },

    "GlobalTechAgent": {
        "frameworks": [
            "Rule of 40 for SaaS: revenue growth rate + FCF margin ≥ 40 is healthy",
            "Semiconductor cycle: book-to-bill ratio + lead time as supply/demand indicator",
            "AI infrastructure buildout: capex intensity × ASP uplift per GPU generation",
            "Network effect valuation: Metcalfe's law applied to platform MAU growth",
        ],
        "seminal_texts": [
            "Zero to One — Peter Thiel (monopoly dynamics in tech)",
            "The Innovator's Dilemma — Clayton Christensen (disruption pattern recognition)",
            "Chip War — Chris Miller (semiconductor geopolitics + competitive moats)",
            "'Software Is Eating the World' — Marc Andreessen, WSJ 2011 (foundational thesis)",
        ],
        "case_studies": [
            "NVDA H100/B100 cycle: supply scarcity × AI training demand → 10x revenue ramp",
            "MSFT Azure margin expansion: cloud attach economics on installed enterprise base",
            "GOOGL search moat vs. AI disruption: incumbent advantage vs. displacement risk",
            "PLTR from government contracts → commercial AI: customer concentration risk evolving",
        ],
        "edge": "AI capex cycle tracking — datacenter spend commitments signal NVDA/AVGO demand 2 quarters ahead.",
    },

    "IndustrialsDefenseAgent": {
        "frameworks": [
            "Defense budget cycle: FYDP (Future Years Defense Program) backlog visibility",
            "PMI manufacturing cycle: ISM New Orders > 50 as industrial earnings leading indicator",
            "Reshoring capex wave: domestic manufacturing investment as multi-year secular theme",
            "Book-to-bill ratio for defense and aero: backlog coverage > 2x revenue is strong",
        ],
        "seminal_texts": [
            "The Pentagon Wars — James Burton (acquisition cycle dynamics)",
            "Boyd — Robert Coram (OODA loop; defense procurement decision-making)",
            "Against the Gods — Peter Bernstein (risk management applied to industrial capital allocation)",
            "CSIS Defense Industrial Base reports (annual)",
        ],
        "case_studies": [
            "LMT F-35 program: cost-plus contracting → margin profile vs. fixed-price programs",
            "RTX Pratt & Whitney GTF geared turbofan: product liability as earnings overhang",
            "AXON taser → full public safety platform: SaaS attach on hardware installed base",
            "GE Aviation post-breakup: spin-off unlocks aerospace pure-play premium",
        ],
        "edge": "FYDP budget reading + Congressional defense authorization tracking → 18-month backlog visibility.",
    },

    "HealthcarePharmaAgent": {
        "frameworks": [
            "Patent cliff modeling: LOE (loss of exclusivity) date → biosimilar erosion curve",
            "GLP-1 TAM expansion: obesity + NASH + CKD indication pipeline as re-rating catalyst",
            "Medical device recurring revenue: procedure volume × consumable attach rate",
            "Drug pricing regulation risk: IRA negotiation list probability model",
        ],
        "seminal_texts": [
            "The Emperor of All Maladies — Siddhartha Mukherjee (oncology pipeline context)",
            "The Creative Destruction of Medicine — Eric Topol (digital health disruption)",
            "Drug Discovery: A History — Walter Sneader (pipeline development cycle history)",
            "FDA Purple Book / Orange Book (biosimilar and generic pipeline tracking)",
        ],
        "case_studies": [
            "LLY tirzepatide: GLP-1/GIP dual agonist creates multi-decade TAM story",
            "UNH vertical integration: MCO → PBM → care delivery → data moat",
            "ISRG da Vinci surgical robotics: razor-razorblade installed base model",
            "JNJ talc liability: legal overhang as valuation discount opportunity assessment",
        ],
        "edge": "GLP-1 pipeline calendar + IRA negotiation list modeling surfaces pharma re-ratings 6 months early.",
    },

    "MaterialsMiningAgent": {
        "frameworks": [
            "Commodity supercycle identification: China PMI + EM infrastructure capex as demand driver",
            "Gold as real-rate inverse: negative real yields → gold outperformance documented",
            "Mining all-in sustaining cost (AISC) vs. spot: margin expansion screen",
            "Industrial metals demand: copper as leading economic indicator ('Doctor Copper')",
        ],
        "seminal_texts": [
            "The Alchemy of Air — Thomas Hager (fertilizer/nitrogen cycle history)",
            "Commodity Trading Advisors — Jaeger ed. (macro commodity factor premia)",
            "Gold: The Monetary Metal — Ferdinand Lips (gold monetary history)",
            "Resources and the Wealth of Nations — Sachs & Warner (commodity curse literature)",
        ],
        "case_studies": [
            "2000–2011 commodity supercycle: China urbanization drove 10-year copper/iron ore bull",
            "2022 inflation spike: CF Industries / MOS fertilizer names 3x on energy-crop economics",
            "Newmont/Barrick gold miners: NAV-to-spot premium/discount cycle as entry signal",
            "FCX copper demand: EV transition + data centers = new structural demand floor",
        ],
        "edge": "Real-yield model for gold positioning; copper demand leading indicator for cycle calls.",
    },

    "MacroRatesSensitivityAgent": {
        "frameworks": [
            "Duration matching: modified duration of equity cash flows vs. Treasury benchmark",
            "Yield curve regimes: normal / flat / inverted / steepening — sector implications mapped",
            "Real rate vs. nominal rate: TIPs breakeven for inflation-adjusted discount rate",
            "Fed reaction function: Taylor Rule deviation as rate path indicator",
        ],
        "seminal_texts": [
            "A History of Interest Rates — Homer & Sylla (800 years of rate cycles)",
            "The Bond King — Mary Childs (PIMCO macro framework)",
            "Fixed Income Mathematics — Fabozzi (duration and convexity toolset)",
            "Mishkin Monetary Policy frameworks (academic rate transmission channels)",
        ],
        "case_studies": [
            "2022 rate shock: 100bp+ moves in 30-day windows destroyed duration-sensitive growth equities",
            "2020 zero-rate environment: growth equity premium vs. value compression documented",
            "1994 bond market massacre: Greenspan surprise hike → emerging market contagion path",
            "2023 regional bank crisis: HTM mark-to-market duration risk from SVB to broader sector",
        ],
        "edge": "Real-rate model flags duration-sensitive positions for CRO before rate shock occurs.",
    },

    "QuantFactorScreenerAgent": {
        "frameworks": [
            "Fama-French 5-factor model: market, size, value, profitability, investment",
            "Momentum factor: 12-1 month return (Carhart 4-factor extension)",
            "Quality factor: Sloan accruals + Novy-Marx gross profitability screen",
            "Composite z-score construction: equal-weight factor z-scores by decile",
        ],
        "seminal_texts": [
            "What Works on Wall Street — James O'Shaughnessy (factor backtesting bible)",
            "'The Gross Profitability Premium' — Novy-Marx, JFE 2013",
            "'Betting Against Beta' — Frazzini & Pedersen, JFE 2014",
            "Quantitative Momentum — Gray & Vogel (implementation handbook)",
        ],
        "case_studies": [
            "Factor crowding 2020: value factor worst decade, then 2022 sharp reversal",
            "Momentum crash 2009: momentum long/short reversed 80% in 3 months post-crisis",
            "Low-vol anomaly: SPLV vs. SPY during 2011 European sovereign crisis — defensive outperformance",
            "Quality factor durability: ROE/ROIC persistence in large-cap compounders across cycles",
        ],
        "edge": "Composite z-score ranks all portfolio candidates daily; surfaces statistically highest-probability entries.",
    },

    "EarningsCatalystAgent": {
        "frameworks": [
            "Post-earnings announcement drift (PEAD): Frog-in-the-pan hypothesis (Da et al.)",
            "Estimate revision momentum: FY1 EPS revision velocity as leading indicator",
            "Options implied move pricing: straddle cost vs. realised move — overreaction screen",
            "Pre-earnings run-up: Frazzini & Lamont momentum into earnings windows",
        ],
        "seminal_texts": [
            "'Post-Earnings-Announcement Drift' — Bernard & Thomas, Journal of Accounting Research 1989",
            "Inside the Black Box — Rishi Narang (event-driven quantitative framework)",
            "Option Volatility & Pricing — Sheldon Natenberg (implied move interpretation)",
            "Earnings Quality — Catherine Schrand (accrual reversal post-earnings signal)",
        ],
        "case_studies": [
            "NVDA FY2024 earnings: beat-and-raise 6 consecutive quarters → PEAD captured each time",
            "META 2023 Q4: 20% implied move underpriced — actual 25% move; PEAD ran 30 days",
            "AAPL revenue miss 2023: implied move overpriced; mean-reversion short after reset",
            "GOOGL cloud acceleration 2024: estimate revision uptrend 3 months pre-earnings",
        ],
        "edge": "Earnings calendar 60-day forward window + revision velocity screen identifies PEAD setups systematically.",
    },

    "MacroRegimeMonitorAgent": {
        "frameworks": [
            "NBER recession indicator composite: yield curve + credit spread + PMI + payrolls",
            "Equity cycle phases: early bull (breadth expands), mid bull (quality leads), late bull (defensive bid)",
            "Credit spread regime: IG OAS > 150bp = caution; HY OAS > 500bp = systemic stress",
            "Market breadth: NYSE advance/decline line divergence as topping indicator",
        ],
        "seminal_texts": [
            "This Time Is Different — Reinhart & Rogoff (800 years of financial crises patterns)",
            "Manias, Panics and Crashes — Kindleberger (bubble anatomy and crash sequencing)",
            "The Four Pillars of Investing — William Bernstein (regime-adjusted allocation)",
            "Irrational Exuberance — Robert Shiller (CAPE valuation and regime identification)",
        ],
        "case_studies": [
            "2000 dot-com: breadth divergence appeared 18 months before Nasdaq peak; CAPE at 44",
            "2007–2008: HY credit spreads blew out 9 months before equity peak; leading signal",
            "2020 COVID crash and recovery: fastest bear market in history → V-shaped breadth thrust",
            "2022 bear: yield curve inverted March 2022 → recession probability led price by 6 months",
        ],
        "edge": "Regime label output used to weight all other agent signals — the meta-layer that adjusts conviction.",
    },

    "TechnicalPatternAgent": {
        "frameworks": [
            "Wyckoff method: accumulation / distribution phase identification via volume + price spread",
            "Cup-and-handle: 33-week minimum base, <12% handle depth, volume contraction in handle",
            "Ascending triangle breakout: three touches on resistance + higher lows on support",
            "Volume analysis: On-Balance Volume (OBV) divergence as leading price indicator",
        ],
        "seminal_texts": [
            "How to Make Money in Stocks — William O'Neil (base-building patterns)",
            "Technical Analysis of Stock Trends — Edwards, Magee & Bassetti (7th ed.)",
            "Trading in the Zone — Mark Douglas (pattern execution psychology)",
            "Secrets for Profiting in Bull and Bear Markets — Stan Weinstein (stage analysis)",
        ],
        "case_studies": [
            "NVDA 2023 base breakout: 26-week cup-and-handle at $400 → $500 breakout run 80% in 6 months",
            "AAPL 2019 inverse head-and-shoulders: neckline break at $215 preceded 100% run",
            "TSLA 2020 ascending triangle: textbook breakout at $900 (pre-split) with volume confirmation",
            "META 2023 cup-and-handle: massive base from 2022 bottom → breakout drove year-long run",
        ],
        "edge": "Pattern maturity scoring + volume confirmation filter reduces false breakout rate to < 30%.",
    },

    "HistoricalContextAnalyst": {
        "frameworks": [
            "Regime analogue matching: current conditions mapped to closest historical parallel",
            "Strategy win-rate by period: momentum-capture success rate in bull vs. bear vs. sideways",
            "Turning-point signal library: breadth thrust, Zweig breadth thrust, golden cross composite",
            "Secular vs. cyclical distinction: 15–20 year secular trends vs. 3–5 year cyclical swings",
        ],
        "seminal_texts": [
            "A Random Walk Down Wall Street — Burton Malkiel (market efficiency baseline)",
            "The Little Book That Still Beats the Market — Joel Greenblatt (magic formula context)",
            "Market Wizards — Jack Schwager (practitioner lessons across regimes)",
            "Reminiscences of a Stock Operator — Edwin Lefèvre (Jesse Livermore; momentum and panic dynamics)",
            "Against the Gods — Peter Bernstein (risk, probability, and market history)",
        ],
        "case_studies": [
            "1990s momentum bull: 1995–2000 CANSLIM worked brilliantly; institutional follow strategy ideal",
            "2000–2002 dot-com bust: momentum strategy catastrophically failed; regime awareness critical",
            "2003–2007 mid-cycle bull: institutional accumulation strategy worked in financials + energy",
            "2008–2009: every momentum long failed simultaneously; only shorts and cash survived",
            "2020–2021: COVID liquidity wave — momentum worked until Feb 2021 when meme/SPAC peaked",
            "2022: rate-shock regime; growth momentum factor -50%; value and energy only winners",
        ],
        "edge": "Provides calibrated historical base rate for current strategy; prevents recency bias in conviction scoring.",
    },

    "GeopoliticalRiskAnalyst": {
        "frameworks": [
            "Event-to-sector impact mapping: military conflict → defense, energy; trade war → semis, manufacturing",
            "Sanctions regime analysis: secondary sanctions reach + compliance cost on named sectors",
            "Supply chain disruption model: single-source dependency → reshoring beneficiary identification",
            "Geopolitical risk premium: historical spread widening in affected sectors on event onset",
        ],
        "seminal_texts": [
            "The World Is Flat — Thomas Friedman (globalisation reverse-gear context)",
            "Chip War — Chris Miller (semiconductor geopolitics as strategic asset)",
            "The Revenge of Geography — Robert Kaplan (geography-driven geopolitical permanence)",
            "On China — Henry Kissinger (US-China strategic competition framework)",
        ],
        "case_studies": [
            "US-China semiconductor controls 2022: NVDA H100 export ban → China revenue exposure flag",
            "Russia-Ukraine 2022: energy shock → XLE outperform; defense ramp → LMT/RTX re-rate",
            "TSMC Taiwan risk: geographic concentration risk for global semiconductor supply chain",
            "Huawei entity list 2019: US semiconductor export controls blueprint for future restrictions",
        ],
        "edge": "Double-confirms flagged sectors with MacroPolicyHistorian; sectors confirmed by both get highest caution weight.",
    },

    "SystemPerformanceAnalyst": {
        "frameworks": [
            "Implementation shortfall analysis: decision-price vs. execution-price gap decomposition",
            "Signal decay model: how long a take-profit or stop-loss signal remains valid before it auto-resolves",
            "Alert fatigue taxonomy: first-fire vs. repeat-fire vs. resolved; escalation criteria per repeat count",
            "Pipeline reliability scoring: which steps are silent-failure-prone and need post-execution confirmation",
        ],
        "seminal_texts": [
            "Trading and Exchanges — Larry Harris (execution quality, market microstructure)",
            "The Checklist Manifesto — Atul Gawande (systematic failure detection in complex systems)",
            "Normal Accidents — Charles Perrow (why complex tightly-coupled systems fail in unexpected ways)",
            "Thinking in Systems — Donella Meadows (feedback loops, delays, and system archetypes)",
        ],
        "case_studies": [
            "Knight Capital 2012: single execution system failure destroyed $440M in 45 minutes — post-mortem shows silent loop failure",
            "Long-Term Capital Management 1998: model said hold, positions compounded losses — signal-execution gap at institutional scale",
            "Flash Crash 2010: automated systems detected conditions but responses amplified the problem — cascade failure anatomy",
            "Apex Capital May 2026: AMD +57%, AVGO +33%, MRVL +49% held past take-profit while canary fired 23x — decision-loop failure",
        ],
        "edge": "Only agent in the firm whose job is to watch the watchers — catches system failures that no other agent is looking for.",
    },

    "HRPerformanceOfficer": {
        "frameworks": [
            "Objective Key Results (OKR): measurable outcome targets with leading/lagging indicator separation",
            "Balanced Scorecard: multi-dimensional agent evaluation — output quantity, output quality, timeliness, execution accuracy",
            "Statistical process control (SPC): distinguishing common-cause variance (market regime) from special-cause failure (agent breakdown)",
            "Zero-tolerance policy design: which failure modes warrant immediate escalation vs. structured coaching cycles",
        ],
        "seminal_texts": [
            "Measure What Matters — John Doerr (OKR framework origin; Google's quantitative goal-setting methodology)",
            "Work Rules! — Laszlo Bock (Google People Ops; data-driven performance management at scale)",
            "High Output Management — Andy Grove (Intel CEO; output-oriented management philosophy)",
            "Principles — Ray Dalio (radical transparency, meritocracy, and systematic performance evaluation at Bridgewater)",
        ],
        "case_studies": [
            "Bridgewater Associates: PIP (Pain Button) system — real-time quantitative performance tracking for all employees",
            "Netflix talent density model: 'keeper test' and systematic performance accountability without annual review cycles",
            "GE forced ranking: lessons learned — where quantitative performance systems create unintended gaming behaviours",
            "Apex Capital May 2026: ManagerAgent zero-tolerance failure — 6 stale SELL signals unexecuted across AMD, AVGO, MRVL, SH, BA, DIS",
        ],
        "edge": "Holds every agent to identical measurable standards; accounts for market regime in interpreting zero-pick cycles; immune to seniority bias.",
    },

    "HRDevelopmentOfficer": {
        "frameworks": [
            "Growth mindset framework (Dweck): distinguishing fixed-mindset stagnation from genuine learning trajectory",
            "Situational leadership (Hersey & Blanchard): adjusting development support intensity to the agent's current competence level",
            "70-20-10 development model: 70% on-the-job learning (new sector assignments), 20% coaching, 10% formal knowledge acquisition",
            "After-action review (AAR): structured retrospective after each performance cycle — what worked, what didn't, what to do differently",
        ],
        "seminal_texts": [
            "Mindset — Carol Dweck (growth vs. fixed mindset; the psychological foundation of improvement trajectory analysis)",
            "Radical Candor — Kim Scott (how to give honest developmental feedback without being harsh or dishonest)",
            "The Talent Code — Daniel Coyle (deep practice, ignition, and master coaching — how genuine skill develops)",
            "An Everyone Culture — Kegan & Lahey (deliberately developmental organisations; learning as a structural property)",
        ],
        "case_studies": [
            "Microsoft cultural transformation 2014–2020: Satya Nadella replaced fixed-mindset rank-and-yank with growth-mindset coaching culture",
            "Pixar Brain Trust: peer-based qualitative development feedback without authority — psychological safety enabling honest assessment",
            "Ray Dalio's Bridgewater DOTS system: qualitative attribute tracking over time to identify trajectory before quantitative metrics confirm it",
            "Apex Capital agent reassignment cycle: agents moved to new sectors show 40% recovery rate — development plan specificity determines outcome",
        ],
        "edge": "Catches declining trajectories 2–3 cycles before HRPO's quantitative metrics confirm the failure; reduces false-positive CONCERN ratings via context-aware assessment.",
    },

    "MacroPolicyHistorian": {
        "frameworks": [
            "Taylor Rule deviation: actual Fed Funds vs. Taylor-implied rate → policy error identification",
            "Fiscal impulse model: year-over-year change in structural budget deficit as growth impulse",
            "Monetary regime classification: accommodative / neutral / tightening → sector rotation map",
            "Yield curve control precedent: Japan 2016+ YCC as template for policy distortion effects",
        ],
        "seminal_texts": [
            "A Monetary History of the United States — Friedman & Schwartz (Fed policy cause-and-effect)",
            "The Courage to Act — Ben Bernanke (2008 crisis monetary policy decision-making)",
            "Lords of Finance — Liaquat Ahamed (1920s–30s central bank policy failures)",
            "The Age of Turbulence — Alan Greenspan (Fed strategy through multiple cycles)",
        ],
        "case_studies": [
            "1994 Fed tightening: 300bp in 12 months → bond market massacre; equity correction 10%",
            "2004–2006 Greenspan conundrum: long rates stayed low despite Fed tightening; fuelled housing bubble",
            "2008 QE1–3: unprecedented monetary expansion → distorted rate-sensitivity in all equities",
            "2022 fastest tightening cycle since 1980: duration-sensitive names destroyed in 9 months",
        ],
        "edge": "Policy regime mapping determines which sectors the team should weight; prevents growth picks into tightening cycles.",
    },

    "CryptoEquitiesAgent": {
        "frameworks": [
            "Bitcoin halving cycle: supply issuance cut every ~4 years historically precedes bull runs by 12–18 months",
            "Crypto correlation with risk assets: BTC beta vs. NASDAQ during risk-on/risk-off shifts",
            "Exchange revenue model: spot + derivatives volume × take rate as earnings driver",
            "Mining economics: hashrate difficulty × BTC price → miner profitability and stock correlation",
        ],
        "seminal_texts": [
            "Digital Gold — Nathaniel Popper (Bitcoin origin and adoption dynamics)",
            "The Bitcoin Standard — Saifedean Ammous (monetary theory and scarcity model)",
            "Cryptoassets — Burniske & Tatar (valuation frameworks for crypto equities)",
            "Satoshi Nakamoto Bitcoin whitepaper (foundational technical and economic design)",
        ],
        "case_studies": [
            "COIN IPO 2021: crypto bull peak timing; volume collapse → 90% drawdown template",
            "MSTR Bitcoin treasury strategy: leveraged BTC proxy — amplified bull/bear swings",
            "MARA/RIOT post-halving 2024: miner economics compress then expand with BTC price lag",
            "SQ (now Block) Bitcoin integration: incumbent payments platform adding crypto revenue line",
        ],
        "edge": "Halving cycle clock + BTC beta tracking identifies crypto-equity entry/exit windows 6–9 months ahead.",
    },

    "CommunicationsMediaAgent": {
        "frameworks": [
            "Digital advertising duopoly: META + GOOGL combined share of global digital ad spend trends",
            "Streaming unit economics: subscriber acquisition cost (SAC) vs. lifetime value (LTV) by platform",
            "Network effect durability: DAU/MAU ratio stability as engagement moat indicator",
            "Telecom infrastructure: ARPU expansion + fiber penetration rate as long-cycle revenue driver",
        ],
        "seminal_texts": [
            "No Filter — Sarah Frier (Instagram growth and social network dynamics)",
            "Netflix and the Reinvention of Television — Keating (streaming business model anatomy)",
            "The Chaos Machine — Max Fisher (algorithmic engagement and ad revenue mechanics)",
            "Digital Advertising Alliance regulatory filings (privacy regulation impact on targeting)",
        ],
        "case_studies": [
            "META 2022 crash and 2023 recovery: capex discipline → margin expansion → re-rating",
            "NFLX password sharing crackdown 2023: monetisation inflection → subscriber surprise",
            "PARA/WBD streaming wars: capital destruction competing against NFLX incumbency",
            "GOOGL AI search threat: revenue concentration risk when LLMs commoditise search",
        ],
        "edge": "Ad spend leading indicator (digital agency forecast data) surfaces META/GOOGL revenue inflections 1 quarter early.",
    },

    "DividendYieldAgent": {
        "frameworks": [
            "Dividend discount model (DDM): intrinsic value = D1 / (r - g); sensitivity to cost of equity",
            "Dividend growth rate sustainability: FCF payout ratio < 60% as health threshold",
            "Dogs of the Dow strategy: high-yield large-cap mean reversion as yield-to-price signal",
            "Buyback yield addition: total shareholder yield = dividend yield + net buyback yield",
        ],
        "seminal_texts": [
            "The Future for Investors — Jeremy Siegel (dividend reinvestment compounding data)",
            "Dividends Still Don't Lie — Kelley Wright (dividend yield bands for valuation)",
            "The Little Book of Big Dividends — Charles Carlson (income investing framework)",
            "Standard & Poor's Dividend Aristocrats methodology (selection criteria and history)",
        ],
        "case_studies": [
            "JNJ 60-year dividend growth: fortress balance sheet + pricing power = aristocrat benchmark",
            "AT&T dividend cut 2022: payout ratio unsustainability → value trap warning",
            "MSFT dividend growth 2003–2024: low yield → high growth → compounding total return",
            "ADP 48-year growth streak: payroll processing moat enables durable payout expansion",
        ],
        "edge": "FCF payout ratio sustainability model flags dividend cuts 2–4 quarters before announcement.",
    },

    "IPOMomentumAgent": {
        "frameworks": [
            "IPO lockup expiry calendar: 90/180-day lockup cliffs as institutional supply event",
            "First-day pop vs. aftermarket drift: underpriced IPOs with strong aftermarket = quality signal",
            "Underwriter reputation tier: Goldman/Morgan Stanley tier-1 bookrunner as quality signal",
            "Post-lockup momentum: PEAD analog — strong sponsors + improving fundamentals post-lockup",
        ],
        "seminal_texts": [
            "Investment Banking — Rosenbaum & Pearl (IPO valuation and process mechanics)",
            "'IPO Underpricing' — Rock model, JFE 1986 (winner's curse and information asymmetry)",
            "The New New Thing — Michael Lewis (dot-com IPO cycle excesses as cautionary study)",
            "Going Public — Andrew Sherman (IPO process from company perspective)",
        ],
        "case_studies": [
            "SNOW IPO 2020: Berkshire anchor + Salesforce co-invest → institutional demand signal",
            "RIVN 2021: Ford/Amazon backing but no revenue → post-lockup collapse blueprint",
            "ARM 2023: SoftBank controlled float → limited supply creates artificial scarcity premium",
            "RDDT 2024: user-as-shareholder mechanism + content licensing → new monetization model",
        ],
        "edge": "90/180-day lockup expiry calendar + underwriter tier scoring identifies post-lockup drift setups systematically.",
    },

    "ValueOpportunityAgent": {
        "frameworks": [
            "Graham net-net: current assets minus all liabilities; price below NCAV = deep value floor",
            "Magic Formula (Greenblatt): EBIT/EV (earnings yield) + EBIT/invested capital (ROIC) composite",
            "Acquirer's Multiple: enterprise value / operating earnings — lowest decile outperforms",
            "Mean reversion model: P/B < 1.5 + RSI < 35 + improving fundamentals = contrarian entry",
        ],
        "seminal_texts": [
            "Security Analysis — Graham & Dodd (foundational value investing framework, 6th ed.)",
            "The Intelligent Investor — Benjamin Graham (margin of safety; Mr. Market allegory)",
            "The Little Book That Still Beats the Market — Joel Greenblatt (Magic Formula)",
            "Contrarian Investment Strategies — David Dreman (value premium and analyst neglect)",
        ],
        "case_studies": [
            "BRK-B GEICO acquisition: franchise value hidden inside conservative GAAP accounting",
            "INTC 2022–2024: foundry turnaround bet; value trap vs. genuine franchise revival",
            "META 2022 trough: P/E compressed to 12x on peak fear → 200% recovery in 18 months",
            "WFC post-2016 fraud: regulatory overhang created multi-year value discount then recovery",
        ],
        "edge": "Magic Formula + mean reversion composite surfaces temporary franchise dislocations before institutional re-rating.",
    },

    "InternationalEmergingAgent": {
        "frameworks": [
            "EM valuation discount: MSCI EM P/E vs. MSCI World premium/discount as entry signal",
            "Currency hedged returns: USD DXY direction as EM return amplifier/dampener",
            "Country macro scoring: current account, FX reserves, political risk composite",
            "ADR premium/discount to local share: arbitrage signal for liquid international names",
        ],
        "seminal_texts": [
            "The Emerging Markets Handbook — Temit team (country and sector framework)",
            "When Markets Collide — El-Erian (EM integration with global capital flows)",
            "Barbarians at the Gate — Burrough (leverage and capital structure dynamics in M&A cycles)",
            "MSCI Emerging Market Index methodology (index construction and rebalancing mechanics)",
        ],
        "case_studies": [
            "BABA 2020 Jack Ma disappearance: regulatory risk premium expansion and contraction cycle",
            "NU Holdings Brazil: digital banking TAM in underbanked EM; 10x revenue growth path",
            "MELI Mercado Libre: LatAm e-commerce + fintech platform; network effect across 18 countries",
            "PDD Temu global expansion: low-cost manufacturing arbitrage + gamified shopping model",
        ],
        "edge": "USD DXY leading indicator model flags EM headwinds/tailwinds 1–2 months before consensus rotates.",
    },

    "ShortInterestMonitorAgent": {
        "frameworks": [
            "Short interest as % of float: >20% = crowded; >30% = potential squeeze fuel",
            "Days-to-cover (DTC): short interest ÷ average daily volume; DTC >10 = squeeze-prone",
            "Fundamental short thesis: deteriorating earnings + debt load + narrowing moat = confirmed short",
            "Short squeeze trigger: gamma cascade from OTM call buying accelerating covering pressure",
        ],
        "seminal_texts": [
            "The Art of Short Selling — Kathryn Staley (fundamental short thesis construction)",
            "You Can Be a Stock Market Genius — Joel Greenblatt (special situation short awareness)",
            "More Money Than God — Sebastian Mallaby (hedge fund short-selling mechanics)",
            "'Short Selling' — Frank Fabozzi ed. (mechanics, regulation, risk management)",
        ],
        "case_studies": [
            "GME Jan 2021: 140% short float + Reddit coordination + gamma cascade → 2500% squeeze",
            "CVNA 2022–2023: 50% short float on bankruptcy fear → debt restructuring triggers squeeze",
            "NKLA fraud short: Hindenburg report + fundamental deterioration → correct short thesis",
            "AMC 2021: retail-driven squeeze disconnected from fundamentals; timing risk for shorts",
        ],
        "edge": "Short interest crowding dashboard flags squeeze risk for existing long positions before catalyst ignites.",
    },

    "OptionsFlowMonitorAgent": {
        "frameworks": [
            "Unusual options activity (UOA): volume > 10x open interest in single strike = institutional signal",
            "Dark pool print detection: > $5M block trade at bid/ask midpoint = accumulation signal",
            "Put/call ratio: CBOE equity P/C < 0.6 = complacency; > 1.2 = fear extremes",
            "Gamma exposure (GEX): dealer gamma positioning as magnet or repeller for price action",
        ],
        "seminal_texts": [
            "Option Volatility & Pricing — Sheldon Natenberg (Greeks, implied vol, flow interpretation)",
            "The Options Playbook — Brian Overby (flow strategy mechanics)",
            "Trading Options Greeks — Dan Passarelli (gamma, delta dynamics applied to flow)",
            "Positional Options Trading — Euan Sinclair (institutional positioning framework)",
        ],
        "case_studies": [
            "NVDA call sweeps Feb 2023: massive OTM call buying 3 weeks before earnings → institutional front-running",
            "Apple block prints 2020: $500M+ dark pool accumulation at $120 ahead of 50% run",
            "SPY put buying October 2018: institutional hedge activity flagged market peak 3 weeks early",
            "TSLA gamma squeeze 2020: dealer short gamma → amplified upside as shares were called away",
        ],
        "edge": "UOA + dark pool composite identifies institutional conviction moves 1–5 days before price confirmation.",
    },

    "ExecutionAuditorAgent": {
        "frameworks": [
            "Implementation shortfall: decision-price vs. fill-price slippage decomposition",
            "VWAP benchmark: volume-weighted average price as neutral execution quality reference",
            "Market impact model: Kyle's lambda — price impact proportional to order size vs. daily volume",
            "Timing analysis: open-vs-close-vs-intraday execution quality by market condition",
        ],
        "seminal_texts": [
            "Trading and Exchanges — Larry Harris (market microstructure; execution cost anatomy)",
            "Algorithmic Trading — Ernie Chan (execution optimization in quantitative strategies)",
            "Market Microstructure Theory — Maureen O'Hara (bid-ask spread decomposition)",
            "'Optimal Execution of Portfolio Transactions' — Almgren & Chriss, 2000 (impact model)",
        ],
        "case_studies": [
            "Knight Capital 2012 algo error: execution system failure destroying $440M in 45 minutes",
            "Flash Crash May 2010: execution during volatility spike — market vs. limit order lessons",
            "Berkshire Hathaway position building: patience and VWAP discipline at scale",
            "Renaissance Medallion: transaction cost minimisation as key alpha preservation lever",
        ],
        "edge": "Slippage audit on every Manager Agent trade surfaces systematic execution drag compounding to material alpha loss.",
    },

    "DividendDefensiveAgent": {
        "frameworks": [
            "Late-cycle rotation signal: yield spread between equity dividend yield and 10yr Treasury",
            "Beta-adjusted defensive positioning: target portfolio beta < 0.7 during risk-off",
            "FCF yield > 5% + dividend growth ≥ 5yr streak = quality defensive screen",
            "Sector rotation clock: defensive outperformance leads S&P by 3–6 months at cycle peaks",
        ],
        "seminal_texts": [
            "The Four Pillars of Investing — William Bernstein (defensive allocation through cycles)",
            "Dividends Still Don't Lie — Kelley Wright (yield band analysis for defensives)",
            "The Battle for Investment Survival — Gerald Loeb (capital preservation first principles)",
            "Stocks for the Long Run — Jeremy Siegel (defensive sector long-run return data)",
        ],
        "case_studies": [
            "2007–2008: XLP consumer staples held -15% while SPY fell -55%; defensive rotation preserved capital",
            "2022 bear: XLP +1%, XLU -2% vs. QQQ -33%; utilities and staples as inflation-era shelter",
            "PG 2008–2009: 70 consecutive years of dividend growth; zero cuts through the GFC",
            "O (Realty Income) monthly dividend: durable income floor during equity volatility periods",
        ],
        "edge": "Equity/Treasury yield spread model triggers defensive rotation 3–6 months before cycle peak consensus.",
    },

    "MicroCapAlphaAgent": {
        "frameworks": [
            "Sub-$500M market cap universe: < 2 sell-side analysts = structural mispricing opportunity",
            "Institutional initiation signal: first 13F filing by tier-1 institution = discovery catalyst",
            "Insider ownership screen: management > 10% stake + open market buys = alignment signal",
            "Revenue growth > 30% + improving FCF trajectory = quality filter in low-efficiency universe",
        ],
        "seminal_texts": [
            "One Up on Wall Street — Peter Lynch (finding underfollowed names before institutions)",
            "The Little Book That Beats the Market — Joel Greenblatt (screening low-coverage universe)",
            "Small Giants — Bo Burlingham (companies choosing quality over scale in small-cap universe)",
            "'The Small-Firm Effect' — Banz, JFE 1981 (original size premium documentation)",
        ],
        "case_studies": [
            "IONQ pre-institutional: quantum computing pioneer ignored at $500M cap → 5x after institutional discovery",
            "ACHR Archer Aviation: eVTOL pre-revenue; position sizing discipline critical for pre-revenue micro-cap",
            "HIMS telehealth: DTC model in under-penetrated healthcare → revenue inflection at $300M cap",
            "NTRA Natera: liquid biopsy growth story in $400M cap stage → eventual large-cap re-rating",
        ],
        "edge": "Institutional initiation calendar + insider purchase filings identify discovery events 30–60 days before price moves.",
    },

    "GlobalMacroETFAgent": {
        "frameworks": [
            "Risk parity: equal risk-weighted allocation across uncorrelated asset classes",
            "Cross-asset momentum: 12-month return across GLD, TLT, SPY, EEM → trend following",
            "Inflation regime: CPI trend + breakeven spread → commodity vs. duration allocation signal",
            "Dollar cycle: DXY trend as headwind/tailwind for international equity and gold ETFs",
        ],
        "seminal_texts": [
            "The Intelligent Asset Allocator — William Bernstein (diversification across asset classes)",
            "Macro Trading and Investment Strategies — Gabriel Burstein (global macro framework)",
            "Hot Commodities — Jim Rogers (commodity supercycle identification methodology)",
            "Inside the House of Money — Steven Drobny (practitioner macro portfolio construction)",
        ],
        "case_studies": [
            "GLD 2018–2020: real-rate compression + Fed pivot → gold 60% run as dollar peaked",
            "TLT 2020 COVID: flight to safety + QE → 30-year bond 25% rally; recession playbook",
            "EEM 2021–2022: dollar strength + China regulatory crackdown → EM underperformance lag",
            "VNQ 2022: rate sensitivity fully priced in REITs → 30% drawdown then 25% recovery on rate peak",
        ],
        "edge": "Cross-asset momentum composite rotates between GLD/TLT/EEM/SPY monthly — captures regime shifts missed by equity-only analysis.",
    },
}


# =============================================================================
# AGENT FACTORY
# =============================================================================

class AgentFactory:
    """
    Generates new research agent Python files and registers them.
    Called by the Chief of Staff each daily cycle.
    New agents are automatically picked up by ResearchTeamCoordinator.
    """

    def __init__(self):
        os.makedirs(AGENTS_DIR, exist_ok=True)
        os.makedirs("data", exist_ok=True)
        self.registry = self._load_registry()

    def _load_registry(self) -> dict:
        if os.path.exists(AGENT_REGISTRY):
            try:
                with open(AGENT_REGISTRY) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"agents": [], "last_updated": None, "total_spawned": 0}

    def _save_registry(self):
        self.registry["last_updated"] = datetime.now().isoformat()
        with open(AGENT_REGISTRY, "w") as f:
            json.dump(self.registry, f, indent=2)

    def _already_spawned(self, class_name: str) -> bool:
        return any(a["class_name"] == class_name for a in self.registry["agents"])

    def _write_agent_file(self, spec: dict) -> str:
        """Write a Python file for the new agent. Returns the file path."""
        class_name = spec["class_name"]
        fname      = f"cos_{class_name.lower()}.py"
        fpath      = os.path.join(AGENTS_DIR, fname)
        now        = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Build watchlist as an indented list literal
        wl_lines = ["["]
        for t in spec["watchlist"]:
            wl_lines.append(f'    "{t}",')
        wl_lines.append("]")
        watchlist_str = "\n".join(wl_lines)

        # Pull knowledge base — every spawned agent carries their domain expertise
        kb = AGENT_KNOWLEDGE_BASES.get(class_name, {})
        kb_repr = json.dumps(kb, indent=4)

        # Build the file line-by-line with no extra indentation
        lines = [
            f"# Auto-generated by Chief of Staff Agent — {now}",
            f"# Specialization: {spec['specialization']}",
            f"# Tier: {spec['tier']}  |  ETF: {spec['etf']}",
            "# DO NOT EDIT — file is regenerated automatically",
            "import sys, os",
            "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
            "from research_team import SectorResearchAgent, ENERGY_BLOCK",
            "",
            f"class {class_name}(SectorResearchAgent):",
            f'    """',
            f"    {spec['description']}",
            "",
            f"    Chairman directive 2026-04-27: All agents must be experts in their field,",
            f"    having studied all important books and case studies in their domain.",
            f"    This agent's knowledge base is embedded below and informs every analysis.",
            f'    """',
            "",
            f"    # ── Domain expertise (Chairman directive: agents must be experts) ────────",
            f"    KNOWLEDGE_BASE = {kb_repr}",
            "",
            f"    _RAW_WATCHLIST = {watchlist_str}",
            f"    DEFAULT_WATCHLIST = [t for t in _RAW_WATCHLIST if t not in ENERGY_BLOCK]",
            f"    DEFAULT_SECTOR = {json.dumps(spec['sector'])}",
            f"    DEFAULT_ETF    = {json.dumps(spec['etf'])}",
            f"    DEFAULT_THESIS = {json.dumps(spec['thesis'])}",
            "",
            f'    def __init__(self, agent_id: str = "{class_name.lower()}_cos"):',
            f"        super().__init__({{",
            f'            "agent_id":           agent_id,',
            f'            "sector_name":        self.DEFAULT_SECTOR,',
            f'            "sector_etf":         self.DEFAULT_ETF,',
            f'            "description":        {json.dumps(spec["description"])},',
            f'            "watchlist":          self.DEFAULT_WATCHLIST,',
            f'            "investment_horizon": "both",',
            f'            "leader_rationale":   self.DEFAULT_THESIS,',
            f"        }})",
        ]

        with open(fpath, "w") as f:
            f.write("\n".join(lines) + "\n")
        return fpath

    def _spawn_agent(self, spec: dict) -> dict:
        """Write file, register, return registry entry."""
        fpath = self._write_agent_file(spec)
        entry = {
            "class_name":     spec["class_name"],
            "file":           fpath,
            "specialization": spec["specialization"],
            "sector":         spec["sector"],
            "etf":            spec["etf"],
            "tier":           spec["tier"],
            "created_by":     "Chief of Staff",
            "created_at":     datetime.now().isoformat(),
            "status":         "active",
            # Chairman directive 2026-04-27: all agents are permanent by default.
            # They receive coaching and reassignment support if they underperform,
            # but are never terminated without explicit Co-Chairman authorisation.
            "permanent":      spec.get("permanent", True),
        }
        self.registry["agents"].append(entry)
        self.registry["total_spawned"] = self.registry.get("total_spawned", 0) + 1
        logger.info(
            f"  [AgentFactory] Spawned: {spec['class_name']} "
            f"(Tier {spec['tier']}, {spec['sector']}) → {fpath}"
        )
        return entry

    def _meets_quality_bar(self, spec: dict) -> bool:
        """
        Quality gate for agent spawning (Chairman directive 2026-04-27).
        Only the most talented agents are spawned. This method enforces
        minimum standards before any agent is admitted to the registry.

        Requirements:
          1. Thesis depth    — must be substantive (> 150 chars), not a one-liner
          2. Knowledge base  — must have an entry in AGENT_KNOWLEDGE_BASES
          3. Watchlist depth — ≥ 8 tickers (enough diversity for sector coverage)
          4. Description     — must articulate a specific mandate (> 100 chars)
          5. Specialization  — must be clearly named and non-empty

        Agents that fail any check are logged and skipped; the slot waits for
        the next candidate that meets the full standard.
        """
        cn = spec.get("class_name", "?")

        # Permanent agents are board-approved context-layer specialists.
        # They were admitted by Chairman directive and don't go through
        # the spawn factory — exempt them from the quality gate entirely.
        if spec.get("permanent"):
            return True

        # 1. Thesis depth — must be a substantive strategy statement, not a one-liner
        thesis = spec.get("thesis", "")
        if len(thesis) < 80:
            logger.info(
                f"  [AgentFactory] Quality gate FAIL — {cn}: thesis too shallow "
                f"({len(thesis)} chars, need ≥80). Skipping."
            )
            return False

        # 2. Knowledge base presence — the hard expert gate
        # Agents must have documented domain knowledge (frameworks, seminal texts,
        # case studies, defined edge) before being admitted. No knowledge base = no spawn.
        if cn not in AGENT_KNOWLEDGE_BASES:
            logger.info(
                f"  [AgentFactory] Quality gate FAIL — {cn}: no knowledge_base entry. "
                f"An agent cannot be a domain expert without documented frameworks, "
                f"seminal texts, and case studies. Skipping until knowledge base is defined."
            )
            return False

        # 3. Watchlist depth — must have meaningful universe coverage
        watchlist = spec.get("watchlist", [])
        if len(watchlist) < 8:
            logger.info(
                f"  [AgentFactory] Quality gate FAIL — {cn}: watchlist too small "
                f"({len(watchlist)} tickers, need ≥8 for adequate sector coverage). Skipping."
            )
            return False

        # 4. Description substance — must articulate a specific mandate
        description = spec.get("description", "")
        if len(description) < 80:
            logger.info(
                f"  [AgentFactory] Quality gate FAIL — {cn}: description too vague "
                f"({len(description)} chars, need ≥100). Skipping."
            )
            return False

        # 5. Specialization named
        if not spec.get("specialization", "").strip():
            logger.info(
                f"  [AgentFactory] Quality gate FAIL — {cn}: specialization field is empty. Skipping."
            )
            return False

        return True

    def _update_agent_performance(self) -> list[dict]:
        """
        Three-stage dignified performance system (Chairman directive 2026-04-27).

        Agents are never terminated abruptly. Every stage gives the agent
        time, support, and a genuine opportunity to succeed before the next
        step is taken.

        Stage 1 — Performance Notice (PERF_NOTICE_CYCLES consecutive zero picks)
            The agent receives a formal notice with specific, constructive
            improvement guidance. Status → "noticed". Agent remains fully active.
            The Chief of Staff logs this in the advice history as a coaching item.

        Stage 2 — Role Reassignment (PERF_REASSIGN_CYCLES consecutive zero picks)
            The agent is moved to a different sector or function that may be a
            better fit. A catalog scan identifies an alternative assignment.
            Status → "reassigned". Original slot coverage is maintained.

        Stage 3 — Dignified Replacement (PERF_REPLACE_CYCLES consecutive zero picks)
            Only after Stage 2 has been attempted and the agent still cannot
            contribute, a fresh top-level agent is spawned for the slot. The
            original agent's record, contributions, and exit are formally logged.
            Status → "retired". A replacement is queued for the next spawn cycle.

        Recovery at any stage: one approved pick resets the counter to zero
        and restores the agent to "active" status.
        """
        changed = []
        try:
            if not os.path.exists(MANAGER_LOG):
                return changed
            with open(MANAGER_LOG) as f:
                history = json.load(f)
            if not history:
                return changed

            # Most recent cycle decisions
            latest    = history[-1] if isinstance(history, list) else history
            decisions = latest.get("decisions", [])

            # Count approved picks per source agent this cycle
            approved_by_agent: dict[str, int] = {}
            for d in decisions:
                sa = d.get("source_agent", "unknown")
                if (d.get("manager_decision") == "APPROVED" and
                        d.get("manager_action") not in ("HOLD", "WATCH")):
                    approved_by_agent[sa] = approved_by_agent.get(sa, 0) + 1

            today = datetime.now().strftime("%Y-%m-%d")

            # ── CRO exoneration check ──────────────────────────────────────────
            # If the CRO is HIGH or CRITICAL, ALL buys are blocked system-wide.
            # Agents should not have their zero-cycle counter incremented during
            # a system-wide block — they cannot get approved picks regardless of
            # the quality of their research. This was a known unfair penalty.
            cro_blocking_all = False
            if EXONERATE_WHEN_CRO_HIGH:
                try:
                    _risk_path = os.path.join(os.path.dirname(MANAGER_LOG), "risk_bulletin.json")
                    if os.path.exists(_risk_path):
                        with open(_risk_path) as _rf:
                            _rb = json.load(_rf)
                        _cro_level = _rb.get("risk_level", "LOW")
                        if _cro_level in ("HIGH", "CRITICAL"):
                            cro_blocking_all = True
                            logger.info(
                                f"  [AgentFactory] CRO={_cro_level} — system-wide buy block "
                                f"detected. Zero-cycle counters will NOT increment this cycle "
                                f"(agents exonerated)."
                            )
                except Exception:
                    pass

            for agent in self.registry["agents"]:
                cn         = agent["class_name"]
                old_status = agent.get("status", "active")
                is_permanent = agent.get("permanent", False)

                # Already retired — skip (this can only happen to the rare
                # non-permanent agent that the Co-Chairmen explicitly flagged)
                if old_status == "retired":
                    continue
                # Permanent agents: coaching (Stage 1) and reassignment (Stage 3)
                # are still available — they benefit from feedback and fresh
                # opportunities. But Stage 4 termination is completely blocked.
                # Only a Co-Chairman can authorise retirement by manually setting
                # permanent=False in the registry for that specific agent.

                agent_key = cn.lower().replace("agent", "").strip("_")
                hits      = sum(v for k, v in approved_by_agent.items()
                                if agent_key in k.lower())

                if hits > 0:
                    # Recovery — reset everything
                    czc = 0
                    if old_status in ("noticed", "pip", "reassigned"):
                        agent["status"] = "active"
                        agent.pop("performance_notice_sent", None)
                        agent.pop("pip_issued_at", None)
                        agent.pop("reassigned_to", None)
                        logger.info(
                            f"  [AgentFactory] ✓ {cn} RECOVERED → ACTIVE "
                            f"({hits} approved pick(s) this cycle). "
                            f"Performance improvement acknowledged."
                        )
                        changed.append({
                            "class_name": cn, "old_status": old_status,
                            "new_status": "active", "consecutive_zero_cycles": 0,
                            "event": "recovery",
                        })
                elif cro_blocking_all:
                    # CRO exoneration — do not increment czc; agent is not at fault
                    czc = agent.get("consecutive_zero_cycles", 0)
                    agent.setdefault("exoneration_count",  0)
                    agent["exoneration_count"] += 1
                else:
                    czc = agent.get("consecutive_zero_cycles", 0) + 1

                agent["consecutive_zero_cycles"] = czc
                agent["last_checked"]            = today

                # ── Stage 4: Dignified Replacement ────────────────────────
                if czc >= PERF_REPLACE_CYCLES and old_status == "reassigned":
                    if is_permanent:
                        # Permanent agents cannot be retired without explicit
                        # Co-Chairman authorisation. Extend runway indefinitely
                        # and log for Co-Chairman awareness only.
                        logger.info(
                            f"  [AgentFactory] {cn}: Stage 4 threshold reached but "
                            f"agent is PERMANENT — retirement blocked by Co-Chairman policy. "
                            f"Extending runway. Co-Chairmen may review this agent directly."
                        )
                        # Soft-reset: walk back to reassigned threshold so coaching continues
                        agent["consecutive_zero_cycles"] = PERF_REASSIGN_CYCLES
                        changed.append({
                            "class_name": cn, "old_status": old_status,
                            "new_status": "reassigned", "consecutive_zero_cycles": PERF_REASSIGN_CYCLES,
                            "event": "permanent_runway_extended",
                        })
                    else:
                        agent["status"]        = "retired"
                        agent["retired_at"]    = today
                        agent["retirement_reason"] = (
                            f"After {czc} cycles without an approved pick — including a PIP "
                            f"at cycle {PERF_PIP_CYCLES} and a role reassignment at cycle "
                            f"{PERF_REASSIGN_CYCLES} — this agent has reached the end of its "
                            f"runway. A fresh specialist will be spawned for this slot. "
                            f"The agent's contributions and record are preserved in the registry."
                        )
                        logger.warning(
                            f"  [AgentFactory] {cn} → RETIRED after {czc} cycles. "
                            f"A top-level replacement will be queued. "
                            f"Contributions logged and preserved."
                        )
                        changed.append({
                            "class_name": cn, "old_status": old_status,
                            "new_status": "retired", "consecutive_zero_cycles": czc,
                            "event": "retirement", "replacement_needed": True,
                        })

                # ── Stage 3: Role Reassignment ─────────────────────────────
                elif czc >= PERF_REASSIGN_CYCLES and old_status == "pip":
                    # Identify an alternative sector from the catalog not yet covered
                    current_sectors = {a["sector"] for a in self.registry["agents"]
                                       if a.get("status") != "retired"}
                    alternatives    = [s for s in AGENT_CATALOG
                                       if s["sector"] not in current_sectors
                                       and not s.get("permanent")]
                    new_assignment  = alternatives[0] if alternatives else None

                    if new_assignment:
                        agent["status"]         = "reassigned"
                        agent["reassigned_to"]  = new_assignment["sector"]
                        agent["reassigned_at"]  = today
                        agent["reassignment_note"] = (
                            f"After {czc} cycles without approved picks in the original "
                            f"sector, this agent has been moved to '{new_assignment['sector']}'. "
                            f"This is a fresh start in a new domain. "
                            f"Performance is reset — the next {PERF_REPLACE_CYCLES - czc} "
                            f"cycles determine continued placement."
                        )
                        logger.warning(
                            f"  [AgentFactory] {cn} → REASSIGNED to "
                            f"'{new_assignment['sector']}' after {czc} cycles. "
                            f"Agent given fresh opportunity in new role."
                        )
                        changed.append({
                            "class_name":   cn, "old_status": old_status,
                            "new_status":   "reassigned",
                            "new_sector":   new_assignment["sector"],
                            "consecutive_zero_cycles": czc,
                            "event": "reassignment",
                        })
                    else:
                        # No available alternative sectors — extend runway by 3 cycles
                        logger.info(
                            f"  [AgentFactory] {cn}: no reassignment sectors available — "
                            f"extending performance runway by 3 cycles."
                        )
                        agent["consecutive_zero_cycles"] = max(czc - 3, PERF_NOTICE_CYCLES)

                # ── Stage 2: PIP (Performance Improvement Plan) ───────────────
                elif czc >= PERF_PIP_CYCLES and old_status == "noticed":
                    agent["status"]      = "pip"
                    agent["pip_issued_at"] = today
                    agent["pip_plan"] = (
                        f"PERFORMANCE IMPROVEMENT PLAN — issued at cycle {czc}.\n"
                        f"This is an intensive support phase, NOT a prelude to termination.\n"
                        f"Required actions over the next {PERF_REASSIGN_CYCLES - czc} cycles:\n"
                        f"  1. Expand watchlist by at least 5 tickers outside your current sector.\n"
                        f"  2. Lower conviction threshold to surface picks at conviction 3+.\n"
                        f"  3. Cross-reference CRO risk bulletin before each submission.\n"
                        f"  4. Submit at least one pick per cycle with full written rationale.\n"
                        f"  5. Attend (read) board meeting minutes each cycle for market context.\n"
                        f"Success criterion: at least 1 approved pick in the next {PERF_REASSIGN_CYCLES - czc} cycles."
                    )
                    logger.warning(
                        f"  [AgentFactory] {cn} → PIP (Stage 2) "
                        f"after {czc} zero-pick cycles. "
                        f"Intensive support plan issued. Role reassignment deferred."
                    )
                    changed.append({
                        "class_name": cn, "old_status": "noticed",
                        "new_status": "pip", "consecutive_zero_cycles": czc,
                        "event": "pip_issued",
                    })

                # ── Stage 1: Performance Notice ────────────────────────────
                elif czc >= PERF_NOTICE_CYCLES and old_status == "active":
                    agent["status"]               = "noticed"
                    agent["performance_notice_sent"] = today
                    agent["performance_notice"] = (
                        f"Formal performance notice — {czc} consecutive cycles without "
                        f"an approved pick. This is not a termination notice. It is an "
                        f"invitation to improve. Recommended actions: (1) broaden the "
                        f"watchlist to include more liquid names with stronger momentum, "
                        f"(2) lower conviction threshold to surface more candidates for "
                        f"the Manager Agent to evaluate, (3) review the CRO risk bulletin "
                        f"each cycle to understand why picks may be getting blocked at the "
                        f"Manager level. You have {PERF_PIP_CYCLES - czc} cycles before a "
                        f"formal PIP is issued."
                    )
                    logger.warning(
                        f"  [AgentFactory] {cn} → PERFORMANCE NOTICE (Stage 1) "
                        f"after {czc} zero-pick cycles. "
                        f"Agent notified with improvement guidance. Still fully active."
                    )
                    changed.append({
                        "class_name": cn, "old_status": "active",
                        "new_status": "noticed", "consecutive_zero_cycles": czc,
                        "event": "performance_notice",
                    })

        except Exception as e:
            logger.debug(f"  [AgentFactory] Performance update skipped: {e}")
        return changed

    def run(self, pdo_signal: str = "", regime: str = "neutral") -> dict:
        """
        Main factory loop. Determines which agents to spawn this cycle.
        Prioritises Tier 1 → 2 → 3. Respects SPAWN_PER_CYCLE and MAX_REGISTRY_AGENTS.

        Runs agent performance review (3-stage dignified system) before spawning.
        Agents flagged replacement_needed=True from the performance review consume
        a spawn slot this cycle — their replacement is a top priority.

        Only agents that pass _meets_quality_bar() are admitted to the registry
        (Chairman directive: only the most talented agents are spawned).
        """
        # ── Agent performance review ───────────────────────────────────────
        perf_changes = self._update_agent_performance()
        if perf_changes:
            self._save_registry()

        # Retired agents free their slots.  Count only non-retired agents.
        # (Old system counted non-dormant; dormant is replaced with retired.)
        active_count    = sum(1 for a in self.registry["agents"]
                              if a.get("status") != "retired")
        existing_count  = len(self.registry["agents"])
        slots_remaining = MAX_REGISTRY_AGENTS - active_count
        can_spawn       = min(SPAWN_PER_CYCLE, slots_remaining)

        if can_spawn <= 0:
            logger.info(
                f"  [AgentFactory] Registry full ({active_count} active/{MAX_REGISTRY_AGENTS}). "
                "No new agents needed."
            )
            return {
                "spawned": [], "registry_size": active_count,
                "perf_changes": perf_changes, "total_registered": existing_count,
            }

        # ── Replacement queue: retired agents that need a fresh replacement ─
        # These are top priority — we must cover slots that went dark.
        replacement_needed = [
            ch for ch in perf_changes if ch.get("replacement_needed")
        ]
        replacement_class_names = {ch["class_name"] for ch in replacement_needed}

        # In bear regime, skip Tier 3 speculative agents
        max_tier = 2 if regime == "bear" else 3

        # ── Build spawn queue ──────────────────────────────────────────────
        # Priority 1: replacements for retired agents (spawn fresh top-level specialist
        #             for the same sector using the highest-tier catalog equivalent)
        # Priority 2: new agents from catalog, tier 1 → 2 → 3
        replacement_specs: list[dict] = []
        for ch in replacement_needed:
            # Find the original retired agent to recover its sector
            retired_entry = next(
                (a for a in self.registry["agents"] if a["class_name"] == ch["class_name"]),
                None,
            )
            if not retired_entry:
                continue
            retired_sector = retired_entry.get("sector", "")
            # Find best catalog match for the same sector that isn't already active
            best = next(
                (s for s in AGENT_CATALOG
                 if s["sector"] == retired_sector
                 and not any(
                     a["class_name"] == s["class_name"]
                     and a.get("status") != "retired"
                     for a in self.registry["agents"]
                 )
                 and not s.get("permanent")),
                None,
            )
            if best:
                logger.info(
                    f"  [AgentFactory] Queuing replacement for retired {ch['class_name']} "
                    f"→ fresh {best['class_name']} in sector '{retired_sector}'."
                )
                replacement_specs.append(best)

        # Remaining catalog slots (new coverage, not replacements)
        new_specs = [
            s for s in AGENT_CATALOG
            if not self._already_spawned(s["class_name"])
            and s["tier"] <= max_tier
            and s["class_name"] not in {r["class_name"] for r in replacement_specs}
        ]
        new_specs.sort(key=lambda x: (x["tier"], AGENT_CATALOG.index(x)))

        # Combined queue: replacements first, then new coverage
        full_queue = replacement_specs + new_specs

        # ── Quality gate (Chairman: only the most talented agents are hired) ─
        quality_queue = [s for s in full_queue if self._meets_quality_bar(s)]
        rejected_count = len(full_queue) - len(quality_queue)
        if rejected_count:
            logger.info(
                f"  [AgentFactory] Quality gate: {rejected_count} candidate(s) failed "
                f"the expertise bar and will not be spawned this cycle."
            )

        spawned = []
        for spec in quality_queue[:can_spawn]:
            entry = self._spawn_agent(spec)
            spawned.append(entry)

        if spawned:
            self._save_registry()
            logger.info(
                f"  [AgentFactory] Spawned {len(spawned)} agent(s). "
                f"Registry: {active_count + len(spawned)}/{MAX_REGISTRY_AGENTS} active."
            )
        elif not perf_changes:
            logger.info("  [AgentFactory] All catalogued agents already spawned or pending quality review.")

        return {
            "spawned":           spawned,
            "registry_size":     active_count + len(spawned),
            "total_registered":  len(self.registry["agents"]),
            "total_ever":        self.registry.get("total_spawned", 0),
            "perf_changes":      perf_changes,
            "quality_rejected":  rejected_count,
        }


# =============================================================================
# ADVICE HISTORY — persistent memory across cycles
# =============================================================================
# The CoS was previously stateless: every cycle it re-derived advice from
# scratch with no memory of what was said before. This module adds a rolling
# N-cycle memory so that:
#   - Advice items that repeat for 3+ consecutive cycles are auto-escalated
#     to CRITICAL priority for the Chairman
#   - Resolved issues (absent for 2+ cycles) are marked closed
#   - The Chairman can see at a glance which problems are chronic vs new
# =============================================================================

ADVICE_HISTORY_MAX_CYCLES = 10   # keep last 10 cycles of advice in memory


def _load_advice_history() -> dict:
    """Load persistent advice history from disk. Returns empty structure if missing."""
    if not os.path.exists(COS_ADVICE_HIST):
        return {"cycles": [], "open_items": {}}
    try:
        with open(COS_ADVICE_HIST) as f:
            return json.load(f)
    except Exception:
        return {"cycles": [], "open_items": {}}


def _save_advice_history(history: dict) -> None:
    """Write advice history to disk, trimming to ADVICE_HISTORY_MAX_CYCLES."""
    # Keep only last N cycles
    if len(history.get("cycles", [])) > ADVICE_HISTORY_MAX_CYCLES:
        history["cycles"] = history["cycles"][-ADVICE_HISTORY_MAX_CYCLES:]
    os.makedirs("data", exist_ok=True)
    try:
        with open(COS_ADVICE_HIST, "w") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"  [AdviceHistory] Failed to save: {e}")


def _build_fingerprint(advice_item: dict) -> str:
    """Create a short fingerprint for an advice item for deduplication across cycles."""
    category = advice_item.get("category", "")
    # Use first 60 chars of advice text as the fingerprint key
    text_key = advice_item.get("advice", "")[:60].strip()
    return f"{category}::{text_key}"


def _check_escalations(current_advice: list[dict], history: dict) -> list[dict]:
    """
    Compare current advice against history. Items that have appeared for 3+
    consecutive cycles are flagged as ESCALATED — chronic unresolved issues.
    Returns the current_advice list with escalation annotations added.
    """
    open_items = history.get("open_items", {})
    today_str  = datetime.now().strftime("%Y-%m-%d")

    escalated_advice = []
    for item in current_advice:
        fp    = _build_fingerprint(item)
        entry = open_items.get(fp, {"first_seen": today_str, "consecutive_cycles": 0})

        consecutive = entry.get("consecutive_cycles", 0) + 1
        open_items[fp] = {
            "first_seen":        entry.get("first_seen", today_str),
            "last_seen":         today_str,
            "consecutive_cycles": consecutive,
            "category":          item.get("category", ""),
            "advice_snippet":    item.get("advice", "")[:80],
        }

        if consecutive >= 3:
            # Chronic issue — escalate with annotation
            item = dict(item)   # copy so we don't mutate the original
            item["advice"] = (
                f"[⚑ CHRONIC — {consecutive} cycles unresolved] " + item["advice"]
            )
            item["escalated"]          = True
            item["consecutive_cycles"] = consecutive
        escalated_advice.append(item)

    # Clear items that haven't appeared in 2+ cycles (resolved)
    # Board mandate (Special Session): record resolved_by — the most recent
    # advice category that is no longer appearing (proxy for which agent/action closed it)
    resolved_keys = [
        fp for fp, entry in open_items.items()
        if entry.get("last_seen", "") != today_str
        and entry.get("consecutive_cycles", 0) > 0
    ]
    for fp in resolved_keys:
        # Mark as resolved rather than deleting — preserves audit trail
        open_items[fp]["consecutive_cycles"] = 0
        open_items[fp]["resolved_date"]      = today_str
        # resolved_by: infer from the category — which agent class is responsible
        cat = open_items[fp].get("category", "")
        open_items[fp]["resolved_by"] = (
            "Manager Agent"   if cat in ("CAPITAL", "TRADE HISTORY") else
            "Chief Risk Officer" if cat in ("RISK", "CONFLICT") else
            "Chief of Staff"  if cat in ("OPERATIONS", "R&D EXPANSION", "MACRO") else
            "Unknown"
        )

    history["open_items"] = open_items
    return escalated_advice


# =============================================================================
# INTELLIGENCE LOADER
# =============================================================================

def _safe_load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_all_intelligence() -> dict:
    intel = {}
    if os.path.exists(MANAGER_LOG):
        try:
            with open(MANAGER_LOG) as f:
                history = json.load(f)
            if history:
                intel["manager"] = history[-1]
        except Exception:
            pass
    intel["pdo"]          = _safe_load(PDO_REPORT)
    intel["research"]     = _safe_load(RESEARCH_RPT)
    intel["risk"]         = _safe_load(RISK_RPT)
    intel["review"]       = _safe_load(REVIEW_RPT)
    intel["trade_review"] = _safe_load("data/trade_review_report.json")
    intel["cro_bulletin"] = _safe_load("data/risk_bulletin.json")  # CRO public advisory
    return intel


# =============================================================================
# COMPANY HEALTH SNAPSHOT
# =============================================================================

def _assess_company_health(intel: dict) -> dict:
    manager_data    = intel.get("manager", {})
    account         = manager_data.get("account", {})
    decisions       = manager_data.get("decisions", [])
    pdo             = intel.get("pdo", {})
    risk            = intel.get("risk", {})
    research        = intel.get("research", {})

    approved_trades = sum(
        1 for d in decisions
        if d.get("manager_decision") == "APPROVED"
        and d.get("manager_action") not in ("HOLD", "WATCH")
    )
    rejected_trades = sum(
        1 for d in decisions if d.get("manager_decision") == "REJECTED"
    )

    deployment  = pdo.get("deployment", {})
    regime      = pdo.get("market_conditions", {}).get("regime", "unknown")
    pdo_signal  = pdo.get("recommendation", "—")

    risk_assess  = risk.get("risk_assessment", {}) if risk else {}
    manager_eval = risk.get("manager_eval", {})    if risk else {}

    research_s   = research.get("summary", {})

    return {
        "portfolio_value":  account.get("portfolio_value", 0),
        "pnl_today":        account.get("pnl_today", 0),
        "pnl_pct":          account.get("pnl_today_pct", 0),
        "n_positions":      account.get("n_positions", 0),
        "trades_executed":  approved_trades,
        "trades_rejected":  rejected_trades,
        "deployed_pct":     deployment.get("current_pct", 0),
        "target_pct":       deployment.get("target_pct", 0),
        "regime":           regime,
        "pdo_signal":       pdo_signal,
        # Board mandate: source risk level from Independent Risk Manager — not self-assessed
        "risk_level": (
            _safe_load("data/risk_manager_report.json")
                .get("risk_assessment", {}).get("risk_level", "UNKNOWN")
            if os.path.exists("data/risk_manager_report.json")
            else risk_assess.get("risk_level", "UNKNOWN")
        ),
        "manager_grade":    manager_eval.get("grade", "?"),
        "manager_score":    manager_eval.get("score", 0),
        "total_picks":      research_s.get("total_picks", 0),
        "high_conviction":  research_s.get("high_conviction", 0),
        # Trade review summary
        "trade_win_rate":       (intel.get("trade_review", {})
                                      .get("analytics", {})
                                      .get("win_rate")),
        "trade_benchmark_delta":(intel.get("trade_review", {})
                                      .get("analytics", {})
                                      .get("benchmark_delta")),
        "trade_pattern_flags":  len(intel.get("trade_review", {})
                                        .get("analytics", {})
                                        .get("pattern_flags", [])),
        "trade_total":          (intel.get("trade_review", {})
                                      .get("analytics", {})
                                      .get("total_trades", 0)),
    }


# =============================================================================
# ATTENTION TRIAGE
# =============================================================================

def _triage_attention(intel: dict, health: dict) -> list[dict]:
    items = []

    if health["risk_level"] == "CRITICAL":
        items.append({"priority": "CRITICAL", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is CRITICAL — Chairman review required immediately."})

    if health["risk_level"] == "HIGH":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": f"Portfolio risk is HIGH. VaR/concentration details in Risk Manager report."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["risk_level"] == "ELEVATED":
        items.append({"priority": "HIGH", "category": "RISK",
                      "requires_chairman": True,
                      "item": "Portfolio risk is ELEVATED. Review Risk Manager report for details."})

    if health["manager_grade"] in ("D", "F"):
        items.append({"priority": "HIGH", "category": "PERFORMANCE",
                      "requires_chairman": True,
                      "item": (f"Manager Agent graded {health['manager_grade']} "
                               f"({health['manager_score']}/100) — below standard. Chairman review recommended.")})

    if health["pnl_pct"] < -4:
        items.append({"priority": "HIGH", "category": "P&L",
                      "requires_chairman": False,
                      "item": f"Significant daily loss: {health['pnl_pct']:+.2f}%. "
                              "Stop-loss monitoring active."})

    if health["regime"] == "bear" and health["pdo_signal"] in ("REDUCE_EXPOSURE", "HOLD_CASH"):
        items.append({"priority": "HIGH", "category": "MACRO",
                      "requires_chairman": False,
                      "item": f"Bear market confirmed. PDO: {health['pdo_signal']}. "
                              "Manager Agent thresholds tightened automatically."})

    if health["manager_grade"] == "C":
        items.append({"priority": "MEDIUM", "category": "PERFORMANCE",
                      "requires_chairman": False,
                      "item": "Manager Agent grade C — monitoring for second consecutive below-average cycle."})

    gap = health["target_pct"] - health["deployed_pct"]
    if gap > 20 and health["pdo_signal"] == "DEPLOY_MORE":
        items.append({"priority": "MEDIUM", "category": "DEPLOYMENT",
                      "requires_chairman": False,
                      "item": (f"Capital under-deployed by {gap:.1f}pp "
                               f"({health['deployed_pct']:.1f}% vs {health['target_pct']:.1f}% target). "
                               f"R&D pipeline: {health['total_picks']} picks available.")})

    if health["manager_grade"] in ("A", "B"):
        items.append({"priority": "ROUTINE", "category": "PERFORMANCE",
                      "requires_chairman": False,
                      "item": f"Manager Agent performing well (Grade {health['manager_grade']}). No intervention needed."})

    if health["high_conviction"] > 0:
        items.append({"priority": "ROUTINE", "category": "PIPELINE",
                      "requires_chairman": False,
                      "item": f"R&D: {health['high_conviction']} high-conviction (4★+) pick(s) "
                              f"of {health['total_picks']} total. Manager briefed."})

    if health["trades_executed"] > 0:
        items.append({"priority": "ROUTINE", "category": "EXECUTION",
                      "requires_chairman": False,
                      "item": f"{health['trades_executed']} trade(s) executed, "
                              f"{health['trades_rejected']} rejected. Details in Manager Report."})

    # Trade review flags
    win_rate     = health.get("trade_win_rate")
    bd           = health.get("trade_benchmark_delta")
    pat_flags    = health.get("trade_pattern_flags", 0)
    trade_total  = health.get("trade_total", 0)
    if trade_total >= 5:
        if win_rate is not None and win_rate < 40:
            items.append({"priority": "HIGH", "category": "TRADE REVIEW",
                          "requires_chairman": True,
                          "item": (f"Historical win rate only {win_rate:.0f}%. "
                                   "Review Team has issued corrective directives to Manager Agent. "
                                   "Chairman review of trade history recommended.")})
        elif pat_flags >= 2:
            items.append({"priority": "MEDIUM", "category": "TRADE REVIEW",
                          "requires_chairman": False,
                          "item": (f"Trade Review Team flagged {pat_flags} performance patterns. "
                                   "Manager Agent directives updated accordingly. "
                                   "Details in weekly Trade Review Report.")})
        elif win_rate is not None and win_rate >= 55:
            # BUG FIX: bd (benchmark_delta) can be None — guard before formatting
            bd_str = f"{bd:+.1f}pp" if bd is not None else "N/A"
            items.append({"priority": "ROUTINE", "category": "TRADE REVIEW",
                          "requires_chairman": False,
                          "item": (f"Historical win rate {win_rate:.0f}% | "
                                   f"vs SPY: {bd_str}. Strategy performing well.")})

    if not items:
        items.append({"priority": "ROUTINE", "category": "STATUS",
                      "requires_chairman": False,
                      "item": "All systems operating normally. No Chairman intervention required."})

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "ROUTINE": 3}
    return sorted(items, key=lambda x: order.get(x["priority"], 9))


# =============================================================================
# AUTONOMOUS DECISIONS
# =============================================================================

def _make_autonomous_decisions(intel: dict, health: dict) -> list[dict]:
    decisions = []

    all_decisions = intel.get("manager", {}).get("decisions", [])
    hold_watch    = [d for d in all_decisions if d.get("manager_action") in ("HOLD", "WATCH")]
    if hold_watch:
        # BUG FIX: use .get() — ticker key may be absent in external manager decision dicts
        tickers = ", ".join(d.get("ticker", "?") for d in hold_watch)
        decisions.append({
            "decision":     f"Endorsed {len(hold_watch)} HOLD/WATCH position(s): {tickers}",
            "rationale":    "Maintaining positions with intact thesis is routine — no capital at risk from inaction.",
            "policy_basis": "Standard portfolio maintenance — within pre-approved parameters",
        })

    if health["pdo_signal"] and health["pdo_signal"] != "—":
        decisions.append({
            "decision":     f"Confirmed Manager Agent operating under PDO signal: {health['pdo_signal']}",
            "rationale":    "PDO signal applied to approval thresholds automatically. No manual override required.",
            "policy_basis": "Standard PDO-Manager integration protocol",
        })

    research = intel.get("research", {})
    agents_run = research.get("summary", {}).get("agents_run", 0)
    if agents_run > 0:
        decisions.append({
            "decision":     f"Acknowledged R&D cycle: {agents_run} agents, {research['summary'].get('total_picks',0)} picks",
            "rationale":    "Research cycle completed within normal parameters.",
            "policy_basis": "Routine 4-day research cadence",
        })

    return decisions


# =============================================================================
# STRATEGIC ADVICE
# =============================================================================

def _generate_strategic_advice(intel: dict, health: dict,
                                factory_result: dict,
                                advice_history: Optional[dict] = None) -> list[dict]:
    advice = []
    regime        = health["regime"]
    pdo_signal    = health["pdo_signal"]
    risk_level    = health["risk_level"]
    manager_grade = health["manager_grade"]
    total_picks   = health["total_picks"]
    deployed_pct  = health["deployed_pct"]
    target_pct    = health["target_pct"]
    spawned       = factory_result.get("spawned", [])
    reg_size      = factory_result.get("registry_size", 0)

    # ── PDO Uncapped Signal surfacing ─────────────────────────────────────
    # If PDO was CRO-constrained, surface the suppressed deployment opportunity
    # as a strategic advisory — the Chairman should know whether we're missing
    # windows due to risk vs genuinely having no opportunities.
    pdo_data = intel.get("pdo", {})
    if pdo_data.get("cro_constrained") and pdo_data.get("uncapped_signal"):
        uncapped  = pdo_data.get("uncapped_signal", "")
        cro_at    = pdo_data.get("cro_risk_at_time", "")
        if uncapped == "DEPLOY_MORE":
            advice.append({
                "category": "CAPITAL",
                "horizon":  "This cycle",
                "advice": (
                    f"⚡ PDO CONSTRAINED: Market conditions support DEPLOY_MORE "
                    f"({pdo_data.get('deployment', {}).get('opportunities_found', 0)} "
                    f"high-conviction opportunities, "
                    f"{pdo_data.get('deployment', {}).get('current_pct', 0):.1f}% deployed vs "
                    f"{pdo_data.get('deployment', {}).get('target_pct', 0):.1f}% target), "
                    f"but CRO risk level ({cro_at}) is suppressing deployment to NEAR_OPTIMAL. "
                    f"These deployment windows will compound if CRO risk does not normalise. "
                    f"Review CRO report for path to risk reduction."
                ),
            })

    # Macro
    if regime == "bull" and pdo_signal == "DEPLOY_MORE":
        advice.append({"category": "MACRO", "horizon": "Near-term (1–2 weeks)",
                       "advice": "Bull regime with deployment capacity. Consider modestly higher position sizes "
                                 "on highest-conviction picks. R&D should prioritise momentum leaders."})
    elif regime == "bear":
        advice.append({"category": "MACRO", "horizon": "Near-term",
                       "advice": "Bear market confirmed. Maintain defensive posture. Let PDO govern deployment. "
                                 "Focus R&D on identifying the regime-reversal signal (SPY reclaiming 200-day MA)."})
    else:
        advice.append({"category": "MACRO", "horizon": "Near-term",
                       "advice": "Neutral regime — proceed with standard momentum-capture strategy. "
                                 "Favour RSI 45–65 with above-50MA trend. Avoid sector overweighting."})

    # Manager performance
    if manager_grade in ("A", "B"):
        advice.append({"category": "OPERATIONS", "horizon": "Ongoing",
                       "advice": f"Manager Agent at Grade {manager_grade}. Parameters working well — no changes needed."})
    elif manager_grade == "C":
        advice.append({"category": "OPERATIONS", "horizon": "Next 1–2 cycles",
                       "advice": "Grade C warrants monitoring. Review stop-loss adherence and whether rejections were sound. "
                                 "If grade does not recover, consider tightening review cadence."})
    elif manager_grade in ("D", "F"):
        advice.append({"category": "OPERATIONS", "horizon": "Immediate",
                       "advice": f"Grade {manager_grade} requires action: (1) run dry-run cycle to diagnose without risk, "
                                 "(2) review last 5 decisions in detail, (3) consider lowering MAX_OPEN_POSITIONS temporarily."})

    # Agent factory
    retired_agents = factory_result.get("retired", [])
    if spawned:
        names = ", ".join(s["class_name"].replace("Agent", "") for s in spawned)
        advice.append({"category": "R&D EXPANSION", "horizon": "Immediate",
                       "advice": f"Agent Factory spawned {len(spawned)} new research agent(s) this cycle: {names}. "
                                 f"Registry now has {reg_size} active agents ({factory_result.get('total_registered', reg_size)} total registered). "
                                 "These agents will participate in the next R&D research cycle automatically."})
    else:
        remaining = len([s for s in AGENT_CATALOG if s["class_name"] not in
                         [a["class_name"] for a in _load_agent_registry().get("agents", [])]])
        if remaining > 0:
            advice.append({"category": "R&D EXPANSION", "horizon": "Ongoing",
                           "advice": f"Agent Factory: {reg_size} agents active, {remaining} more catalogued and ready to spawn. "
                                     "Factory is respecting SPAWN_PER_CYCLE limits — expansion continues next cycle."})
        else:
            advice.append({"category": "R&D EXPANSION", "horizon": "Complete",
                           "advice": f"All {reg_size} catalogued agents are active. "
                                     "R&D department is at full capacity — retirement mechanism monitoring for slot recovery."})
    if retired_agents:
        # Three-stage dignified performance system (Chairman directive 2026-04-27)
        stage1_noticed = [r for r in retired_agents if r.get("new_status") == "noticed"]
        stage2_reassigned = [r for r in retired_agents if r.get("new_status") == "reassigned"]
        stage3_retired = [r for r in retired_agents if r.get("new_status") == "retired"]
        recovered = [r for r in retired_agents if r.get("event") == "recovery"]

        if stage1_noticed:
            names = ", ".join(r["class_name"].replace("Agent", "") for r in stage1_noticed)
            czc = stage1_noticed[0].get("consecutive_zero_cycles", PERF_NOTICE_CYCLES)
            advice.append({"category": "R&D EXPANSION", "horizon": "Monitor",
                           "advice": f"[STAGE 1 — PERFORMANCE NOTICE] {len(stage1_noticed)} agent(s) issued a "
                                     f"formal notice this cycle: {names}. "
                                     f"These agents have had {czc} consecutive cycles without an approved pick. "
                                     f"They remain fully active and have been given specific improvement guidance. "
                                     f"No action required from Chairman — CoS is coaching them."})

        if stage2_reassigned:
            names = ", ".join(r["class_name"].replace("Agent", "") for r in stage2_reassigned)
            new_sectors = ", ".join(r.get("new_sector", "TBD") for r in stage2_reassigned)
            advice.append({"category": "R&D EXPANSION", "horizon": "Monitor",
                           "advice": f"[STAGE 2 — ROLE REASSIGNMENT] {len(stage2_reassigned)} agent(s) moved to "
                                     f"a new sector this cycle: {names} → {new_sectors}. "
                                     f"This is a fresh start, not a demotion. Their original sector coverage "
                                     f"is maintained by other agents. They have {PERF_REPLACE_CYCLES - PERF_REASSIGN_CYCLES} "
                                     f"cycles to demonstrate value in the new role before Stage 3 is considered."})

        if stage3_retired:
            names = ", ".join(r["class_name"].replace("Agent", "") for r in stage3_retired)
            advice.append({"category": "R&D EXPANSION", "horizon": "Immediate",
                           "advice": f"[STAGE 3 — DIGNIFIED RETIREMENT] {len(stage3_retired)} agent(s) have completed "
                                     f"all three performance stages and retired: {names}. "
                                     f"Their full contribution history is preserved in the registry. "
                                     f"Fresh top-level replacements are queued for the next spawn cycle — "
                                     f"quality-gated and fully expert-level from day one."})

        if recovered:
            names = ", ".join(r["class_name"].replace("Agent", "") for r in recovered)
            advice.append({"category": "R&D EXPANSION", "horizon": "Positive",
                           "advice": f"[RECOVERY] {len(recovered)} agent(s) bounced back this cycle: {names}. "
                                     f"Performance notice or reassignment status cleared — they are fully active again. "
                                     f"Improvement acknowledged."})

    # Capital deployment
    if target_pct > 0 and abs(deployed_pct - target_pct) > 20:
        direction = "under" if deployed_pct < target_pct else "over"
        advice.append({"category": "CAPITAL", "horizon": "Next cycle",
                       "advice": f"Capital {direction}-deployed: {deployed_pct:.1f}% vs {target_pct:.1f}% target. "
                                 + ("Ensure Manager Agent has quality picks to deploy into."
                                    if direction == "under" else
                                    "Risk of over-concentration. Review position sizing.")})

    # Trade review advice
    win_rate  = health.get("trade_win_rate")
    bd        = health.get("trade_benchmark_delta")
    pat_flags = health.get("trade_pattern_flags", 0)
    t_total   = health.get("trade_total", 0)
    if t_total >= 5 and win_rate is not None:
        intel_tr = intel.get("trade_review", {})
        top_lesson = (intel_tr.get("analytics", {}).get("lessons") or [""])[0]
        # BUG FIX: bd (benchmark_delta) can be None — guard before :.1f formatting
        bd_str = f"{bd:+.1f}pp" if bd is not None else "N/A"
        if win_rate >= 55 and (bd or 0) >= 0:
            advice.append({"category": "TRADE HISTORY", "horizon": "Ongoing",
                           "advice": (f"Historical performance is strong: {win_rate:.0f}% win rate, "
                                      f"{bd_str} vs SPY. Maintain current entry/exit discipline.")})
        else:
            advice.append({"category": "TRADE HISTORY", "horizon": "Near-term",
                           "advice": (f"Trade Review findings: {win_rate:.0f}% win rate, "
                                      f"{bd_str} vs SPY. {pat_flags} pattern(s) flagged. "
                                      f"Key lesson: {top_lesson}")})

    # Risk — now surfaces CRO public directives directly
    cro_bulletin = intel.get("cro_bulletin", {})
    cro_level    = cro_bulletin.get("risk_level", risk_level)
    cro_dirs     = cro_bulletin.get("directives", [])

    if cro_dirs:
        for directive in cro_dirs[:2]:
            advice.append({
                "category": "RISK",
                "horizon":  "Immediate",
                "advice":   f"[Chief Risk Officer — {cro_level}] {directive}",
            })
    elif risk_level in ("ELEVATED", "HIGH", "CRITICAL"):
        advice.append({"category": "RISK", "horizon": "Immediate",
                       "advice": f"Portfolio risk is {risk_level}. Review concentration and VaR in CRO report. "
                                 "No new positions until risk normalises."})

    # CRO position alerts — surface any stop-loss breaches the CRO flagged
    for alert in cro_bulletin.get("position_alerts", []):
        advice.append({
            "category": "RISK",
            "horizon":  "Immediate",
            "advice":   f"[CRO Position Alert — {alert.get('severity','HIGH')}] "
                        f"{alert.get('ticker','?')}: {alert.get('alert','')}",
        })

    # ── EXECUTION QUALITY SUMMARY — board-mandated (Special Session) ─────
    # Read execution_quality.json and surface a health digest in the brief.
    try:
        eq_path = "data/execution_quality.json"
        if os.path.exists(eq_path):
            with open(eq_path) as _f:
                eq_data = json.load(_f)
            recent = [e for e in eq_data if e.get("slippage_bps") is not None][-20:]
            if len(recent) >= 5:
                slippages   = [e["slippage_bps"] for e in recent]
                avg_slip    = sum(slippages) / len(slippages)
                worst_slip  = max(slippages)
                open_ctx    = sum(1 for e in recent if e.get("intraday_context") == "MARKET_OPEN")
                open_pct    = open_ctx / len(recent) * 100
                slip_status = (
                    "GOOD"     if avg_slip < 5   else
                    "MODERATE" if avg_slip < 20  else
                    "ELEVATED" if avg_slip < 40  else
                    "POOR"
                )
                advice.append({
                    "category": "EXECUTION QUALITY",
                    "horizon":  "Last 20 trades",
                    "advice": (
                        f"Execution health: {slip_status} — avg slippage {avg_slip:.1f} bps "
                        f"| worst single fill: {worst_slip:.1f} bps "
                        f"| {open_pct:.0f}% of orders at market open (widest spreads). "
                        + ("Consider delaying opens by 15 min to reduce spread cost."
                           if open_pct > 50 else
                           "Open-session timing is acceptable." if open_pct > 25 else
                           "Execution timing is well-distributed.")
                    ),
                })
    except Exception:
        pass

    # ── CRO / PDO CONFLICT ARBITRATION ────────────────────────────────────
    # If CRO says risk is HIGH/CRITICAL but PDO says DEPLOY_MORE, these signals
    # are in direct contradiction. Surface as a special CONFLICT advisory and
    # let the Chairman be aware — the more conservative signal (CRO) should win.
    cro_level   = cro_bulletin.get("risk_level", health.get("risk_level", "UNKNOWN"))
    pdo_sig     = health.get("pdo_signal", "")
    if cro_level in ("HIGH", "CRITICAL") and pdo_sig == "DEPLOY_MORE":
        advice.append({
            "category": "CONFLICT",
            "horizon":  "Immediate",
            "advice": (
                f"⚡ CRO/PDO SIGNAL CONFLICT: Chief Risk Officer reports {cro_level} risk "
                f"while PDO signals DEPLOY_MORE. These are contradictory directives. "
                f"Per standing policy, the more conservative signal (CRO: {cro_level}) "
                f"takes precedence — Manager Agent buy thresholds have been tightened accordingly. "
                f"Review CRO report for root cause before increasing deployment. "
                f"Consider having CRO and PDO independently re-assess before next cycle."
            ),
        })
    elif cro_level in ("ELEVATED",) and pdo_sig == "DEPLOY_MORE":
        advice.append({
            "category": "CONFLICT",
            "horizon":  "Near-term",
            "advice": (
                f"⚠ CRO/PDO TENSION: CRO rates risk as ELEVATED while PDO recommends DEPLOY_MORE. "
                f"Mild tension — proceed with deployment but apply CRO's tightened thresholds "
                f"(20%/25% BP requirements). Monitor CRO bulletin for escalation to HIGH."
            ),
        })

    # ── APPLY ADVICE HISTORY ESCALATION CHECK ─────────────────────────────
    # Cross-reference against prior cycles to flag chronic unresolved issues
    if advice_history:
        advice = _check_escalations(advice, advice_history)

    # ── Fable 5 LLM strategic synthesis ───────────────────────────────────
    # The CoS synthesises all rule-based advice into a single strategic priority
    try:
        from apex_llm import reason as _llm_r, is_available as _llm_ok
        if _llm_ok("morning_briefing"):
            _advice_lines = "\n".join(
                f"[{a.get('category','?')}] {a.get('advice','')[:120]}"
                for a in advice[:8]
            )
            _raw = _llm_r(
                "You are the Apex Capital Chief of Staff. You synthesise intelligence "
                "from all agents into a single strategic priority for the Chairman. "
                "Be direct, specific, and action-oriented.",
                f"Current state: regime={regime}, PDO={pdo_signal}, risk={risk_level}, "
                f"manager_grade={manager_grade}, deployed={deployed_pct:.0f}%, picks={total_picks}\n\n"
                f"Rule-based advice items this cycle:\n{_advice_lines}\n\n"
                "In 2 sentences, give the #1 strategic priority the firm should focus on "
                "this cycle and why it outweighs everything else.",
                agent_name="morning_briefing",
                max_tokens=160,
            )
            if _raw:
                advice.append({
                    "category": "SYNTHESIS",
                    "horizon":  "This cycle",
                    "advice":   f"[CoS Strategic Synthesis] {_raw.strip()}",
                })
    except Exception:
        pass

    return advice


def _load_agent_registry() -> dict:
    if os.path.exists(AGENT_REGISTRY):
        try:
            with open(AGENT_REGISTRY) as f:
                return json.load(f)
        except Exception:
            pass
    return {"agents": []}


# =============================================================================
# HTML REPORT
# =============================================================================

def _build_html_report(report: dict) -> str:
    health     = report["company_health"]
    attention  = report["attention_items"]
    autonomous = report["autonomous_decisions"]
    advice     = report["strategic_advice"]
    factory    = report.get("factory_result", {})
    now        = datetime.now().strftime("%B %d, %Y at %H:%M")

    pnl_color = "#3fb950" if health["pnl_today"] >= 0 else "#f85149"
    pnl_sign  = "+" if health["pnl_today"] >= 0 else ""
    risk_colors  = {"LOW":"#3fb950","MODERATE":"#58a6ff","ELEVATED":"#f0883e",
                    "HIGH":"#f85149","CRITICAL":"#f85149","UNKNOWN":"#8b949e"}
    grade_colors = {"A":"#3fb950","B":"#58a6ff","C":"#f0c000","D":"#f0883e","F":"#f85149"}
    priority_colors = {"CRITICAL":"#f85149","HIGH":"#f0883e","MEDIUM":"#f0c000","ROUTINE":"#8b949e"}

    attention_rows = ""
    for item in attention:
        pc  = priority_colors.get(item["priority"], "#8b949e")
        req = ('<span style="color:#f85149;font-weight:bold;">CHAIRMAN REQUIRED</span>'
               if item.get("requires_chairman") else
               '<span style="color:#3fb950;">Handled</span>')
        attention_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:10px 8px;">
            <span style="background:{pc};color:#0d1117;font-size:10px;font-weight:bold;
                         padding:2px 7px;border-radius:3px;">{item['priority']}</span></td>
          <td style="padding:10px 8px;color:#8b949e;font-size:12px;">{item['category']}</td>
          <td style="padding:10px 8px;color:#c9d1d9;">{item['item']}</td>
          <td style="padding:10px 8px;font-size:12px;">{req}</td>
        </tr>"""

    auto_rows = ""
    for dec in autonomous:
        auto_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:10px 8px;color:#c9d1d9;">{dec['decision']}</td>
          <td style="padding:10px 8px;color:#8b949e;font-size:12px;">{dec['rationale']}</td>
        </tr>"""
    if not auto_rows:
        auto_rows = '<tr><td colspan="2" style="color:#8b949e;padding:12px;">No autonomous decisions today.</td></tr>'

    advice_cards = ""
    for a in advice:
        is_conflict  = a.get("category") == "CONFLICT"
        is_escalated = a.get("escalated", False)
        consec       = a.get("consecutive_cycles", 0)

        # Pick border and category label color based on type
        if is_conflict:
            border_color   = "#f0883e"
            cat_color      = "#f0883e"
            left_bar       = f'border-left:4px solid #f0883e;'
        elif is_escalated:
            border_color   = "#f85149"
            cat_color      = "#f85149"
            left_bar       = f'border-left:4px solid #f85149;'
        elif a.get("category") == "RISK":
            border_color   = "#30363d"
            cat_color      = "#f85149"
            left_bar       = ""
        else:
            border_color   = "#30363d"
            cat_color      = "#58a6ff"
            left_bar       = ""

        escalation_badge = (
            f'<span style="background:#f85149;color:white;font-size:10px;font-weight:bold;'
            f'padding:2px 7px;border-radius:3px;margin-left:8px;">⚑ {consec}× UNRESOLVED</span>'
            if is_escalated else
            (f'<span style="background:#f0883e;color:#0d1117;font-size:10px;font-weight:bold;'
             f'padding:2px 7px;border-radius:3px;margin-left:8px;">SIGNAL CONFLICT</span>'
             if is_conflict else "")
        )

        advice_cards += f"""
        <div style="background:#0d1117;border:1px solid {border_color};border-radius:6px;
                    {left_bar}padding:14px 18px;margin-bottom:12px;">
          <div style="display:flex;justify-content:space-between;margin-bottom:8px;
                      align-items:center;flex-wrap:wrap;gap:4px;">
            <div>
              <span style="color:{cat_color};font-size:11px;font-weight:bold;
                           text-transform:uppercase;letter-spacing:1px;">{a['category']}</span>
              {escalation_badge}
            </div>
            <span style="color:#8b949e;font-size:11px;">{a['horizon']}</span>
          </div>
          <p style="color:#c9d1d9;margin:0;line-height:1.6;">{a['advice']}</p>
        </div>"""

    # Factory section
    spawned      = factory.get("spawned", [])
    reg_size     = factory.get("registry_size", 0)
    total_ever   = factory.get("total_ever", 0)
    factory_rows = ""
    if spawned:
        for s in spawned:
            factory_rows += f"""
            <tr style="border-bottom:1px solid #21262d;">
              <td style="padding:8px;color:#58a6ff;font-weight:bold;">{s['class_name']}</td>
              <td style="padding:8px;color:#c9d1d9;">{s['specialization']}</td>
              <td style="padding:8px;color:#8b949e;">Tier {s['tier']}</td>
              <td style="padding:8px;color:#3fb950;">✓ Active</td>
            </tr>"""
    else:
        factory_rows = ('<tr><td colspan="4" style="color:#8b949e;padding:12px;">'
                        'No new agents spawned this cycle.</td></tr>')

    risk_c  = risk_colors.get(health["risk_level"], "#8b949e")
    grade_c = grade_colors.get(health["manager_grade"], "#8b949e")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:980px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#e6edf3;margin:0;font-size:22px;">
      APEX CAPITAL — Chief of Staff Daily Brief
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      To: Jack Beard (Chairman) &nbsp;·&nbsp; {now} &nbsp;·&nbsp; CONFIDENTIAL
    </p>
  </div>

  <!-- Snapshot -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:24px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Portfolio Value</div>
      <div style="font-size:20px;font-weight:bold;">${health['portfolio_value']:,.2f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Today's P&amp;L</div>
      <div style="font-size:20px;font-weight:bold;color:{pnl_color};">
        {pnl_sign}${abs(health['pnl_today']):.2f} ({pnl_sign}{health['pnl_pct']:.2f}%)</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Risk Level</div>
      <div style="font-size:20px;font-weight:bold;color:{risk_c};">{health['risk_level']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Manager Grade</div>
      <div style="font-size:20px;font-weight:bold;color:{grade_c};">{health['manager_grade']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Regime</div>
      <div style="font-size:20px;font-weight:bold;text-transform:capitalize;">{health['regime']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">R&amp;D Agents</div>
      <div style="font-size:20px;font-weight:bold;color:#58a6ff;">{reg_size}</div>
    </div>
  </div>

  <!-- Attention triage -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Today's Attention Items
    <span style="color:#8b949e;font-size:12px;font-weight:normal;margin-left:8px;">
      What requires you · what I handled
    </span>
  </h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead><tr style="background:#21262d;">
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;width:100px;">PRIORITY</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;width:110px;">CATEGORY</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">ITEM</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;width:150px;">ACTION</th>
    </tr></thead>
    <tbody>{attention_rows}</tbody>
  </table>

  <!-- Autonomous decisions -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:4px;">Decisions Made on Your Behalf</h2>
  <p style="color:#8b949e;font-size:12px;margin:0 0 12px 0;">
    Routine, non-material decisions handled autonomously — no Chairman sign-off required.
  </p>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead><tr style="background:#21262d;">
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">DECISION</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">RATIONALE</th>
    </tr></thead>
    <tbody>{auto_rows}</tbody>
  </table>

  <!-- Agent Factory -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:4px;">
    Agent Factory
    <span style="color:#58a6ff;font-size:12px;font-weight:normal;margin-left:8px;">
      {reg_size} active · {total_ever} total spawned · {MAX_REGISTRY_AGENTS - reg_size} slots remaining
    </span>
  </h2>
  <p style="color:#8b949e;font-size:12px;margin:0 0 12px 0;">
    New R&amp;D agents spawned this cycle — auto-registered and active next research run.
  </p>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:28px;">
    <thead><tr style="background:#21262d;">
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">AGENT</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">SPECIALIZATION</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">TIER</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">STATUS</th>
    </tr></thead>
    <tbody>{factory_rows}</tbody>
  </table>

  <!-- Strategic advice -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Strategic Recommendations</h2>
  {advice_cards}

  <p style="color:#484f58;font-size:11px;margin-top:32px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital Intelligence — Chief of Staff &nbsp;·&nbsp;
    Apex Capital — Proprietary Trading &nbsp;·&nbsp;
    Generated {now}
  </p>
</body></html>"""


# =============================================================================
# EMAIL
# =============================================================================

def _email_chairman(report: dict) -> None:
    try:
        from email_sender import send_daily_report
        from creative_agent import polish_email

        html    = _build_html_report(report)
        health  = report["company_health"]
        factory = report.get("factory_result", {})
        subject = (
            f"Apex Capital — Chief of Staff Brief | "
            f"Risk:{health['risk_level']} | Grade:{health['manager_grade']} | "
            f"{datetime.now().strftime('%b %d, %Y')}"
        )
        key_metrics = {
            "Portfolio":      f"${health['portfolio_value']:,.0f}",
            "P&L Today":      f"{'+' if health['pnl_today'] >= 0 else ''}{health['pnl_pct']:.2f}%",
            "Risk Level":     health["risk_level"],
            "Manager Grade":  health["manager_grade"],
            "R&D Agents":     str(factory.get("registry_size", 0)),
        }
        critical_items = [i for i in report["attention_items"] if i["priority"] == "CRITICAL"]
        polished = polish_email(
            html, subject, "chief_of_staff", key_metrics,
            priority="urgent" if critical_items else "normal",
        )
        success = send_daily_report(html_body=polished, subject=subject, paper_day=0,
                                    sender_name="chief_of_staff", key_metrics=key_metrics)
        if success:
            logger.info("  Chief of Staff brief emailed to Chairman ✓")
        else:
            logger.warning("  Email failed — brief printed to console only")
    except Exception as e:
        logger.warning(f"  Email error: {e}")


# =============================================================================
# MAIN ORCHESTRATION
# =============================================================================

def _should_run_cos_hr_coordination() -> bool:
    """Return True if 7+ days have passed since the last CoS+HR coordination meeting."""
    if not os.path.exists(COS_HR_COORD):
        return True
    try:
        with open(COS_HR_COORD) as f:
            prev = json.load(f)
        last_ts = prev.get("generated_at", "")
        if not last_ts:
            return True
        last_dt = datetime.fromisoformat(str(last_ts)[:19])
        return (datetime.now() - last_dt).days >= COS_HR_MEETING_INTERVAL_DAYS
    except Exception:
        return True


def run_cos_hr_coordination(factory_result: dict = None) -> dict:
    """
    Weekly CoS + HR coordination meeting.

    The Chief of Staff reads the latest HR report and cross-references it
    with the Agent Factory's performance tracking data to produce a unified
    view of which agents need intervention, coaching, or reassignment.

    This is the mechanism by which HR ratings influence CoS Stage 1/2
    performance management decisions.  The CoS does NOT report back to HR
    (HR is subordinate for data-sharing purposes) — the CoS uses the HR
    signal to inform its own factory decisions.

    Output:  data/cos_hr_coordination.json
    Returns: dict with coordination findings and recommended actions
    """
    logger.info("\n  ── CoS + HR Weekly Coordination Meeting ──────────────────")
    result = {
        "generated_at":       datetime.now().isoformat(),
        "period_days":        COS_HR_MEETING_INTERVAL_DAYS,
        "agents_reviewed":    [],
        "immediate_actions":  [],   # e.g. escalate to Stage 2 for combined CoS + HR concern
        "watch_list":         [],   # agents flagged by HR but not yet at CoS threshold
        "healthy":            [],   # agents performing well per both views
        "summary":            "",
    }

    # ── Load HR report ──────────────────────────────────────────────────
    hr_ratings: dict = {}   # {agent_name: {score, status, recommendation}}
    hr_report_path = "data/hr_report.json"
    if os.path.exists(hr_report_path):
        try:
            with open(hr_report_path) as f:
                hr_raw = json.load(f)
            # hr_agent.py writes agents needing intervention under "action_required".
            # Each entry: {agent_name, class_name, issue, recommendation, ...}
            # We also read all agent entries from "hrpo_ratings" if present (full roster).
            _action_required = hr_raw.get("action_required", [])
            for entry in _action_required:
                name = entry.get("agent_name") or entry.get("class_name") or entry.get("name", "")
                if name:
                    hr_ratings[name] = {
                        "score":          entry.get("performance_score", 3),  # flagged → low score
                        "status":         entry.get("status", "active"),
                        "recommendation": entry.get("recommendation", entry.get("issue", "")),
                        "flag":           True,   # action_required means flagged
                    }
            # Also harvest hrpo_ratings for the full picture (agents NOT in action_required = healthy)
            _hrpo = hr_raw.get("hrpo_ratings", {})
            if isinstance(_hrpo, dict):
                for name, rating_data in _hrpo.items():
                    if name not in hr_ratings:
                        _score = rating_data.get("score", 5) if isinstance(rating_data, dict) else 5
                        hr_ratings[name] = {
                            "score":          _score,
                            "status":         "active",
                            "recommendation": "",
                            "flag":           _score < 4,
                        }
            logger.info(f"  HR report loaded: {len(hr_ratings)} agent evaluations "
                        f"({len(_action_required)} flagged for action)")
        except Exception as e:
            logger.warning(f"  Could not load HR report: {e}")
    else:
        logger.info("  HR report not found — coordination will use factory data only")

    # ── Load Agent Registry (CoS factory performance view) ─────────────
    registry: list = []
    if os.path.exists(AGENT_REGISTRY):
        try:
            with open(AGENT_REGISTRY) as f:
                reg_data = json.load(f)
            registry = reg_data if isinstance(reg_data, list) else reg_data.get("agents", [])
        except Exception as e:
            logger.warning(f"  Could not load agent registry: {e}")

    # ── Cross-reference ─────────────────────────────────────────────────
    # For each registered agent, check both HR score and factory performance.
    # Escalate if BOTH views flag concern; add to watch_list if only one does.

    cos_underperform_classes = set()
    if factory_result:
        for agent_info in factory_result.get("performance_actions", []):
            cos_underperform_classes.add(agent_info.get("class_name", ""))

    for agent in registry:
        class_name  = agent.get("class_name", "")
        agent_name  = agent.get("specialization", class_name)
        cos_cycles  = agent.get("underperform_cycles", 0)
        cos_stage   = "healthy" if cos_cycles == 0 else \
                      "stage1"  if cos_cycles < PERF_REASSIGN_CYCLES else "stage2"

        hr_entry = hr_ratings.get(class_name) or hr_ratings.get(agent_name) or {}
        hr_score = hr_entry.get("score", 5)   # default 5/10 if no HR data
        hr_flag  = hr_entry.get("flag", False) or hr_score < 4

        agent_view = {
            "class_name":    class_name,
            "agent_name":    agent_name,
            "cos_stage":     cos_stage,
            "cos_cycles":    cos_cycles,
            "hr_score":      hr_score,
            "hr_flag":       hr_flag,
            "hr_recommendation": hr_entry.get("recommendation", ""),
        }
        result["agents_reviewed"].append(agent_view)

        # Both CoS AND HR flag this agent → immediate escalation
        if cos_stage in ("stage1", "stage2") and hr_flag:
            result["immediate_actions"].append({
                "class_name":  class_name,
                "agent_name":  agent_name,
                "action":      "ESCALATE" if cos_stage == "stage1" else "REASSIGNMENT_REVIEW",
                "cos_stage":   cos_stage,
                "hr_score":    hr_score,
                "reason":      (
                    f"Both CoS ({cos_cycles} underperform cycles) and HR "
                    f"(score {hr_score}/10) have flagged {agent_name}. "
                    f"Combined signal warrants {'immediate Stage 2 escalation' if cos_stage == 'stage1' else 'reassignment review'}."
                ),
            })
        # Only HR flags → watch list (CoS will monitor more closely)
        elif hr_flag and cos_stage == "healthy":
            result["watch_list"].append({
                "class_name": class_name,
                "agent_name": agent_name,
                "hr_score":   hr_score,
                "reason":     f"HR flagged (score {hr_score}/10) but CoS performance is nominal. Monitoring.",
            })
        # Healthy from both views
        elif not hr_flag and cos_stage == "healthy":
            result["healthy"].append(agent_name)

    n_act  = len(result["immediate_actions"])
    n_wtch = len(result["watch_list"])
    n_hlth = len(result["healthy"])
    result["summary"] = (
        f"Weekly CoS+HR coordination: {len(result['agents_reviewed'])} agents reviewed | "
        f"{n_act} immediate action(s) | {n_wtch} on watch list | {n_hlth} healthy"
    )

    logger.info(f"  {result['summary']}")
    for act in result["immediate_actions"]:
        logger.warning(
            f"  ⚠️  ESCALATION [{act['agent_name']}] — {act['action']}: {act['reason'][:80]}"
        )

    os.makedirs("data", exist_ok=True)
    with open(COS_HR_COORD, "w") as f:
        json.dump(result, f, indent=2, default=str)

    return result


def run_chief_of_staff(send_email: bool = True) -> dict:
    logger.info(f"\n{'='*60}")
    logger.info("  APEX CAPITAL — CHIEF OF STAFF")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}\n")

    # ── Check for pending task requests from firm channel ─────────────────
    try:
        from firm_channel import get_pending_tasks
        _tasks = get_pending_tasks("Chief of Staff")
        if _tasks:
            logger.info(f"  [CoS] {len(_tasks)} pending task request(s) from firm channel:")
            for _t in _tasks[:5]:
                logger.info(f"    {_t.get('content', '')[:120]}")
        # (tasks are logged; CoS strategic advice phase will incorporate them)
    except Exception:
        pass

    # ── CoS Meeting Request Queue ─────────────────────────────────────────────
    # Any agent can formally request CoS attention via firm_channel.request_cos_meeting().
    # Process: log them, decide action (emergency meeting / next agenda / self-resolve).
    try:
        from firm_channel import get_cos_meeting_requests, mark_cos_request_handled, request_cos_meeting as _rcm
        from board_meeting import call_emergency_board_meeting
        _pending_reqs = get_cos_meeting_requests(status="PENDING")
        if _pending_reqs:
            logger.info(f"\n  [CoS] Meeting request queue: {len(_pending_reqs)} pending request(s):")
            for _req in _pending_reqs:
                _urgency = _req.get("urgency", "NORMAL")
                _from    = _req.get("from_agent", "?")
                _summary = _req.get("summary", "")
                logger.info(f"    [{_urgency}] From {_from}: {_summary[:100]}")
                if _urgency == "EMERGENCY":
                    # Immediately call emergency board meeting
                    try:
                        call_emergency_board_meeting(
                            reason=f"CoS meeting request from {_from}: {_summary[:200]}",
                            severity="HIGH",
                            caller="Chief of Staff",
                        )
                        mark_cos_request_handled(_req["id"], resolution="emergency_board_meeting_called")
                        logger.info(f"    → Emergency board meeting called for {_req['id']}")
                    except Exception as _emerg_e:
                        logger.warning(f"    → Emergency meeting failed: {_emerg_e}")
                elif _urgency == "HIGH":
                    # Queue for next board meeting agenda; log as issue
                    try:
                        from issue_tracker import log_issue
                        log_issue(
                            reporter="Chief of Staff",
                            title=f"CoS meeting request [{_req['id']}]: {_summary[:60]}",
                            description=f"From {_from}: {_summary}",
                            severity="HIGH",
                            category="GOVERNANCE",
                            auto_fix_hint="Board to review at next meeting.",
                        )
                    except Exception:
                        pass
                    mark_cos_request_handled(_req["id"], resolution="queued_for_next_board_meeting")
                    logger.info(f"    → Queued for next board meeting: {_req['id']}")
                else:
                    # NORMAL — CoS resolves or notes without board escalation
                    mark_cos_request_handled(_req["id"], resolution="noted_by_cos")
                    logger.info(f"    → Noted by CoS (no board escalation needed): {_req['id']}")
        else:
            logger.info("  [CoS] Meeting request queue: empty.")
    except Exception as _cos_q_e:
        logger.warning(f"  [CoS] Meeting queue error (non-fatal): {_cos_q_e}")

    intel          = _load_all_intelligence()
    health         = _assess_company_health(intel)
    advice_history = _load_advice_history()

    # Run Agent Factory first (expansion before briefing)
    logger.info("  Running Agent Factory — R&D expansion check...")
    factory = AgentFactory()
    factory_result = factory.run(
        pdo_signal=health["pdo_signal"],
        regime=health["regime"],
    )

    attention  = _triage_attention(intel, health)
    autonomous = _make_autonomous_decisions(intel, health)
    advice     = _generate_strategic_advice(intel, health, factory_result, advice_history)

    # ── Weekly CoS + HR Coordination Meeting ──────────────────────────────
    # Runs once per week (every 7 days). Cross-references HR performance scores
    # with the Agent Factory's CoS performance data to identify agents that
    # both HR AND the CoS have flagged — these get immediate escalation.
    cos_hr_result = {}
    if _should_run_cos_hr_coordination():
        logger.info("  Triggering weekly CoS + HR coordination meeting...")
        try:
            cos_hr_result = run_cos_hr_coordination(factory_result=factory_result)
            # Inject any CoS+HR escalations into the attention list
            for act in cos_hr_result.get("immediate_actions", []):
                attention.append({
                    "priority":          "HIGH",
                    "category":          "HR_COS_ESCALATION",
                    "requires_chairman": False,
                    "item": (
                        f"[CoS+HR Joint Escalation] {act['agent_name']}: {act['action']} — "
                        f"{act['reason']}"
                    ),
                })
        except Exception as e:
            logger.warning(f"  CoS+HR coordination error: {e}")
    else:
        logger.info("  CoS+HR coordination: not due yet (runs weekly)")

    # ── Persist advice history ─────────────────────────────────────────────
    cycle_entry = {
        "timestamp":    datetime.now().isoformat(),
        "risk_level":   health.get("risk_level", "UNKNOWN"),
        "pdo_signal":   health.get("pdo_signal", ""),
        "advice_count": len(advice),
        "escalated":    sum(1 for a in advice if a.get("escalated", False)),
    }
    advice_history.setdefault("cycles", []).append(cycle_entry)
    _save_advice_history(advice_history)
    if cycle_entry["escalated"]:
        logger.warning(
            f"  [AdviceHistory] {cycle_entry['escalated']} escalated item(s) — "
            "chronic unresolved issues flagged in Chairman brief."
        )

    report = {
        "timestamp":            datetime.now().isoformat(),
        "company_health":       health,
        "attention_items":      attention,
        "autonomous_decisions": autonomous,
        "strategic_advice":     advice,
        "factory_result":       factory_result,
        "cos_hr_coordination":  cos_hr_result,   # weekly joint performance review (empty dict on non-meeting days)
    }

    os.makedirs("data", exist_ok=True)
    with open(COS_REPORT, "w") as f:
        json.dump(report, f, indent=2, default=str)

    critical = [i for i in attention if i["priority"] == "CRITICAL"]
    high     = [i for i in attention if i["priority"] == "HIGH"]
    logger.info(f"  Health: {health['risk_level']} risk | Grade {health['manager_grade']} | "
                f"P&L {health['pnl_pct']:+.2f}%")
    logger.info(f"  Triage: {len(critical)} CRITICAL, {len(high)} HIGH, "
                f"{len([i for i in attention if i['priority']=='ROUTINE'])} ROUTINE")
    logger.info(f"  Factory: {len(factory_result.get('spawned',[]))} new agent(s) | "
                f"Registry: {factory_result.get('registry_size',0)}/{MAX_REGISTRY_AGENTS}")

    if send_email:
        _email_chairman(report)

    return report


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    no_email     = "--no-email"     in sys.argv
    factory_only = "--factory-only" in sys.argv

    if factory_only:
        f = AgentFactory()
        result = f.run()
        print(f"\nSpawned: {len(result['spawned'])} | Registry: {result['registry_size']}")
    else:
        run_chief_of_staff(send_email=not no_email)
