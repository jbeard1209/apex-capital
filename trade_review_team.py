# =============================================================================
# APEX CAPITAL — HISTORICAL TRADE REVIEW TEAM
# =============================================================================
# Three agents who review every closed position, extract performance patterns,
# and distribute actionable lessons to the agents that need them.
#
# This team is DISTINCT from review_agent.py (which reviews open positions).
# The Trade Review Team only analyses CLOSED / historical trades.
#
# AGENTS
# ──────
#   TradeHistorianAgent
#     Reconstructs every completed trade from state.json and
#     manager_decisions.json. Matches buys to sells, computes hold durations,
#     and links each trade back to the manager's original rationale.
#
#   PerformanceAnalystAgent
#     Takes the Historian's trade records and performs statistical analysis:
#     win rate, average gain/loss, sector breakdown, benchmark comparison
#     vs SPY for the same periods, hold-duration patterns, and failure modes.
#
#   ReviewTeamLeader
#     Orchestrates the above two agents, compiles the findings into a
#     report with explicit lessons and distributes it to:
#       • data/trade_review_report.json   ← all agents read this
#       • Manager Agent (lesson dict to adjust future approvals)
#       • Independent Risk Manager (win-rate feeds into Manager grade)
#       • Chief of Staff (included in daily intel brief)
#       • R&D Team Leader (sector win-rates adjust sector scoring)
#     Emails the Chairman a clean summary.
#
# CADENCE: runs every 7 days (tracked via state.json → last_review_team_run)
#
# Usage:
#   python3 trade_review_team.py
#   python3 trade_review_team.py --no-email
# =============================================================================

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trade_review_team")

STATE_PATH        = "data/state.json"
MANAGER_LOG_PATH  = "data/manager_decisions.json"
REVIEW_RPT_PATH   = "data/trade_review_report.json"


# =============================================================================
# AGENT 1 — TRADE HISTORIAN
# =============================================================================

class TradeHistorianAgent:
    """
    Reconstructs every completed trade from the buy_log and sell_log in
    state.json. Matches entries to exits by ticker (FIFO order).
    Enriches each trade with the manager's rationale if available in the log.
    """

    def run(self) -> list[dict]:
        logger.info("  [Trade Historian] Reconstructing trade history...")

        buy_log  = []
        sell_log = []

        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    state = json.load(f)
                buy_log  = state.get("buy_log",  [])
                sell_log = state.get("sell_log",  [])
            except Exception as e:
                logger.warning(f"  [Trade Historian] Could not read state: {e}")

        # Load manager rationale map: ticker → list of rationale strings
        rationale_map: dict[str, list[str]] = defaultdict(list)
        if os.path.exists(MANAGER_LOG_PATH):
            try:
                with open(MANAGER_LOG_PATH) as f:
                    history = json.load(f)
                for entry in history:
                    for d in entry.get("decisions", []):
                        t = d.get("ticker", "")
                        r = d.get("manager_rationale", [])
                        if t and r:
                            rationale_map[t].extend(r)
            except Exception:
                pass

        # Group buys by ticker (FIFO queue)
        buy_queues: dict[str, list[dict]] = defaultdict(list)
        for b in sorted(buy_log, key=lambda x: x.get("date", "")):
            buy_queues[b["ticker"]].append(b)

        trades: list[dict] = []
        for sell in sorted(sell_log, key=lambda x: x.get("date", "")):
            ticker    = sell["ticker"]
            exit_date = sell["date"]
            reason    = sell.get("reason", "unknown")
            pnl_pct   = sell.get("pnl_pct", 0.0)

            # Match to earliest open buy for this ticker
            entry_date = None
            conviction = None
            if buy_queues[ticker]:
                buy = buy_queues[ticker].pop(0)
                entry_date = buy.get("date")
                conviction = buy.get("conviction", 1.0)

            hold_days = 0
            if entry_date and exit_date:
                try:
                    hold_days = (
                        date.fromisoformat(exit_date) -
                        date.fromisoformat(entry_date)
                    ).days
                except Exception:
                    hold_days = 0

            outcome = (
                "WIN"  if pnl_pct > 0 else
                "LOSS" if pnl_pct < 0 else
                "FLAT"
            )

            trades.append({
                "ticker":      ticker,
                "entry_date":  entry_date,
                "exit_date":   exit_date,
                "hold_days":   hold_days,
                "pnl_pct":     round(pnl_pct, 2),
                "outcome":     outcome,
                "exit_reason": reason,
                "conviction":  conviction,
                "rationale":   rationale_map.get(ticker, [])[:2],  # top 2 rationale lines
            })

        logger.info(f"  [Trade Historian] Reconstructed {len(trades)} closed trade(s)")
        return trades


