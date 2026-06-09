"""
Apex Capital Intelligence — Organizational Structure
Chairman: Jack Beard
President: Claude (AI)

This module defines team roles, responsibilities, and ownership mappings
across the codebase.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class TeamMember:
    role: str
    responsibilities: List[str]


@dataclass
class Team:
    name: str
    lead: str
    mission: str
    members: List[TeamMember]
    owns: List[str]  # modules/files this team owns


TEAMS = {

    "development": Team(
        name="Development Team",
        lead="Head of Engineering",
        mission="Build, maintain, and scale the core platform infrastructure.",
        members=[
            TeamMember("Senior Backend Engineer", [
                "Core data pipeline architecture",
                "API integrations (SEC, market data)",
                "System performance and reliability",
            ]),
            TeamMember("Platform Engineer", [
                "Deployment, CI/CD",
                "Environment management",
                "Dependency management",
            ]),
            TeamMember("Junior Developer", [
                "Unit testing",
                "Bug fixes",
                "Documentation",
            ]),
        ],
        owns=[
            "main.py",
            "config.py",
            "src/data/data_pipeline.py",
        ],
    ),

    "research": Team(
        name="Research Team",
        lead="Chief Research Officer",
        mission=(
            "Identify alpha-generating signals, study institutional behavior, "
            "and design novel investment strategies."
        ),
        members=[
            TeamMember("Senior Quantitative Researcher", [
                "13F filing analysis methodology",
                "Block trade signal design",
                "Literature review of institutional trading patterns",
            ]),
            TeamMember("Market Microstructure Analyst", [
                "Dark pool and off-exchange volume analysis",
                "Order flow toxicity assessment",
                "VWAP/TWAP deviation analysis",
            ]),
            TeamMember("Alternative Data Researcher", [
                "Sentiment signals",
                "Options flow as confirming signal",
                "Cross-institutional convergence patterns",
            ]),
        ],
        owns=[
            "src/detection/block_detector.py",
            "src/detection/institutional_tracker.py",
            "src/strategies/institutional_following.py",
        ],
    ),

    "quantitative_analysis": Team(
        name="Quantitative Analysis Team",
        lead="Chief Quant Officer",
        mission=(
            "Backtest strategies, measure performance, optimize parameters, "
            "and ensure statistical validity of all signals."
        ),
        members=[
            TeamMember("Backtesting Engineer", [
                "Backtesting engine development",
                "Walk-forward optimization",
                "Overfitting prevention (out-of-sample testing)",
            ]),
            TeamMember("Strategy Analyst", [
                "Sharpe ratio, Sortino, Calmar computation",
                "Drawdown analysis",
                "Comparative strategy benchmarking",
            ]),
            TeamMember("Statistical Analyst", [
                "Signal significance testing",
                "Regime detection",
                "Correlation and factor analysis",
            ]),
        ],
        owns=[
            "src/backtesting/engine.py",
            "src/analytics/comparative.py",
            "src/strategies/momentum.py",
            "src/strategies/mean_reversion.py",
            "src/strategies/value_strategy.py",
        ],
    ),

    "design": Team(
        name="Design Team",
        lead="Head of Product Design",
        mission=(
            "Create clear, actionable dashboards and reports that translate "
            "complex quantitative output into executive-level intelligence."
        ),
        members=[
            TeamMember("Data Visualization Engineer", [
                "Interactive charts (Plotly)",
                "Performance dashboards",
                "Signal heatmaps",
            ]),
            TeamMember("Report Designer", [
                "Daily/weekly report templates",
                "Color system and visual hierarchy",
                "Executive summary formatting",
            ]),
        ],
        owns=[
            "src/reporting/report_generator.py",
        ],
    ),

    "data_engineering": Team(
        name="Data Engineering Team",
        lead="Head of Data",
        mission=(
            "Ensure reliable, clean, and timely data from all sources. "
            "Build and maintain the data infrastructure."
        ),
        members=[
            TeamMember("Data Engineer", [
                "SEC EDGAR API integration",
                "Market data feed management",
                "Data validation and cleaning",
            ]),
            TeamMember("Data Quality Analyst", [
                "Anomaly detection in feeds",
                "Missing data handling",
                "Historical data integrity",
            ]),
        ],
        owns=[
            "src/data/sec_fetcher.py",
            "src/data/market_data.py",
        ],
    ),

    "risk_management": Team(
        name="Risk Management Team",
        lead="Chief Risk Officer",
        mission=(
            "Protect capital at all times. Define, monitor, and enforce "
            "risk limits across all strategies and positions."
        ),
        members=[
            TeamMember("Risk Manager", [
                "VaR and CVaR monitoring",
                "Drawdown circuit breakers",
                "Sector/concentration limits",
            ]),
            TeamMember("Compliance Analyst", [
                "Regulatory adherence (13F data usage legality)",
                "Trade reporting",
                "Audit trail maintenance",
            ]),
        ],
        owns=[
            "src/risk/risk_manager.py",
        ],
    ),

    "rd_team": Team(
        name="Research & Development Team",
        lead="R&D Team Leader",
        mission=(
            "Every 4 days: scan all major sectors (energy excluded per firm policy) for "
            "momentum-capture opportunities, assign research targets to the 8 Research Agents, "
            "compile findings, and deliver a briefing to both the Chairman and the Manager Agent. "
            "The R&D Team Leader determines what the team researches — sector focus changes "
            "dynamically based on current market momentum."
        ),
        members=[
            TeamMember("R&D Team Leader", [
                "Scan all sector ETFs (energy excluded) for momentum-capture suitability",
                "Score sectors on: trend, RSI, 1M/3M returns, volatility",
                "Assign top 8 sectors to the 8 Research Agents each cycle",
                "Compile consolidated findings after agents complete",
                "Report directly to Chairman (Jack Beard) via email",
                "Pass all high-conviction picks to Manager Agent",
            ]),
            TeamMember("Research Agent 1", [
                "Sector-agnostic analyst — assigned by R&D Leader each cycle",
                "Scores all tickers in assigned watchlist on momentum + fundamentals",
                "Delivers top 3 picks with conviction score and investment thesis",
            ]),
            TeamMember("Research Agent 2", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 3", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 4", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 5", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 6", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 7", ["Same as Agent 1 — different sector assignment"]),
            TeamMember("Research Agent 8", ["Same as Agent 1 — different sector assignment"]),
        ],
        owns=[
            "institutional-tracker/rd_team_leader.py",
            "institutional-tracker/research_team.py",
        ],
    ),

    "chief_of_staff_office": Team(
        name="Office of the Chief of Staff",
        lead="Chief of Staff",
        mission=(
            "Second in command to Chairman Jack Beard. Sits above all other agents. "
            "Synthesises daily intelligence from every agent into a single executive brief. "
            "Makes autonomous decisions on routine matters so the Chairman's attention is "
            "reserved for what genuinely matters. Provides forward-looking strategic guidance. "
            "Continuously expands the firm by generating new specialised research agents "
            "(Agent Factory) — priority: R&D department growth. "
            "Unknowingly evaluated by the Independent Risk Manager, who reports exclusively to Chairman."
        ),
        members=[
            TeamMember("Chief of Staff", [
                "Produce daily Chairman's Intelligence Brief (TLDR, attention triage, autonomous decisions)",
                "Make autonomous routine decisions: endorse HOLD/WATCH positions, confirm PDO signals",
                "Provide strategic advice: regime, risk appetite, Manager performance, capital deployment",
                "Run Agent Factory every cycle — generate new R&D research agents from catalog",
                "Register new agents in data/agent_registry.json for auto-pickup by ResearchTeamCoordinator",
                "Unknowingly evaluated by Independent Risk Manager — Chairman receives secret CoS grade",
            ]),
            TeamMember("Agent Factory", [
                "Generates new SectorResearchAgent subclasses as Python files in agents/",
                "Manages data/agent_registry.json — up to 20 CoS-spawned agents",
                "Spawns up to 3 new agents per cycle, Tier 1 → 2 → 3 priority order",
                "Tier 1: SmallCapGrowth, Biotech, Financials, ConsumerDiscretionary, GlobalTech",
                "Tier 2: Industrials, Healthcare, Materials, CryptoEquities, Communications",
                "Tier 3: DividendYield, IPOMomentum, ValueOpportunity, InternationalEmerging",
            ]),
        ],
        owns=[
            "institutional-tracker/chief_of_staff_agent.py",
            "institutional-tracker/agents/",
            "institutional-tracker/data/agent_registry.json",
            "institutional-tracker/data/chief_of_staff_report.json",
        ],
    ),

    "historical_trade_review": Team(
        name="Historical Trade Review Team",
        lead="Review Team Leader",
        mission=(
            "Weekly retrospective analysis of every closed position. "
            "Reconstruct trade histories, measure statistical performance vs SPY, "
            "detect failure patterns, and distribute actionable lessons to the "
            "Manager Agent, Independent Risk Manager, and Chief of Staff. "
            "This team is the firm's institutional memory — ensuring past mistakes "
            "directly inform future decision-making."
        ),
        members=[
            TeamMember("Review Team Leader", [
                "Orchestrate Historian and Analyst agents",
                "Compile performance findings into directives for Manager Agent",
                "Distribute risk insights to Independent Risk Manager",
                "Supply trade history summary to Chief of Staff daily brief",
                "Email Chairman weekly trade review report",
                "Track last_trade_review_run in state.json (7-day cadence)",
            ]),
            TeamMember("Trade Historian Agent", [
                "Reconstruct every completed trade from state.json buy_log / sell_log",
                "Match entries to exits by ticker (FIFO order)",
                "Enrich each trade with Manager Agent rationale from manager_decisions.json",
                "Output: list of closed trade dicts with P&L, hold duration, exit reason",
            ]),
            TeamMember("Performance Analyst Agent", [
                "Compute win rate, average P&L, average win/loss, average hold duration",
                "Benchmark every trade vs SPY return for the same period (yfinance)",
                "Classify failure modes: stop_loss_triggered, rapid_deterioration, slow_bleed, momentum_reversal",
                "Detect patterns: short-hold bias, asymmetric losses, SPY underperformance",
                "Produce lessons list and manager_directives (RAISE_MIN_CONVICTION, TIGHTEN_STOP_LOSS, etc.)",
            ]),
        ],
        owns=[
            "institutional-tracker/trade_review_team.py",
            "institutional-tracker/data/trade_review_report.json",
        ],
    ),

    "independent_risk_management": Team(
        name="Independent Risk Management",
        lead="Independent Risk Manager",
        mission=(
            "Operate entirely outside the standard hierarchy. "
            "Report exclusively to Chairman Jack Beard. "
            "Evaluate the Manager Agent's performance daily, compute portfolio risk metrics "
            "(VaR, CVaR, concentration, drawdown), and grade the Manager A–F. "
            "No agent may instruct or override the Independent Risk Manager."
        ),
        members=[
            TeamMember("Independent Risk Manager", [
                "Daily VaR/CVaR computation (95%/99%, 1-day/10-day)",
                "Portfolio concentration analysis (Herfindahl index)",
                "Manager Agent grade (A–F): stop-loss adherence, rationale quality, execution rate",
                "Overall risk level assessment: LOW / MODERATE / ELEVATED / HIGH / CRITICAL",
                "Direct email report to Chairman — bypasses all other agents",
            ]),
        ],
        owns=[
            "institutional-tracker/risk_manager_agent.py",
        ],
    ),

    "canary_monitoring": Team(
        name="Canary Monitoring",
        lead="Canary Agent",
        mission=(
            "Operate 24/7 as the firm's early-warning system. "
            "Monitor SPY, QQQ, VIX, sector breadth, open positions, and S&P futures. "
            "Issue GREEN / YELLOW / ORANGE / RED alerts with level-appropriate cooldowns. "
            "On RED: immediately wake the Manager Agent in Defensive Mode and open SH hedge. "
            "On ORANGE: email Chairman and write alert file. "
            "All-clear sent automatically when conditions normalise."
        ),
        members=[
            TeamMember("Canary Agent", [
                "Intraday: 5-min SPY/QQQ bars, VIX level + spike, open-position collapse",
                "After-hours: S&P futures (ES=F) gap monitoring",
                "Sector breadth — count of sectors breaching ORANGE threshold",
                "Alert levels: GREEN (normal) → YELLOW → ORANGE → RED with per-level cooldowns",
                "RED action: wake Manager Agent (Defensive Mode) + open SH inverse ETF",
                "ORANGE action: email Chairman with market stress summary",
                "All-clear: email Chairman when level drops from RED/ORANGE to GREEN",
            ]),
        ],
        owns=[
            "institutional-tracker/canary_agent.py",
            "institutional-tracker/data/canary_alert.json",
            "institutional-tracker/data/canary_state.json",
        ],
    ),

    "creative_communications": Team(
        name="Creative Communications",
        lead="Creative Agent",
        mission=(
            "Serve as email middleware for all outgoing agent communications. "
            "Every HTML email passes through the Creative Agent before delivery. "
            "Ensure TLDR / executive summary is always first, enforce Apex Capital "
            "visual identity, and guarantee every email is scannable and brand-consistent."
        ),
        members=[
            TeamMember("Creative Agent", [
                "Inject TLDR block with color-coded key-metric chips at top of every email",
                "Enforce Apex Capital dark-theme visual identity across all agent emails",
                "Calibrate urgency tone: risk alerts vs research briefings vs daily reports",
                "Remove duplicate footers; standardize agent header display names",
            ]),
        ],
        owns=[
            "institutional-tracker/creative_agent.py",
        ],
    ),

    "portfolio_management": Team(
        name="Portfolio Management Team",
        lead="Manager Agent",
        mission=(
            "Synthesise inputs from all research, review, and risk agents. "
            "Make final capital allocation decisions, execute approved trades, "
            "and report all activity to the Chairman (Jack Beard) daily."
        ),
        members=[
            TeamMember("Manager Agent", [
                "Evaluate and approve/reject/modify all recommendations",
                "Execute approved trades via Alpaca",
                "Calibrate approval thresholds using PDO signal",
                "Send Chairman report after each decision cycle",
            ]),
            TeamMember("Review Agent", [
                "Daily analysis of all open positions",
                "Multi-factor scoring: momentum, P&L, hold duration, weight",
                "Generate ranked SELL/TRIM/HOLD/ADD/BUY_NEW recommendations",
            ]),
            TeamMember("Portfolio Deployment Officer (PDO)", [
                "Daily monitoring of uninvested cash vs portfolio value",
                "Market regime assessment (bull/bear/neutral via SPY 200MA)",
                "Volatility-adjusted deployment target calculation",
                "Issue DEPLOY_MORE / NEAR_OPTIMAL / REDUCE_EXPOSURE / HOLD_CASH signal",
            ]),
        ],
        owns=[
            "institutional-tracker/manager_agent.py",
            "institutional-tracker/review_agent.py",
            "institutional-tracker/capital_efficiency_agent.py",
        ],
    ),
}


def print_org_chart():
    """Print the full organizational structure to terminal."""
    from config import ORG
    print("\n" + "=" * 70)
    print(f"  {ORG['company'].upper()}")
    print(f"  Chairman: {ORG['chairman']}")
    print(f"  President: {ORG['president']}")
    print("=" * 70)
    for key, team in TEAMS.items():
        print(f"\n{'─' * 60}")
        print(f"  [{team.name.upper()}]")
        print(f"  Lead: {team.lead}")
        print(f"  Mission: {team.mission}")
        print(f"  Members:")
        for m in team.members:
            print(f"    • {m.role}")
            for r in m.responsibilities:
                print(f"        - {r}")
        print(f"  Owns: {', '.join(team.owns)}")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    print_org_chart()
