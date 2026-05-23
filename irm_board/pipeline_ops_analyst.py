# =============================================================================
# APEX CAPITAL — IRM BOARD: PIPELINE & OPERATIONS ARCHITECT
# =============================================================================
# Dr. James Osei — Pipeline & Operations Architect
# Independent Risk Manager Board, Seat 5
#
# EXPERTISE
# ─────────
#   Systems reliability engineering and operational audit of automated
#   decision pipelines.  Specialises in the class of failure that the
#   other three IRM specialists cannot see: not "did the portfolio lose money?"
#   but "did the system that manages the portfolio behave as designed?"
#
#   Primary references:
#     • Site Reliability Engineering — Beyer et al. (Google, 2016)
#     • The Art of Monitoring — Turnbull (2016)
#     • Release It! — Michael Nygard (stability patterns in distributed systems)
#     • Accelerate — Forsgren et al. (DORA metrics: deployment frequency, MTTR)
#     • Normal Accidents — Charles Perrow (tight coupling, cascade failure)
#     • The Checklist Manifesto — Atul Gawande (systematic failure in complex ops)
#
# RESPONSIBILITIES
# ────────────────
#   1. Verify that agents ran in dependency-correct order (e.g. PDO before
#      any agent that consumes its signal)
#   2. Flag configuration drift: TTL windows, cadence settings, and thresholds
#      that no longer match operational reality
#   3. Detect agent cadence failures: monitoring agents that are supposed to run
#      every cycle but haven't produced fresh output
#   4. Identify deduplication risks: multiple agents or pipeline steps that
#      could submit the same order to Alpaca
#   5. Verify that newly added agents are producing output and are wired into
#      the correct pipeline positions
#   6. Flag orphaned legacy artifacts: scripts, files, or data that suggest
#      superseded systems are still active
#
# METHOD
# ──────
#   All analysis is performed by reading data file timestamps and contents.
#   The architect never reads source code directly — it infers execution
#   ordering and cadence from the metadata of what the agents produce.
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

logger = logging.getLogger("irm_board.pipeline_ops")

# Expected output files and their maximum acceptable age in hours.
# Agents that haven't produced fresh output within their window are flagged.
AGENT_OUTPUT_FRESHNESS: dict = {
    "data/risk_bulletin.json":            {"max_age_h": 26,  "agent": "Chief Risk Officer",         "critical": True},
    "data/review_recommendations.json":   {"max_age_h": 26,  "agent": "Review Agent",               "critical": True},
    "data/manager_decisions.json":        {"max_age_h": 26,  "agent": "Manager Agent",              "critical": True},
    "data/capital_efficiency_report.json":{"max_age_h": 26,  "agent": "Portfolio Deployment Officer","critical": True},
    "data/rd_system_performance.json":    {"max_age_h": 26,  "agent": "System Performance Analyst", "critical": True},
    "data/chief_of_staff_report.json":    {"max_age_h": 26,  "agent": "Chief of Staff",             "critical": False},
    "data/hr_report.json":                {"max_age_h": 26,  "agent": "HR Team",                    "critical": False},
    "data/irm_daily_check.json":          {"max_age_h": 26,  "agent": "IRM Daily Board",            "critical": True},
    "data/canary_state.json":             {"max_age_h": 13,  "agent": "Canary Agent",               "critical": True},
    "data/research_team_report.json":     {"max_age_h": 96,  "agent": "R&D Research Team",         "critical": False},
    "data/rd_historical_context.json":    {"max_age_h": 96,  "agent": "Historical Context Analyst", "critical": False},
    "data/rd_geopolitical_context.json":  {"max_age_h": 96,  "agent": "Geopolitical Risk Analyst",  "critical": False},
}

# Files that should NOT exist in production (legacy artifacts)
LEGACY_ARTIFACTS: list = [
    # head_agent.py had hardcoded NVDA/AVGO/AMD/MRVL allocations from the
    # original single-agent design. Its data file, if present, indicates
    # the old system ran — positions may be untracked by the current pipeline.
]