# =============================================================================
# AGENT 2 — PERFORMANCE ANALYST
# =============================================================================

class PerformanceAnalystAgent:
    """
    Takes the Historian's trade records and computes statistical analytics:
    win rate, average P&L, sector breakdown, benchmark comparison,
    hold-duration patterns, and failure mode classification.
    """

    def _spy_return(self, start: str, end: str) -> float:
        """SPY return (%) between two ISO date strings."""
        try:
            df = yf.download("SPY", start=start, end=end,
                             progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                return 0.0
            prices = df["Close"]
            return float((prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0] * 100)
        except Exception:
            return 0.0

    def _classify_failure(self, trade: dict) -> Optional[str]:
        """Classify why a losing trade lost, if discernible."""
        if trade["outcome"] != "LOSS":
            return None
        reason = trade.get("exit_reason", "").lower()
        pnl    = trade["pnl_pct"]
        hold   = trade["hold_days"]

        if "stop_loss" in reason or "stop-loss" in reason:
            return "stop_loss_triggered"
        if hold < 5 and pnl < -2:
            return "rapid_deterioration"
        if hold > 20 and pnl < -5:
            return "slow_bleed"
        if "momentum" in reason:
            return "momentum_reversal"
        return "other"

    def run(self, trades: list[dict]) -> dict:
        logger.info(f"  [Performance Analyst] Analysing {len(trades)} trade(s)...")

        if not trades:
            return {
                "total_trades":    0,
                "win_rate":        None,
                "avg_pnl_pct":     None,
                "avg_win_pct":     None,
                "avg_loss_pct":    None,
                "avg_hold_days":   None,
                "best_trade":      None,
                "worst_trade":     None,
                "benchmark_delta": None,
                "failure_modes":   {},
                "sector_stats":    {},
                "lessons":         ["Insufficient trade history — no closed positions yet."],
                "pattern_flags":   [],
            }

        wins   = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]

        win_rate    = len(wins) / len(trades) * 100
        avg_pnl     = sum(t["pnl_pct"] for t in trades) / len(trades)
        avg_win     = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0
        avg_loss    = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        avg_hold    = sum(t["hold_days"] for t in trades) / len(trades)

        best  = max(trades, key=lambda t: t["pnl_pct"])
        worst = min(trades, key=lambda t: t["pnl_pct"])

        # Benchmark comparison (SPY for same periods)
        portfolio_total = sum(t["pnl_pct"] for t in trades)
        spy_total       = 0.0
        for t in trades:
            if t.get("entry_date") and t.get("exit_date"):
                spy_total += self._spy_return(t["entry_date"], t["exit_date"])
        benchmark_delta = portfolio_total - spy_total

        # Failure mode breakdown
        failure_modes: dict[str, int] = defaultdict(int)
        for t in losses:
            mode = self._classify_failure(t)
            if mode:
                failure_modes[mode] += 1

        # Hold-duration distribution
        hold_distribution = {
            "under_5d":  sum(1 for t in trades if t["hold_days"] < 5),
            "5_to_15d":  sum(1 for t in trades if 5 <= t["hold_days"] < 15),
            "15_to_30d": sum(1 for t in trades if 15 <= t["hold_days"] < 30),
            "over_30d":  sum(1 for t in trades if t["hold_days"] >= 30),
        }

        # Win rate by hold duration
        early_exits  = [t for t in trades if t["hold_days"] < 7]
        normal_exits = [t for t in trades if 7 <= t["hold_days"] < 30]
        long_holds   = [t for t in trades if t["hold_days"] >= 30]

        def _wr(group):
            return (sum(1 for t in group if t["outcome"] == "WIN") / len(group) * 100
                    if group else None)

        win_by_hold = {
            "under_7d":  _wr(early_exits),
            "7_to_30d":  _wr(normal_exits),
            "over_30d":  _wr(long_holds),
        }

        # Win rate by exit reason
        win_by_reason: dict[str, dict] = {}
        reason_groups: dict[str, list] = defaultdict(list)
        for t in trades:
            reason_groups[t.get("exit_reason", "unknown")].append(t)
        for reason, group in reason_groups.items():
            win_by_reason[reason] = {
                "count":    len(group),
                "win_rate": _wr(group),
                "avg_pnl":  round(sum(t["pnl_pct"] for t in group) / len(group), 2),
            }

        # Pattern flags — actionable observations
        pattern_flags = []
        if win_rate < 40:
            pattern_flags.append(
                f"LOW WIN RATE ({win_rate:.0f}%): Manager Agent is approving too many low-conviction picks. "
                "Raise minimum conviction threshold for BUY_NEW to 4★."
            )
        if avg_loss < -8:
            pattern_flags.append(
                f"LARGE AVERAGE LOSS ({avg_loss:.1f}%): Losses are exceeding the stop-loss design level. "
                "Verify stop-loss enforcement is functioning correctly."
            )
        if avg_win < 3 and wins:
            pattern_flags.append(
                f"SMALL AVERAGE WIN ({avg_win:.1f}%): Winners are being exited too early. "
                "Consider raising take-profit threshold or using trailing stops."
            )
        if avg_win > 0 and avg_loss < 0 and abs(avg_loss) > avg_win * 1.5:
            pattern_flags.append(
                "ASYMMETRIC LOSSES: Average loss is >1.5x average gain — "
                "classic sign of cutting winners short and letting losers run. "
                "Review exit discipline with Manager Agent."
            )
        if hold_distribution.get("under_5d", 0) > len(trades) * 0.4:
            pattern_flags.append(
                "SHORT HOLD BIAS: >40% of trades exit within 5 days. "
                "This suggests excessive reaction to short-term noise. "
                "Consider requiring minimum 7-day hold before momentum-reversal exits."
            )
        if benchmark_delta < -5:
            pattern_flags.append(
                f"UNDERPERFORMING SPY by {abs(benchmark_delta):.1f}pp: "
                "Portfolio is generating less alpha than a passive index. "
                "Reassess the momentum-capture strategy's sector and timing parameters."
            )
        elif benchmark_delta > 5:
            pattern_flags.append(
                f"OUTPERFORMING SPY by {benchmark_delta:.1f}pp: "
                "Strategy is generating genuine alpha. Continue current approach."
            )
        if failure_modes.get("slow_bleed", 0) > 2:
            pattern_flags.append(
                "SLOW BLEED PATTERN: Multiple positions losing slowly over >20 days. "
                "Tighten max-hold-day rule or implement a time-stop for underwater positions."
            )

        # Distilled lessons for Manager Agent to apply
        lessons = []
        if win_rate >= 55:
            lessons.append(
                f"Historical win rate {win_rate:.0f}% — strategy is working. Maintain current conviction standards."
            )
        elif win_rate >= 40:
            lessons.append(
                f"Win rate {win_rate:.0f}% is acceptable. Focus on improving exit timing rather than entry criteria."
            )
        else:
            lessons.append(
                f"Win rate only {win_rate:.0f}% — entry criteria need tightening. Prefer 4★+ conviction picks."
            )

        if wins:
            lessons.append(
                f"Average winner: +{avg_win:.1f}%. "
                f"Best trade: {best['ticker']} (+{best['pnl_pct']:.1f}%). "
                "Replicate entry conditions from winning trades."
            )
        if losses:
            lessons.append(
                f"Average loser: {avg_loss:.1f}%. "
                f"Worst trade: {worst['ticker']} ({worst['pnl_pct']:.1f}%). "
                "Understand what went wrong before re-entering similar setups."
            )
        if benchmark_delta != 0:
            lessons.append(
                f"vs SPY (same periods): {benchmark_delta:+.1f}pp. "
                + ("Active management is adding value." if benchmark_delta > 0 else
                   "Consider reducing active bets and letting more capital track momentum ETFs.")
            )

        logger.info(
            f"  [Performance Analyst] Win rate: {win_rate:.0f}% | "
            f"Avg P&L: {avg_pnl:+.2f}% | vs SPY: {benchmark_delta:+.1f}pp"
        )

        return {
            "total_trades":      len(trades),
            "win_rate":          round(win_rate, 1),
            "avg_pnl_pct":       round(avg_pnl, 2),
            "avg_win_pct":       round(avg_win, 2),
            "avg_loss_pct":      round(avg_loss, 2),
            "avg_hold_days":     round(avg_hold, 1),
            "best_trade":        {"ticker": best["ticker"],  "pnl_pct": best["pnl_pct"]},
            "worst_trade":       {"ticker": worst["ticker"], "pnl_pct": worst["pnl_pct"]},
            "benchmark_delta":   round(benchmark_delta, 2),
            "spy_total_same_periods": round(spy_total, 2),
            "failure_modes":     dict(failure_modes),
            "hold_distribution": hold_distribution,
            "win_rate_by_hold":  win_by_hold,
            "win_rate_by_reason": win_by_reason,
            "pattern_flags":     pattern_flags,
            "lessons":           lessons,
        }


