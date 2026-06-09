# =============================================================================
# APEX CAPITAL — IRM BOARD: BEHAVIORAL FINANCE ANALYST
# =============================================================================
# Marcus Chen — Behavioral Finance & Sentiment Analyst
# Independent Risk Manager Board, Seat 3
#
# EXPERTISE
# ─────────
#   Cognitive psychology applied to trading and investment decisions.
#   Has read and absorbed every landmark work in behavioral finance and
#   market psychology:
#
#     • Daniel Kahneman — "Thinking, Fast and Slow"
#     • Kahneman & Tversky — "Prospect Theory" (Econometrica, 1979)
#     • Richard Thaler — "Misbehaving: The Making of Behavioral Economics"
#     • Robert Shiller — "Irrational Exuberance" (3rd ed.)
#     • James Montier — "Behavioural Investing" & "The Little Book of Behavioral Investing"
#     • Andrew Lo — "Adaptive Markets: Financial Evolution at the Speed of Thought"
#     • George Soros — "The Alchemy of Finance" (reflexivity theory)
#     • Nassim Taleb — "Fooled by Randomness"
#     • Jason Zweig — "Your Money and Your Brain"
#     • Meir Statman — "What Investors Really Want"
#     • Charles Mackay — "Extraordinary Popular Delusions and the Madness of Crowds"
#     • John Kenneth Galbraith — "The Great Crash 1929"
#
# RESPONSIBILITIES
# ────────────────
#   1. Detect cognitive biases in Manager Agent decision patterns:
#      - Recency bias (overweighting recent P&L)
#      - Overconfidence (excessive buys after winning streaks)
#      - Loss aversion / disposition effect (holding losers, selling winners)
#      - Herding (mimicking institutional signals without independent analysis)
#      - Anchoring (price anchors from entry price affecting decisions)
#      - Confirmation bias (only seeking data that supports existing positions)
#
#   2. Monitor sentiment risk:
#      - VIX-based fear/greed index
#      - Canary alert patterns as a proxy for market sentiment extremes
#      - Whether agents are reacting to noise vs signal
#
#   3. Flag process failures:
#      - Decisions made without sufficient deliberation (impulsive execution)
#      - Systematic undercutting of risk rules under emotional pressure
#
# OUTPUT
# ──────
#   analyze() → dict with keys:
#     domain, risk_score (1-10), confidence, key_findings,
#     red_flags, recommendations
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("irm_board.behavioral")

# Bias detection thresholds
BIAS_THRESHOLDS = {
    "overconfidence_buy_streak":   3,    # buys after N consecutive wins = overconfidence signal
    "loss_aversion_hold_loss_pct": -8.0, # holding beyond -8% despite stop-loss rule
    "herding_inst_pct":            0.80, # >80% of buys from institutional signals = herding
    "recency_acceleration_days":   5,    # activity spike in last N days vs rolling average
    "win_streak_trigger":          3,    # consecutive wins before checking overconfidence
    "loss_streak_trigger":         2,    # consecutive losses before checking loss aversion
}

# Sentiment regime descriptors (VIX-mapped)
SENTIMENT_REGIMES = {
    "COMPLACENCY":  (0,   15,  "Market deeply complacent. Shiller: 'The time to be "
                                "fearful is when others are greedy.' Risk of sharp reversal."),
    "CALM":         (15,  20,  "Normal market sentiment. Rational fear/greed balance."),
    "CAUTIOUS":     (20,  28,  "Mild anxiety. Investors becoming more selective."),
    "FEARFUL":      (28,  35,  "Elevated fear. Herding and panic-selling risk elevated. "
                                "Kahneman: 'Loss aversion is roughly 2x as powerful as "
                                "equivalent gain.' Agents may over-react to losses."),
    "PANIC":        (35,  50,  "Panic regime. Cognitive bandwidth collapses under stress. "
                                "Montier: 'In a crisis, the brain's fight-or-flight "
                                "response overwhelms analytical reasoning.'"),
    "CRISIS":       (50,  200, "Crisis/crash regime. Decision quality across ALL agents "
                                "degrades severely. All signals suspect. Soros reflexivity "
                                "fully active: price moves driving fundamentals."),
}