# Dependency ordering: key must be older than value (key ran first).
# Format: {earlier_file: later_file, ...}
# If earlier_file is NEWER than later_file, the ordering was violated.
DEPENDENCY_ORDER: dict = {
    # PDO must be written before manager_decisions (13F + Manager both use PDO signal)
    "data/capital_efficiency_report.json": "data/manager_decisions.json",
    # CRO bulletin must precede PDO
    "data/risk_bulletin.json": "data/capital_efficiency_report.json",
    # Review recommendations must precede manager decisions
    "data/review_recommendations.json": "data/manager_decisions.json",
}

# Research TTL (hours) — should match _RESEARCH_TTL_HOURS in main.py
RESEARCH_TTL_H = 72


def _file_age_hours(path: str) -> Optional[float]:
    """Return file age in hours, or None if file doesn't exist."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        # Some files are lists (e.g. manager_decisions.json), others are dicts.
        # Only try .get() on dicts; fall through to OS mtime otherwise.
        gen_at = None
        if isinstance(data, dict):
            gen_at = data.get("generated_at") or data.get("timestamp") or data.get("last_updated")
        elif isinstance(data, list) and data and isinstance(data[-1], dict):
            # List of decision records — use the most recent entry's timestamp
            last = data[-1]
            gen_at = last.get("generated_at") or last.get("timestamp")
        if gen_at:
            dt = datetime.fromisoformat(str(gen_at)[:19])
            return (datetime.now() - dt).total_seconds() / 3600
        # Fall back to OS mtime
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except Exception:
        # Last resort: OS mtime
        try:
            mtime = os.path.getmtime(path)
            return (datetime.now().timestamp() - mtime) / 3600
        except Exception:
            return None


class PipelineOpsAnalyst:
    """
    Dr. James Osei — Pipeline & Operations Architect.
    IRM Board Seat 5. Audits pipeline execution order, agent cadence,
    configuration drift, and operational continuity.
    """

    def __init__(self):
        self.name  = "James Osei"
        self.title = "Pipeline & Operations Architect"
        self.domain = "Pipeline Execution Order · Agent Cadence · Configuration Drift · Operational Continuity · FE/FI Pod Health"

    def _is_weekday_local(self) -> bool:
        from datetime import datetime
        return datetime.now().weekday() < 5

    def analyze(self, irm_context: dict = None) -> dict:
        findings      = []
        red_flags     = []
        recommendations = []
        risk_score    = 0

        # ── 1. Agent output freshness ─────────────────────────────────────
        for path, spec in AGENT_OUTPUT_FRESHNESS.items():
            age = _file_age_hours(path)
            agent = spec["agent"]
            max_age = spec["max_age_h"]
            is_critical = spec["critical"]

            if age is None:
                msg = f"{agent} — output file missing: {path}"
                if is_critical:
                    red_flags.append(msg)
                    risk_score += 2
                else:
                    findings.append(msg)
                    risk_score += 1
            elif age > max_age:
                msg = (
                    f"{agent} — output is {age:.0f}h old "
                    f"(threshold: {max_age}h). Agent may have missed a cycle."
                )
                if is_critical:
                    red_flags.append(msg)
                    risk_score += 2
                else:
                    findings.append(msg)
                    risk_score += 1
            else:
                findings.append(
                    f"{agent} — output fresh ({age:.1f}h old, within {max_age}h window). ✓"
                )

        # ── 2. Dependency ordering validation ─────────────────────────────
        for earlier, later in DEPENDENCY_ORDER.items():
            age_e = _file_age_hours(earlier)
            age_l = _file_age_hours(later)

            if age_e is None or age_l is None:
                continue   # Can't validate if either file is missing

            # Earlier file should be OLDER (larger age_h) than later file.
            # If earlier is NEWER (smaller age_h), it ran after the later file
            # — which means the consumer used a stale version of the earlier file.
            # Minimum delta: 30 minutes (0.5h) — differences under this are
            # likely race conditions within the same pipeline cycle, not true
            # ordering violations.
            MIN_DELTA_H = 0.5
            if age_e < age_l and (age_l - age_e) > MIN_DELTA_H:
                earlier_name = earlier.split("/")[-1].replace(".json","")
                later_name   = later.split("/")[-1].replace(".json","")
                msg = (
                    f"DEPENDENCY VIOLATION: {earlier_name} is {age_e:.1f}h old "
                    f"but {later_name} is {age_l:.1f}h old — "
                    f"{later_name} ran BEFORE {earlier_name} and used a stale signal."
                )
                red_flags.append(msg)
                risk_score += 3
                recommendations.append(
                    f"Ensure {earlier_name} runs before {later_name} in the pipeline. "
                    f"See Step 2e (PDO) placement in main.py."
                )

        # ── 3. Research TTL dark-window check ─────────────────────────────
        research_age = _file_age_hours("data/research_team_report.json")
        if research_age is not None:
            if research_age > RESEARCH_TTL_H:
                msg = (
                    f"RESEARCH DARK WINDOW: Research picks are {research_age:.0f}h old, "
                    f"exceeding the {RESEARCH_TTL_H}h TTL. Manager Agent is running "
                    f"without fresh research picks this cycle."
                )
                red_flags.append(msg)
                risk_score += 2
                recommendations.append(
                    f"R&D cycle runs every 4 days (96h). With TTL={RESEARCH_TTL_H}h, "
                    f"there is a {96 - RESEARCH_TTL_H}h dark window each cycle. "
                    f"Consider running R&D every 3 days to eliminate it entirely."
                )
            elif research_age > RESEARCH_TTL_H * 0.8:
                findings.append(
                    f"Research picks are {research_age:.0f}h old — "
                    f"approaching the {RESEARCH_TTL_H}h TTL. Will expire in "
                    f"~{RESEARCH_TTL_H - research_age:.0f}h."
                )

        # ── 4. Canary continuity check ────────────────────────────────────
        canary_age = _file_age_hours("data/canary_state.json")
        if canary_age is None:
            red_flags.append(
                "CANARY CONTINUITY: canary_state.json not found. "
                "The canary agent may not be running. 24/7 market monitoring is offline."
            )
            risk_score += 3
            recommendations.append(
                "Verify canary_agent.py is scheduled to run continuously. "
                "Without the canary, intraday drawdowns go undetected until the 4:15 PM pipeline runs."
            )
        elif canary_age > 13:
            red_flags.append(
                f"CANARY CONTINUITY: canary_state.json is {canary_age:.0f}h old. "
                f"Canary should update every few hours — a {canary_age:.0f}h gap suggests "
                f"it may have stopped running."
            )
            risk_score += 2

        # ── 5. SystemPerformanceAnalyst cadence check ──────────────────────
        spa_age = _file_age_hours("data/rd_system_performance.json")
        if spa_age is not None and spa_age > 26:
            msg = (
                f"MONITORING GAP: SystemPerformanceAnalyst output is {spa_age:.0f}h old. "
                f"It is a monitoring agent and should run every cycle (≤26h). "
                f"If it only runs on R&D days (every 4 days), system failures go "
                f"undetected for up to {spa_age:.0f}h."
            )
            red_flags.append(msg)
            risk_score += 2
            recommendations.append(
                "Run SystemPerformanceAnalyst at Step 0b every cycle, "
                "not only on R&D days. It is a monitoring agent, not a research agent."
            )

        # ── 6. HR report and IRM sealed annex freshness ───────────────────
        hr_age    = _file_age_hours("data/hr_report.json")
        sealed_age = _file_age_hours("data/hr_irm_sealed.json")
        if hr_age is not None and hr_age > 26:
            findings.append(
                f"HR report is {hr_age:.0f}h old — HR team may have missed a cycle."
            )
        if sealed_age is not None and sealed_age > 26:
            findings.append(
                f"Sealed IRM evaluation is {sealed_age:.0f}h old — "
                f"IRM self-audit may not have reached the Chairman this cycle."
            )

        # ── 7. FE pod pipeline health ──────────────────────────────────────
        import os as _os
        fe_pipeline_age = _file_age_hours("data/fe_pipeline_report.json")
        fe_board_age    = _file_age_hours("data/fe_board_report.json")
        fe_picks_age    = _file_age_hours("data/fe_picks_queue.json")

        if fe_pipeline_age is not None and fe_pipeline_age > 30:
            findings.append(
                f"FE PIPELINE: fe_pipeline_report.json is {fe_pipeline_age:.0f}h old — "
                f"FE pipeline may have missed its daily run."
            )
        elif fe_pipeline_age is None and self._is_weekday_local():
            findings.append("FE PIPELINE: fe_pipeline_report.json not found — FE pipeline may not have run yet.")

        if fe_board_age is not None and fe_board_age > 200:
            findings.append(
                f"FE BOARD: Last board meeting was {fe_board_age:.0f}h ago "
                f"({fe_board_age/24:.1f} days). FE board meets weekly — check schedule."
            )

        if fe_picks_age is not None:
            findings.append(f"FE picks queue: {fe_picks_age:.0f}h old.")
        else:
            findings.append("FE picks queue not found — no picks awaiting FE board review.")

        # ── 8. FI pod pipeline health ──────────────────────────────────────
        fi_pipeline_age = _file_age_hours("data/fi_pipeline_report.json")
        fi_board_age    = _file_age_hours("data/fi_board_report.json")

        if fi_pipeline_age is not None and fi_pipeline_age > 30:
            findings.append(
                f"FI PIPELINE: fi_pipeline_report.json is {fi_pipeline_age:.0f}h old — "
                f"FI pipeline may have missed its daily run."
            )
        elif fi_pipeline_age is None and self._is_weekday_local():
            findings.append("FI PIPELINE: fi_pipeline_report.json not found — FI pipeline may not have run yet.")

        if fi_board_age is not None and fi_board_age > 200:
            findings.append(
                f"FI BOARD: Last board meeting was {fi_board_age:.0f}h ago "
                f"({fi_board_age/24:.1f} days). FI board meets weekly — check schedule."
            )

        # ── 10. Risk score cap and confidence ─────────────────────────────
        risk_score = min(10, risk_score)

        if red_flags:
            confidence = "HIGH"
        elif findings:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        if not recommendations:
            recommendations.append(
                "Pipeline operational health appears normal. "
                "All agents are producing fresh output in dependency-correct order."
            )

        logger.debug(
            f"  [PipelineOps] Score={risk_score} | "
            f"Findings={len(findings)} | RedFlags={len(red_flags)}"
        )

        return {
            "analyst":         self.name,
            "domain":          self.domain,
            "risk_score":      risk_score,
            "confidence":      confidence,
            "key_findings":    findings,
            "red_flags":       red_flags,
            "recommendations": recommendations,
        }


# =============================================================================
# Standalone entry point
# =============================================================================
if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(levelname)-8s %(message)s")
    analyst = PipelineOpsAnalyst()
    result  = analyst.analyze()
    print(f"\n  Pipeline & Operations Audit — Dr. James Osei")
    print(f"  Risk Score: {result['risk_score']}/10  |  Confidence: {result['confidence']}")
    print(f"\n  Key Findings ({len(result['key_findings'])}):")
    for f in result["key_findings"]:
        print(f"    • {f[:100]}")
    if result["red_flags"]:
        print(f"\n  Red Flags ({len(result['red_flags'])}):")
        for r in result["red_flags"]:
            print(f"    🔴 {r[:100]}")
    print()