# =============================================================================
# AGENT 3 — REVIEW TEAM LEADER
# =============================================================================

class ReviewTeamLeader:
    """
    Orchestrates the Historian and Analyst, compiles the full review report,
    and distributes lessons to all relevant agents.
    """

    def _compile_manager_directives(self, analytics: dict) -> list[dict]:
        """
        Translate performance findings into explicit directives the Manager
        Agent should follow when evaluating future recommendations.
        """
        directives = []
        win_rate = analytics.get("win_rate") or 0

        if win_rate < 40 and analytics["total_trades"] >= 5:
            directives.append({
                "rule":      "RAISE_MIN_CONVICTION",
                "threshold": 4,
                "reason":    f"Historical win rate only {win_rate:.0f}%. Require 4★ minimum for BUY_NEW.",
            })
        if (analytics.get("avg_win_pct") or 0) < 3 and analytics["total_trades"] >= 5:
            directives.append({
                "rule":   "EXTEND_HOLD_TARGET",
                "reason": "Average winner exits too early. Allow momentum to run — do not sell at first RSI dip.",
            })

        avg_loss = analytics.get("avg_loss_pct") or 0
        if avg_loss < -8:
            directives.append({
                "rule":   "TIGHTEN_STOP_LOSS_VIGILANCE",
                "reason": f"Average loss {avg_loss:.1f}% — exceeds design threshold. Verify stop-loss is auto-approved.",
            })

        benchmark_delta = analytics.get("benchmark_delta") or 0
        if benchmark_delta < -5:
            directives.append({
                "rule":   "PREFER_HIGH_CONVICTION_ONLY",
                "reason": "Underperforming SPY passively. Only approve 4–5★ picks until alpha recovers.",
            })

        return directives

    def _compile_risk_insights(self, analytics: dict, trades: list[dict]) -> dict:
        """
        Distilled risk-relevant insights for the Independent Risk Manager.
        """
        total   = analytics["total_trades"]
        win_r   = analytics.get("win_rate") or 0
        avg_p   = analytics.get("avg_pnl_pct") or 0
        bd      = analytics.get("benchmark_delta") or 0
        fails   = analytics.get("failure_modes", {})

        grade_impact = "NEUTRAL"
        notes = []

        if total < 3:
            notes.append("Too few closed trades for statistical significance.")
        else:
            if win_r >= 55 and bd >= 0:
                grade_impact = "POSITIVE"
                notes.append(f"Strong historical performance: {win_r:.0f}% win rate, +{bd:.1f}pp vs SPY.")
            elif win_r < 40 or bd < -5:
                grade_impact = "NEGATIVE"
                notes.append(
                    f"Weak historical performance: {win_r:.0f}% win rate, {bd:+.1f}pp vs SPY. "
                    "Should weigh against Manager Agent grade."
                )
            else:
                notes.append(f"Adequate historical performance: {win_r:.0f}% win rate, {bd:+.1f}pp vs SPY.")

            if fails.get("stop_loss_triggered", 0) > total * 0.3:
                grade_impact = "NEGATIVE"
                notes.append(
                    f"{fails['stop_loss_triggered']} of {total} trades hit stop-loss — "
                    "high stop-loss frequency suggests poor entry timing."
                )

        return {
            "grade_impact":      grade_impact,
            "notes":             notes,
            "total_trades":      total,
            "win_rate":          win_r,
            "benchmark_delta":   bd,
            "failure_mode_summary": fails,
        }

    def _compile_cos_summary(self, analytics: dict) -> dict:
        """High-level summary for the Chief of Staff's intelligence brief."""
        return {
            "total_trades":    analytics["total_trades"],
            "win_rate":        analytics.get("win_rate"),
            "avg_pnl_pct":     analytics.get("avg_pnl_pct"),
            "benchmark_delta": analytics.get("benchmark_delta"),
            "top_lesson":      analytics["lessons"][0] if analytics.get("lessons") else "Insufficient data.",
            "pattern_count":   len(analytics.get("pattern_flags", [])),
        }

    def run(self, send_email: bool = True) -> dict:
        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — HISTORICAL TRADE REVIEW TEAM")
        logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # Phase 1: Reconstruct trade history
        historian = TradeHistorianAgent()
        trades    = historian.run()

        # Phase 2: Analyse performance
        analyst   = PerformanceAnalystAgent()
        analytics = analyst.run(trades)

        # Phase 3: Compile for each consumer
        manager_directives = self._compile_manager_directives(analytics)
        risk_insights      = self._compile_risk_insights(analytics, trades)
        cos_summary        = self._compile_cos_summary(analytics)

        report = {
            "generated_at":        datetime.now().isoformat(),
            "team":                "Historical Trade Review Team",
            "trades":              trades,
            "analytics":           analytics,
            "manager_directives":  manager_directives,
            "risk_insights":       risk_insights,
            "cos_summary":         cos_summary,
        }

        os.makedirs("data", exist_ok=True)
        with open(REVIEW_RPT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"  Trade review report saved → {REVIEW_RPT_PATH}")
        logger.info(
            f"  Summary: {analytics['total_trades']} trades | "
            f"Win rate: {analytics.get('win_rate','N/A')}% | "
            f"vs SPY: {analytics.get('benchmark_delta',0):+.1f}pp"
        )
        logger.info(f"  Manager directives: {len(manager_directives)}")
        logger.info(f"  Pattern flags: {len(analytics.get('pattern_flags', []))}")

        if send_email:
            _email_chairman(report)

        return report