class BehavioralAnalyst:
    """
    Marcus Chen — Behavioral Finance & Sentiment Analyst.
    IRM Board Seat 3. Detects cognitive biases and sentiment risks.
    """

    def __init__(self):
        self.name = "Marcus Chen"
        self.title = "Behavioral Finance & Sentiment Analyst"
        self.domain = "Cognitive Bias Detection & Market Sentiment"

    # ------------------------------------------------------------------
    # Data loaders
    # ------------------------------------------------------------------

    def _load_manager_decisions(self) -> list:
        path = "data/manager_decisions.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        return data[-1].get("decisions", [])
            except Exception:
                pass
        return []

    def _load_trade_history(self) -> dict:
        path = "data/trade_review_report.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_canary_state(self) -> dict:
        path = "data/canary_state.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_irm_report(self) -> dict:
        path = "data/risk_manager_report.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    # ------------------------------------------------------------------
    # Bias detection methods
    # ------------------------------------------------------------------

    def _detect_loss_aversion(self, decisions: list, irm: dict) -> tuple[list, list]:
        """
        Disposition effect: do agents hold losers too long?
        Classical Shefrin-Statman finding: investors sell winners 1.7x faster than losers.
        """
        findings = []
        red_flags = []

        perf = irm.get("performance", {})
        worst = perf.get("worst_position", {})
        worst_pnl = worst.get("pnl_pct", 0)
        worst_ticker = worst.get("ticker", "?")

        if worst_pnl < BIAS_THRESHOLDS["loss_aversion_hold_loss_pct"]:
            # Check if Manager has a HOLD or WATCH on this position
            holds = [d for d in decisions
                     if d.get("ticker") == worst_ticker
                     and d.get("manager_action") in ("HOLD", "WATCH")]
            if holds:
                red_flags.append(
                    f"DISPOSITION EFFECT SUSPECTED: {worst_ticker} is at "
                    f"{worst_pnl:.1f}% loss yet Manager Action = "
                    f"'{holds[0].get('manager_action')}'. "
                    f"Per Shefrin & Statman (1985): investors irrationally hold "
                    f"losers to avoid realising a loss. This is also a stop-loss "
                    f"rule breach — risk rules must override psychological resistance."
                )

        # Also check if Manager is SELLING winners aggressively
        sells = [d for d in decisions if d.get("manager_action") in ("SELL", "TRIM")]
        winner_sells = [d for d in sells if d.get("unrealized_pnl_pct", 0) > 5]
        if winner_sells and worst_pnl < -5:
            findings.append(
                f"Pattern consistent with disposition effect: {len(winner_sells)} "
                f"profitable positions being trimmed/sold while losses remain "
                f"unrealised in portfolio. Kahneman: this is a classic 'sure gain' "
                f"preference over holding for larger gain, combined with loss-aversion "
                f"on the losing side."
            )
        elif not winner_sells and not red_flags:
            findings.append(
                "No clear disposition effect detected. Manager is not systematically "
                "selling winners faster than losers this cycle."
            )

        return findings, red_flags

    def _detect_overconfidence(self, decisions: list, trade_history: dict) -> tuple[list, list]:
        """
        Overconfidence: excessive trading after wins, under-sizing after losses.
        DeBondt & Thaler (1985): investors systematically overestimate precision of forecasts.
        """
        findings = []
        red_flags = []

        analytics = trade_history.get("analytics", {})
        win_rate = analytics.get("win_rate", None)

        buys = [d for d in decisions if d.get("manager_action") in ("BUY_NEW", "ADD")]

        if win_rate is not None and win_rate > 65 and len(buys) >= 3:
            red_flags.append(
                f"OVERCONFIDENCE SIGNAL: Win rate = {win_rate:.0f}% and Manager "
                f"executed {len(buys)} buys this cycle. "
                f"DeBondt & Thaler (1985): high recent win rates cause agents "
                f"to systematically overestimate their edge, leading to oversizing "
                f"and insufficient downside analysis. "
                f"Thaler: 'Overconfidence is the most robust finding in the psychology "
                f"of judgment.' Recommend independent validation of each buy thesis."
            )
        elif win_rate is not None and win_rate < 35 and len(buys) == 0:
            findings.append(
                f"Win rate = {win_rate:.0f}% with no new buys — possibly correct "
                f"loss-averse response, but risk of excessive conservatism "
                f"(Kahneman's 'loss aversion' can also cause under-investment "
                f"at the point of maximum opportunity)."
            )
        elif buys:
            findings.append(
                f"{len(buys)} buy orders executed this cycle. "
                f"Trade history win rate: {win_rate:.0f}% — within normal confidence bounds."
                if win_rate is not None else
                f"{len(buys)} buy orders executed this cycle. No win-rate data to cross-check."
            )
        else:
            findings.append("No buys this cycle — overconfidence check not applicable.")

        return findings, red_flags

    def _detect_herding(self, decisions: list) -> tuple[list, list]:
        """
        Herding: are all buys driven by institutional 13F signals (follow-the-crowd)?
        Bikhchandani & Sharma (2001): herding amplifies volatility and destroys alpha.
        """
        findings = []
        red_flags = []

        buys = [d for d in decisions if d.get("manager_action") in ("BUY_NEW", "ADD")]
        if not buys:
            findings.append("No buys to assess for herding behaviour.")
            return findings, red_flags

        # Check what fraction cite institutional signal as rationale
        inst_driven = [
            d for d in buys
            if "13f" in str(d.get("rationale", "")).lower()
            or "institutional" in str(d.get("rationale", "")).lower()
            or "signal" in str(d.get("rationale", "")).lower()
        ]
        pct_inst = len(inst_driven) / len(buys) if buys else 0

        if pct_inst >= BIAS_THRESHOLDS["herding_inst_pct"]:
            red_flags.append(
                f"HERDING RISK: {pct_inst:.0%} of buys are institution-signal-driven. "
                f"Bikhchandani et al. (1992): information cascades form when agents "
                f"discard private signals in favour of observed crowd behaviour. "
                f"When all 13F-followers buy simultaneously at filing announcements, "
                f"alpha is arbitraged away and you're left holding at peak institutional "
                f"entry prices. Independent fundamental validation required."
            )
        else:
            findings.append(
                f"{pct_inst:.0%} of buys are institution-signal-driven "
                f"— herding risk within acceptable bounds."
            )

        return findings, red_flags

    def _assess_sentiment_regime(self, canary: dict, irm: dict) -> tuple[list, list]:
        """
        Maps current market sentiment to psychological decision-quality implications.
        """
        findings = []
        red_flags = []

        # Approximate VIX from canary triggers or IRM data
        triggers = canary.get("last_triggers", [])
        alert_level = canary.get("last_level", "GREEN")
        ra = irm.get("risk_assessment", {})
        risk_level = ra.get("risk_level", "UNKNOWN")

        # Map alert level to approximate VIX zone
        vix_estimate_map = {
            "GREEN":  12,
            "YELLOW": 23,
            "ORANGE": 32,
            "RED":    50,
        }
        vix_est = vix_estimate_map.get(alert_level, 20)

        for regime_name, (lo, hi, desc) in SENTIMENT_REGIMES.items():
            if lo <= vix_est < hi:
                if regime_name in ("PANIC", "CRISIS"):
                    red_flags.append(
                        f"SENTIMENT REGIME: {regime_name} (VIX est. ~{vix_est}). {desc}"
                    )
                elif regime_name == "COMPLACENCY" and risk_level in ("HIGH", "CRITICAL"):
                    red_flags.append(
                        f"DANGEROUS DIVERGENCE: Market appears complacent "
                        f"(VIX est. ~{vix_est}) while IRM-rated portfolio risk is "
                        f"{risk_level}. Shiller: complacency at fundamental highs "
                        f"is a classic late-cycle signal. Soros reflexivity: "
                        f"prices can remain irrational while risk accumulates silently."
                    )
                elif regime_name == "FEARFUL":
                    findings.append(
                        f"Sentiment regime: {regime_name} (VIX est. ~{vix_est}). "
                        f"{desc}"
                    )
                else:
                    findings.append(
                        f"Sentiment regime: {regime_name} (VIX est. ~{vix_est}). "
                        f"Decision quality not materially impaired."
                    )
                break

        return findings, red_flags

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, irm_context: Optional[dict] = None) -> dict:
        """
        Run full behavioral analysis.
        Returns structured findings for the IRM Director.
        """
        irm     = irm_context or self._load_irm_report()
        decs    = self._load_manager_decisions()
        history = self._load_trade_history()
        canary  = self._load_canary_state()

        all_findings = []
        all_red_flags = []
        recommendations = []

        # 1. Loss aversion / disposition effect
        f, r = self._detect_loss_aversion(decs, irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 2. Overconfidence
        f, r = self._detect_overconfidence(decs, history)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 3. Herding
        f, r = self._detect_herding(decs)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # 4. Sentiment regime
        f, r = self._assess_sentiment_regime(canary, irm)
        all_findings.extend(f)
        all_red_flags.extend(r)

        # Risk score
        risk_score = 2
        risk_score += len(all_red_flags) * 2
        risk_score = min(10, risk_score)

        # Recommendations
        if any("DISPOSITION" in r for r in all_red_flags):
            recommendations.append(
                "Enforce stop-loss rules mechanically — remove human/agent discretion "
                "on loss positions. Kahneman: 'Rules protect us from our own biases.'"
            )
        if any("OVERCONFIDENCE" in r for r in all_red_flags):
            recommendations.append(
                "Introduce a mandatory 'pre-mortem' step for each new BUY: "
                "require the Manager Agent to articulate the top 3 ways this "
                "trade can fail before approving. Reduces overconfidence by 30%+ "
                "per Klein (2007) research."
            )
        if any("HERDING" in r for r in all_red_flags):
            recommendations.append(
                "Set a maximum of 50% of new buys from institutional signals in any "
                "single cycle. Remaining buys must be R&D-driven or fundamental."
            )
        if not recommendations:
            recommendations.append(
                "No significant behavioral biases detected this cycle. "
                "Continue monitoring decision patterns across runs."
            )

        confidence = "HIGH" if decs else "MEDIUM"

        logger.debug(
            f"[Behavioral] Score={risk_score} | "
            f"Findings={len(all_findings)} | RedFlags={len(all_red_flags)}"
        )

        return {
            "analyst":         self.name,
            "title":           self.title,
            "domain":          self.domain,
            "risk_score":      risk_score,
            "confidence":      confidence,
            "key_findings":    all_findings,
            "red_flags":       all_red_flags,
            "recommendations": recommendations,
            "timestamp":       datetime.now().isoformat(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    analyst = BehavioralAnalyst()
    result = analyst.analyze()
    print(f"\n{'='*60}")
    print(f"  {result['analyst']} — {result['title']}")
    print(f"  Risk Score: {result['risk_score']}/10  |  Confidence: {result['confidence']}")
    print(f"{'='*60}")
    for f in result["key_findings"]:
        print(f"  • {f}")
    if result["red_flags"]:
        print(f"\n  RED FLAGS:")
        for r in result["red_flags"]:
            print(f"  ⚑ {r}")
    print()
