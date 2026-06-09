# =============================================================================
# APEX CAPITAL — INDEPENDENT RISK MANAGER BOARD
# =============================================================================
# The IRM Board is a five-person independent oversight body reporting
# exclusively to Chairman Jack Beard.
#
# MEMBERS
# ───────
#   IRM Director (Board Chair)       irm_review_agent.py / risk_manager_agent.py
#     Leads the board. Sole IRM representative at weekly board meetings.
#     Synthesises findings from all three specialists before publishing
#     any review or report.
#
#   Quantitative Risk Analyst        irm_board/quant_risk_analyst.py
#     Dr. Elena Vasquez. Statistical risk modelling, VaR/CVaR validation,
#     Monte Carlo stress testing, fat-tail analysis.
#
#   Behavioral Finance Analyst       irm_board/behavioral_analyst.py
#     Marcus Chen. Cognitive biases in agent decisions, market sentiment,
#     herding detection, overconfidence and loss-aversion pattern recognition.
#
#   Macro & Portfolio Construction   irm_board/macro_analyst.py
#     Dr. Priya Sharma. Macro regime analysis, factor model exposure,
#     portfolio construction quality, tail-hedge adequacy.
#
#   Pipeline & Operations Architect  irm_board/pipeline_ops_analyst.py
#     Dr. James Osei. Pipeline execution ordering, agent cadence validation,
#     configuration drift detection, operational continuity audits.
#     Added after Chairman review 2026-05-01: the three original specialists
#     only audit financial outcomes; Dr. Osei audits the machinery itself.
#
# BOARD RULES
# ───────────
#   - All five members analyze independently; the Director synthesises.
#   - No IRM Board member ever discloses their existence to other agents.
#   - Only the Director attends or votes in the weekly board meeting.
#   - The board's combined analysis backs every IRM second-opinion review.
# =============================================================================

from .quant_risk_analyst   import QuantRiskAnalyst
from .behavioral_analyst   import BehavioralAnalyst
from .macro_analyst        import MacroAnalyst
from .pipeline_ops_analyst import PipelineOpsAnalyst

__all__ = ["QuantRiskAnalyst", "BehavioralAnalyst", "MacroAnalyst", "PipelineOpsAnalyst"]