# =============================================================================
# HTML REPORT
# =============================================================================

def _build_html_report(report: dict) -> str:
    a   = report["analytics"]
    now = datetime.now().strftime("%B %d, %Y at %H:%M")

    if not a["total_trades"]:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;padding:24px;">
  <h1 style="color:#58a6ff;">Trade Review Team — No closed positions yet</h1>
  <p style="color:#8b949e;">Trades will appear here once positions have been opened and closed.</p>
</body></html>"""

    wr_color = (
        "#3fb950" if (a["win_rate"] or 0) >= 55 else
        "#f0883e" if (a["win_rate"] or 0) >= 40 else
        "#f85149"
    )
    bd_color  = "#3fb950" if (a["benchmark_delta"] or 0) >= 0 else "#f85149"
    pnl_color = "#3fb950" if (a["avg_pnl_pct"] or 0) >= 0 else "#f85149"

    # Trade rows (most recent 20)
    recent_trades = sorted(
        report["trades"], key=lambda t: t.get("exit_date", ""), reverse=True
    )[:20]
    trade_rows = ""
    for t in recent_trades:
        pnl_c = "#3fb950" if t["pnl_pct"] > 0 else "#f85149" if t["pnl_pct"] < 0 else "#8b949e"
        oc_c  = "#3fb950" if t["outcome"] == "WIN" else "#f85149" if t["outcome"] == "LOSS" else "#8b949e"
        trade_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:8px;font-weight:bold;color:#58a6ff;">{t['ticker']}</td>
          <td style="padding:8px;color:#8b949e;">{t.get('entry_date','—')}</td>
          <td style="padding:8px;color:#8b949e;">{t.get('exit_date','—')}</td>
          <td style="padding:8px;">{t.get('hold_days','—')}d</td>
          <td style="padding:8px;color:{pnl_c};font-weight:bold;">{t['pnl_pct']:+.2f}%</td>
          <td style="padding:8px;color:{oc_c};font-weight:bold;">{t['outcome']}</td>
          <td style="padding:8px;color:#8b949e;font-size:12px;">{t.get('exit_reason','—')}</td>
        </tr>"""

    # Pattern flags
    flag_items = "".join(
        f'<li style="color:#f0883e;margin-bottom:8px;">{f}</li>'
        for f in a.get("pattern_flags", [])
    ) or '<li style="color:#3fb950;">No significant patterns detected — performance within normal bounds.</li>'

    # Lessons
    lesson_items = "".join(
        f'<li style="color:#c9d1d9;margin-bottom:6px;">→ {l}</li>'
        for l in a.get("lessons", [])
    )

    # Manager directives
    dir_rows = ""
    for d in report.get("manager_directives", []):
        dir_rows += f"""
        <tr style="border-bottom:1px solid #21262d;">
          <td style="padding:8px;color:#f0c000;font-weight:bold;">{d['rule']}</td>
          <td style="padding:8px;color:#c9d1d9;">{d['reason']}</td>
        </tr>"""
    if not dir_rows:
        dir_rows = '<tr><td colspan="2" style="color:#3fb950;padding:12px;">No corrective directives — continue current strategy.</td></tr>'

    best  = a.get("best_trade",  {})
    worst = a.get("worst_trade", {})

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:980px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#e6edf3;margin:0;font-size:22px;">
      APEX CAPITAL — Historical Trade Review Report
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      To: Manager Agent · Risk Manager · Chief of Staff &nbsp;·&nbsp; {now}
    </p>
  </div>

  <!-- Headline metrics -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:24px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Closed Trades</div>
      <div style="font-size:22px;font-weight:bold;">{a['total_trades']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Win Rate</div>
      <div style="font-size:22px;font-weight:bold;color:{wr_color};">{a['win_rate']:.1f}%</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Avg P&amp;L</div>
      <div style="font-size:22px;font-weight:bold;color:{pnl_color};">{a['avg_pnl_pct']:+.2f}%</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">vs SPY (same periods)</div>
      <div style="font-size:22px;font-weight:bold;color:{bd_color};">{a['benchmark_delta']:+.2f}pp</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Avg Hold</div>
      <div style="font-size:22px;font-weight:bold;">{a['avg_hold_days']:.1f}d</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Best Trade</div>
      <div style="font-size:18px;font-weight:bold;color:#3fb950;">
        {best.get('ticker','—')} {f"+{best['pnl_pct']:.1f}%" if best else ''}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Worst Trade</div>
      <div style="font-size:18px;font-weight:bold;color:#f85149;">
        {worst.get('ticker','—')} {f"{worst['pnl_pct']:.1f}%" if worst else ''}</div>
    </div>
  </div>

  <!-- Pattern flags -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Patterns Detected</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;">
    <ul style="margin:0;padding-left:20px;line-height:2.0;">{flag_items}</ul>
  </div>

  <!-- Lessons -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Key Lessons</h2>
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;">
    <ul style="margin:0;padding-left:20px;line-height:2.0;">{lesson_items}</ul>
  </div>

  <!-- Manager directives -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Directives for Manager Agent</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    <thead><tr style="background:#21262d;">
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;width:260px;">RULE</th>
      <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">REASON</th>
    </tr></thead>
    <tbody>{dir_rows}</tbody>
  </table>

  <!-- Trade history table -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Recent Closed Trades (last 20)</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    <thead><tr style="background:#21262d;">
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">TICKER</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">ENTRY</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">EXIT</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">HOLD</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">P&amp;L</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">OUTCOME</th>
      <th style="padding:8px;text-align:left;color:#8b949e;font-size:12px;">EXIT REASON</th>
    </tr></thead>
    <tbody>{trade_rows or '<tr><td colspan="7" style="color:#8b949e;padding:12px;">No closed trades yet.</td></tr>'}</tbody>
  </table>

  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;padding-top:12px;">
    Apex Capital Intelligence — Historical Trade Review Team &nbsp;·&nbsp;
    Paper trading simulation — not real investment advice &nbsp;·&nbsp;
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
        a       = report["analytics"]
        subject = (
            f"Apex Capital — Trade Review | "
            f"Win Rate:{a.get('win_rate','N/A')}% | "
            f"vs SPY:{a.get('benchmark_delta',0):+.1f}pp | "
            f"{datetime.now().strftime('%b %d, %Y')}"
        )
        key_metrics = {
            "Closed Trades":  str(a["total_trades"]),
            "Win Rate":       f"{a.get('win_rate',0):.1f}%",
            "Avg P&L":        f"{a.get('avg_pnl_pct',0):+.2f}%",
            "vs SPY":         f"{a.get('benchmark_delta',0):+.2f}pp",
        }
        polished = polish_email(html, subject, "review_team", key_metrics)
        success  = send_daily_report(html_body=polished, subject=subject, paper_day=0,
                                     sender_name="review_team")
        if success:
            logger.info("  Trade review report emailed ✓")
        else:
            logger.warning("  Email failed — report saved to disk only")
    except Exception as e:
        logger.warning(f"  Email error: {e}")


# =============================================================================
# PUBLIC ENTRY POINTS
# =============================================================================

def run_trade_review(send_email: bool = True) -> dict:
    leader = ReviewTeamLeader()
    return leader.run(send_email=send_email)


def load_trade_review() -> dict:
    """Load the most recent trade review report from disk (no re-run)."""
    if not os.path.exists(REVIEW_RPT_PATH):
        return {}
    try:
        with open(REVIEW_RPT_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    no_email = "--no-email" in sys.argv
    run_trade_review(send_email=not no_email)
