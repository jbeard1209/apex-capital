#!/usr/bin/env python3
"""
strategic_roadmap.py — Strategic Expansion Roadmap (#30-#33)
=============================================================
Detailed, phased implementation plans for the four long-horizon projects.

KEY PHILOSOPHY (from founder feedback 2026-05-26):
  - Don't add new pods — route through existing pods where possible
  - #30 (Multi-Asset): funded from FI Pod capacity, not a new pod
  - Focus depth over breadth — each project has a concrete Phase 1 before Phase 2
  - TCA (#33) is the quickest win and can start immediately once live trades exist
"""

import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ROADMAP_FILE = os.path.join(DATA_DIR, "strategic_roadmap.json")


ROADMAP = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "philosophy": (
        "No new pods. Expand within existing infrastructure. "
        "Start each project with a cheap Phase 1 that proves value before committing to full build."
    ),
    "projects": [

        # ────────────────────────────────────────────────────────────────────
        {
            "id":     30,
            "title":  "Multi-Asset Expansion (Crypto Macro Layer)",
            "status": "PLANNED",
            "pod_owner": "FI Pod",
            "rationale": (
                "FI Pod already analyzes macro signals (credit spreads, yield curves, rate moves). "
                "Crypto's on-chain data (BTC funding rates, exchange netflows) are macro signals "
                "with the same DNA as the credit/rate signals FI already handles. "
                "This is NOT a new trading pod — it's adding 2-3 new data inputs to FI's "
                "existing signal pipeline. Crypto positions, if taken, route through FI's "
                "existing approval/execution chain using Alpaca's crypto trading capability."
            ),
            "scope": "Macro features only. No crypto-specific portfolio sleeve initially.",
            "effort": "4-6 weeks (Phase 1 ready in 1 week)",

            "phases": [
                {
                    "phase": 1,
                    "name":  "Fear-Greed Index as Macro Feature",
                    "effort": "3-4 days",
                    "description": (
                        "Add BTC/crypto fear-greed index (free, Alternative.me API) as a single "
                        "macro feature to feature_engineer.py. Like VIX but for crypto market sentiment. "
                        "Value: when crypto fear is extreme (< 20), risk-off signal that has historically "
                        "preceded equity drawdowns. No trading required — pure signal enrichment."
                    ),
                    "deliverables": [
                        "feature_engineer.py: new feature crypto_fear_greed_index (0-100)",
                        "rd_team_leader.py: fetch https://api.alternative.me/fng/?limit=1 daily",
                        "data/crypto_signals.json: cached daily value",
                        "ML experiment idea 19: test crypto_fear_greed as predictor",
                    ],
                    "data_source": "https://api.alternative.me/fng/ (free, no key)",
                    "implementation_file": "crypto_signal_agent.py",
                },
                {
                    "phase": 2,
                    "name":  "BTC On-Chain Flow as Risk Signal",
                    "effort": "1-2 weeks",
                    "description": (
                        "Exchange inflow/outflow (Glassnode free tier or CryptoQuant) as a risk signal. "
                        "Large BTC exchange inflows historically precede sell-offs (bearish). "
                        "Add btc_exchange_netflow_7d as a feature. IRM reviews weekly."
                    ),
                    "deliverables": [
                        "crypto_signal_agent.py: fetch weekly exchange netflow",
                        "feature_engineer.py: btc_exchange_netflow_7d feature",
                        "IRM weekly bulletin: crypto risk flag if netflow > 2 sigma",
                    ],
                    "data_source": "Glassnode free tier OR Coinglass free API",
                    "prerequisites": ["Phase 1 showing signal value in ML experiments"],
                },
                {
                    "phase": 3,
                    "name":  "Crypto Positions via FI Pod (Optional)",
                    "effort": "2-3 weeks",
                    "description": (
                        "If Phase 1+2 signals show predictive value, allow FI Pod to allocate "
                        "a small sleeve (max 5% of portfolio) to BTC/ETH spot positions via "
                        "Alpaca's crypto trading. FI Board reviews same as equity picks. "
                        "Circuit breaker: crypto allocation paused if crypto fear < 25."
                    ),
                    "deliverables": [
                        "fi_pipeline.py: crypto_picks list alongside equity picks",
                        "manager_agent.py: route crypto tickers to Alpaca crypto endpoint",
                        "pretrade_compliance_agent.py: crypto-specific position limits",
                        "position_risk_monitor.py: tighter stop-loss for crypto (-15%)",
                    ],
                    "prerequisites": [
                        "Phase 1 + 2 showing positive ML AUC lift",
                        "Board approval vote",
                        "Disk upgrade to 30GB",
                    ],
                },
            ],
        },

        # ────────────────────────────────────────────────────────────────────
        {
            "id":     31,
            "title":  "Real-Time Signal Layer (Intraday Response)",
            "status": "PLANNED",
            "pod_owner": "CIT Pod (infrastructure) + existing pods (consumption)",
            "rationale": (
                "Current architecture is batch — cron fires every 15-30 min at fastest. "
                "A single Alpaca WebSocket listener (free, already in our API plan) can "
                "push intraday price changes to a lightweight signal refresher. "
                "This doesn't replace the batch system — it adds a reactive layer on top."
            ),
            "scope": "Intraday alerts only. Positions still executed in existing batch cycles.",
            "effort": "3-4 weeks",

            "phases": [
                {
                    "phase": 1,
                    "name":  "WebSocket Listener (price change alerts)",
                    "effort": "1 week",
                    "description": (
                        "alpaca_ws_listener.py subscribes to quote updates for current positions. "
                        "Writes to data/intraday_alerts.json when any position moves >2% in <30 min. "
                        "Canary agent already reads this file — it gets a real-time feed for free."
                    ),
                    "deliverables": [
                        "alpaca_ws_listener.py: WebSocket subscriber, writes intraday_alerts.json",
                        "canary_agent.py: read intraday_alerts.json as additional trigger source",
                        "Crontab: start ws_listener on reboot via @reboot entry",
                    ],
                    "implementation_file": "alpaca_ws_listener.py",
                },
                {
                    "phase": 2,
                    "name":  "Intraday Score Decay",
                    "effort": "1-2 weeks",
                    "description": (
                        "When a position moves >3% against the ML signal direction intraday, "
                        "reduce its conviction score by 20% in data/fe_picks_queue.json. "
                        "Manager Agent reads this at next cycle and may reduce or exit. "
                        "No new agent — extend position_risk_monitor.py with intraday mode."
                    ),
                    "deliverables": [
                        "position_risk_monitor.py: --intraday mode reads intraday_alerts.json",
                        "fe_picks_queue.json: new field intraday_conviction_adj",
                        "manager_agent.py: respect intraday_conviction_adj when sizing",
                    ],
                    "prerequisites": ["Phase 1 running stably for 1 week"],
                },
                {
                    "phase": 3,
                    "name":  "Intraday Catalyst Entry",
                    "effort": "1-2 weeks",
                    "description": (
                        "If a watchlist ticker drops >4% on high volume intraday AND ML score > 0.7, "
                        "auto-generate a BUY_NEW recommendation for next manager cycle. "
                        "This captures 'buy the dip on conviction picks' intraday catalysts."
                    ),
                    "prerequisites": [
                        "Phase 1+2 stable",
                        "Board approval for intraday-triggered entries",
                        "Pre-trade compliance agent live (already done)",
                    ],
                },
            ],
        },

        # ────────────────────────────────────────────────────────────────────
        {
            "id":     32,
            "title":  "Institutional Flow Velocity (Quiver Quantitative)",
            "status": "READY_TO_START",
            "pod_owner": "R&D Pod",
            "rationale": (
                "13F filings are 90 days stale. Congressional stock trades (Senate/House disclosures) "
                "must be filed within 45 days — already 2x fresher. Quiver Quantitative aggregates "
                "this plus corporate insider transactions, lobbying spend, and government contract awards "
                "into a single free API. R&D Pod already tracks institutional signals — this is "
                "the same signal with 2-6x lower latency."
            ),
            "scope": "New features in feature_engineer.py. No new pods.",
            "effort": "2 weeks (Phase 1 in 3-4 days)",
            "user_action_required": "Register free API key at https://api.quiverquant.com/",

            "phases": [
                {
                    "phase": 1,
                    "name":  "Congressional Trade Signals",
                    "effort": "3-4 days",
                    "description": (
                        "Parse Quiver's /beta/historical/congresstrading/{ticker} endpoint. "
                        "For each ticker: count buy/sell transactions by Congress members in "
                        "last 90 days. Compute congress_net_buys_90d feature. "
                        "Historical evidence: Congress members outperform by ~8-10% annually "
                        "on individual stocks (abnormal returns around filing dates)."
                    ),
                    "deliverables": [
                        "quiver_signal_agent.py: fetch and cache congressional trades",
                        "feature_engineer.py: congress_net_buys_90d, congress_buy_flag features",
                        "ML experiment idea 20: test congress signal AUC lift",
                        "R&D weekly report: highlight tickers with recent Congressional buying",
                    ],
                    "data_source": "https://api.quiverquant.com/beta/historical/congresstrading/{ticker}",
                    "implementation_file": "quiver_signal_agent.py",
                },
                {
                    "phase": 2,
                    "name":  "Government Contract Awards",
                    "effort": "1 week",
                    "description": (
                        "Quiver's /beta/historical/govcontracts/{ticker} shows federal contract awards. "
                        "A $500M+ new government contract is a hard revenue catalyst — not a soft signal. "
                        "Add govcontract_award_90d as a feature. Defense, IT services, healthcare "
                        "are the most active sectors for this signal."
                    ),
                    "deliverables": [
                        "quiver_signal_agent.py: fetch contract awards, compute 90d sum",
                        "feature_engineer.py: govcontract_award_90d (dollar value normalized)",
                        "Alt data signals: flag large new contracts as alerts",
                    ],
                },
                {
                    "phase": 3,
                    "name":  "Lobbying + Corporate Insider Combo",
                    "effort": "1 week",
                    "description": (
                        "Combine: (a) lobbying spend trend (company spending more on lobbying = "
                        "expecting regulatory tailwind), (b) corporate insider buys from Quiver's "
                        "own aggregation (complements our existing Form 4 scraper). "
                        "Create a combined quiver_conviction_score = weighted combo of all 4 signals."
                    ),
                },
            ],
        },

        # ────────────────────────────────────────────────────────────────────
        {
            "id":     33,
            "title":  "TCA / Transaction Cost Analysis",
            "status": "READY_TO_START",
            "pod_owner": "Risk Pod",
            "rationale": (
                "Every basis point of slippage is a direct hit to returns. We currently have zero "
                "visibility into execution quality. Alpaca's order history API provides fill prices, "
                "timestamps, and order sizes — everything needed to compute slippage. "
                "This is the highest-ROI project on a per-week-of-effort basis, and fully self-contained."
            ),
            "scope": "Post-trade analysis only. No execution routing changes initially.",
            "effort": "1-2 weeks (Phase 1 in 2-3 days)",

            "phases": [
                {
                    "phase": 1,
                    "name":  "Basic Fill Quality Report",
                    "effort": "2-3 days",
                    "description": (
                        "Parse Alpaca's filled orders from the last 90 days. For each fill: "
                        "fetch the NBBO midpoint at order submission time (from Alpaca's quote data). "
                        "Compute slippage = (fill_price - midpoint) / midpoint * 10000 (basis points). "
                        "Positive = we paid above midpoint (bad for buys). "
                        "Weekly report: avg slippage by order size bucket, by ticker, by time of day."
                    ),
                    "deliverables": [
                        "tca_agent.py: parse Alpaca orders, compute slippage vs midpoint",
                        "data/tca_report.json: weekly slippage report",
                        "fe_board_report: include avg slippage as execution quality KPI",
                        "IRM weekly bulletin: flag any ticker with slippage > 10bps",
                    ],
                    "implementation_file": "tca_agent.py",
                    "schedule": "Weekly Monday 10am ET",
                },
                {
                    "phase": 2,
                    "name":  "Size-Aware Slippage Adjustment",
                    "effort": "1 week",
                    "description": (
                        "If TCA shows slippage increases non-linearly with order size "
                        "(e.g. slippage doubles when order > 0.5% of ADTV), feed this back into "
                        "pretrade_compliance_agent.py as a dynamic max size limit. "
                        "Effectively: TCA data tightens position sizing automatically."
                    ),
                    "deliverables": [
                        "tca_agent.py: compute slippage curve by size bucket",
                        "pretrade_compliance_agent.py: read tca_slippage_model.json for dynamic limits",
                        "data/tca_slippage_model.json: {size_bucket: expected_slippage_bps}",
                    ],
                    "prerequisites": ["4+ weeks of TCA data from Phase 1"],
                },
                {
                    "phase": 3,
                    "name":  "VWAP Execution + Venue Analysis",
                    "effort": "1-2 weeks",
                    "description": (
                        "Replace simple market orders with VWAP orders for positions > 0.3% of ADTV. "
                        "Alpaca supports VWAP order type natively. "
                        "Also: analyze whether Alpaca's PFOF routing systematically disadvantages "
                        "certain stocks — if so, consider IEX routing for those."
                    ),
                    "prerequisites": [
                        "Phase 1+2 TCA data showing meaningful slippage",
                        "Board approval for execution method change",
                    ],
                },
            ],
        },
    ],

    "implementation_order": [
        {"rank": 1, "id": 33, "reason": "Fastest value — 3 days to Phase 1, pure data we already have"},
        {"rank": 2, "id": 32, "reason": "Free API, proven alpha source, R&D Pod can absorb easily"},
        {"rank": 3, "id": 31, "reason": "Reactive layer — high ops value, no new data sources"},
        {"rank": 4, "id": 30, "reason": "Crypto macro first (Phase 1 is 1 free API call), equity crypto later"},
    ],

    "funding_note": (
        "Project #30 (Multi-Asset) — if/when crypto positions are taken (Phase 3), "
        "funded from FI Pod's existing capital allocation. No new capital needed — "
        "this is a reallocation of a portion of FI's existing equity budget to a "
        "small crypto sleeve (max 5%). FI Board reviews crypto picks same as equity."
    ),
}


def get_roadmap() -> dict:
    return ROADMAP


def save_roadmap() -> None:
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(ROADMAP, f, indent=2, default=str)
        os.replace(tmp, ROADMAP_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def print_roadmap():
    for p in ROADMAP["projects"]:
        print(f"\n{'='*60}")
        print(f"#{p['id']} {p['title']}")
        print(f"Pod: {p['pod_owner']} | Status: {p['status']} | Effort: {p['effort']}")
        print(f"WHY: {p['rationale'][:120]}...")
        for ph in p["phases"]:
            print(f"\n  Phase {ph['phase']}: {ph['name']} ({ph['effort']})")
            print(f"    {ph['description'][:120]}...")
    print(f"\n{'='*60}")
    print("Implementation order:")
    for item in ROADMAP["implementation_order"]:
        print(f"  {item['rank']}. #{item['id']} — {item['reason']}")
    print(f"\nFunding: {ROADMAP['funding_note']}")


if __name__ == "__main__":
    save_roadmap()
    print(f"Strategic roadmap saved: {ROADMAP_FILE}")
    print_roadmap()
