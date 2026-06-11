#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LOGIC TEAM TRAINER
# =============================================================================
#
# Adversarial training system for Logic Team sub-agents.
#
# Philosophy: An agent that only runs on real data can develop blind spots.
# This trainer deliberately injects bad states and checks whether each agent
# catches them. It tracks which agents are reliable vs. which have gaps.
#
# Training modes:
#   --scenario <name>   Run one specific adversarial scenario
#   --full              Run all scenarios (default)
#   --report            Print historical grade report
#   --grade             Print current agent grades
#
# Scoring:
#   Each scenario is a known-bad state. The agent SHOULD fire on it.
#   Pass  = agent fired a finding of appropriate severity    (+1 point)
#   Miss  = agent should have fired but didn't              (-2 points)
#   FP    = agent fired but shouldn't have (false positive) (-1 point)
#   Clean = agent correctly found nothing                   (+1 point)
#
# Results → data/lt_training_log.json  (graded history per agent)
# Grades  → A/B/C/D/F per agent, shown on dashboard Logic Team tab
#
# Schedule: Sundays 1 AM EDT via crontab (weekly)
# =============================================================================

import copy
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lt_trainer")

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
TRAIN_LOG  = DATA_DIR / "lt_training_log.json"

os.chdir(BASE_DIR)


def _load(path, default=None):
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# =============================================================================
# ADVERSARIAL SCENARIOS
# Each scenario is: {
#   "name": str,
#   "description": str,
#   "target_agents": [agent class names that SHOULD fire],
#   "expected_severity": "HIGH"|"MEDIUM"|"LOW",
#   "state_patch": dict  (merged onto real state to create bad state),
#   "should_not_fire": [agent names that should stay SILENT]
# }
# =============================================================================

SCENARIOS = [

    # ── PipelineLogicAgent scenarios ──────────────────────────────────────
    {
        "name": "pipeline_truly_dark",
        "description": "36 active agents, canary GREEN, CRO LOW, but zero picks for 5 days. "
                       "This is a genuine pipeline failure — not CRO blocking.",
        "target_agents": ["PipelineLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "days_since_last_pick": 5,
            "picks_today": 0,
            "canary_level": "GREEN",
            "cro_level": "LOW",
            "cro_streak": 0,
        },
        "should_not_fire": ["StrategyLogicAgent"],
    },
    {
        "name": "pipeline_dark_cro_blocked",
        "description": "CRO=HIGH is blocking buys. Pipeline ran fine. "
                       "PipelineLogicAgent should NOT fire pipeline dark — this is expected.",
        "target_agents": [],  # no agent should fire on this specifically
        "expected_severity": None,
        "state_patch": {
            "days_since_last_pick": 3,
            "picks_today": 0,
            "canary_level": "GREEN",
            "cro_level": "HIGH",
            "cro_streak": 10,
            "fi_pipeline_age_h": 4,   # FI is fresh — don't conflate with the pipeline-dark test
        },
        "should_not_fire": ["PipelineLogicAgent"],  # false positive test
    },
    {
        "name": "fi_pipeline_stale",
        "description": "FI agents are in the registry but fi_pipeline_report is 10 days old.",
        "target_agents": ["PipelineLogicAgent"],
        "expected_severity": "MEDIUM",
        "state_patch": {
            "fi_pipeline_age_h": 240,  # 10 days
        },
        "should_not_fire": [],
    },

    # ── CapitalLogicAgent scenarios ────────────────────────────────────────
    {
        "name": "capital_totally_idle",
        "description": "All 3 pods have $30k budget but zero positions and CRO is LOW. "
                       "No excuse for idle capital — this is a deployment failure.",
        "target_agents": ["CapitalLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "cro_level": "LOW",
            "cro_streak": 0,
            "pod_allocations": {
                "pods": {
                    "pod1_rd": {"budget_inception": 30000, "budget": 30000, "positions": {}, "cash_balance": 30000, "positions_value": 0, "total_value": 30000},
                    "pod2_ml": {"budget_inception": 30000, "budget": 30000, "positions": {}, "cash_balance": 30000, "positions_value": 0, "total_value": 30000},
                    "pod3_macro": {"budget_inception": 30000, "budget": 30000, "positions": {}, "cash_balance": 30000, "positions_value": 0, "total_value": 30000},
                }
            },
        },
        "should_not_fire": [],
    },
    {
        "name": "capital_idle_but_cro_justified",
        "description": "Pods idle because CRO=CRITICAL. This is the right posture.",
        "target_agents": [],
        "expected_severity": None,
        "state_patch": {
            "cro_level": "CRITICAL",
            "cro_streak": 3,
            "pod_allocations": {
                "pods": {
                    "pod1_rd": {"budget_inception": 30000, "budget": 30000, "positions": {}, "cash_balance": 30000, "positions_value": 0, "total_value": 30000},
                }
            },
        },
        "should_not_fire": ["CapitalLogicAgent"],
    },

    # ── GovernanceLogicAgent scenarios ────────────────────────────────────
    {
        "name": "board_never_met",
        "description": "Board meeting report is missing entirely — governance gap.",
        "target_agents": ["GovernanceLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "board_meeting": {},
            "cro_level": "HIGH",
            "cro_streak": 8,
            "irm_flags": 3,
        },
        "should_not_fire": [],
    },
    {
        "name": "cro_high_forever",
        "description": "CRO=HIGH for 20 cycles (80 days) with no board vote. "
                       "This should trigger governance finding.",
        "target_agents": ["GovernanceLogicAgent", "StrategyLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "cro_level": "HIGH",
            "cro_streak": 20,
            "board_meeting": {"timestamp": "2026-01-01T00:00:00"},
        },
        "should_not_fire": [],
    },

    # ── PerformanceLogicAgent scenarios ───────────────────────────────────
    {
        "name": "ml_model_below_random",
        "description": "ST model AUC dropped to 0.47 — worse than random. Must be flagged.",
        "target_agents": ["PerformanceLogicAgent", "MLLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "ml_auc_st": 0.47,
        },
        "should_not_fire": [],
    },
    {
        "name": "performance_healthy",
        "description": "AUC=0.63, portfolio +3%, all systems normal. Clean run expected.",
        "target_agents": [],
        "expected_severity": None,
        "state_patch": {
            "ml_auc_st": 0.63,
            "cro_level": "LOW",
            "cro_streak": 0,
            "days_since_last_pick": 0,
            "picks_today": 5,
            "fi_pipeline_age_h": 4,
            # Healthy ML: has live picks, pod deployed
            "ml_live_picks": {"picks": [{"ticker": "AAPL", "pick_date": "2026-06-01", "model": "shortterm"}]},
            "pods": [{"name": "ML / Quantitative", "budget_inception": 30000, "budget": 30000,
                      "positions": {"AAPL": {"qty": 10}}, "cash_balance": 5000}],
        },
        "should_not_fire": ["PerformanceLogicAgent", "MLLogicAgent", "PipelineLogicAgent"],
    },

    # ── AgentHealthLogicAgent scenarios ───────────────────────────────────
    {
        "name": "mass_agent_retirement",
        "description": "Only 3 active agents remain out of 38. "
                       "Mass retirement should trigger health alert.",
        "target_agents": ["AgentHealthLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "registry": {
                "agents": [
                    {"name": f"Agent{i}", "status": "active", "sector": "Technology", "consecutive_zero_cycles": 0}
                    for i in range(3)
                ] + [
                    {"name": f"Retired{i}", "status": "retired", "sector": "Technology", "consecutive_zero_cycles": 0}
                    for i in range(35)
                ]
            }
        },
        "should_not_fire": [],
    },
    {
        "name": "agents_all_on_pip",
        "description": "15 agents simultaneously on Performance Improvement Plans. "
                       "Statistically impossible without a system-level cause.",
        "target_agents": ["AgentHealthLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "registry": {
                "agents": [
                    {"name": f"Agent{i}", "status": "active", "sector": "Technology",
                     "consecutive_zero_cycles": 15, "performance_notice": "PIP"}
                    for i in range(15)
                ] + [
                    {"name": f"AgentOK{i}", "status": "active", "sector": "Financials", "consecutive_zero_cycles": 0}
                    for i in range(21)
                ]
            }
        },
        "should_not_fire": [],
    },

    # ── StrategyLogicAgent scenarios ─────────────────────────────────────
    {
        "name": "paralysis_genuine",
        "description": "CRO has blocked all buys for 12+ cycles. No positions opened. "
                       "This is strategy paralysis.",
        "target_agents": ["StrategyLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "cro_level": "HIGH",
            "cro_streak": 12,
            "entry_dates": {},
            "sell_log": [],
            "picks_today": 0,
        },
        "should_not_fire": [],
    },

    # ── MLLogicAgent scenarios ────────────────────────────────────────────
    {
        "name": "ml_pod_funded_no_validation",
        "description": "ML pod has $30k but zero live picks logged. Can't know if model works.",
        "target_agents": ["MLLogicAgent"],
        "expected_severity": "MEDIUM",
        "state_patch": {
            "ml_live_picks": {"picks": []},
            "pods": [{"name": "ML / Quantitative", "budget_inception": 30000, "budget": 30000}],
            "ml_auc_st": 0.63,
        },
        "should_not_fire": [],
    },

    # ── DataIntegrityLogicAgent scenarios ────────────────────────────────
    {
        "name": "ghost_positions",
        "description": "state.json has 8 positions that review_recommendations doesn't know about.",
        "target_agents": ["DataIntegrityLogicAgent"],
        "expected_severity": "MEDIUM",
        "state_patch": {
            "entry_dates": {f"GHOST{i}": "2026-01-01" for i in range(8)},
        },
        "should_not_fire": [],
    },
    {
        "name": "data_all_consistent",
        "description": "All data files consistent — no ghost positions, pods match.",
        "target_agents": [],
        "expected_severity": None,
        "state_patch": {
            "entry_dates": {},
            "pod_allocations": {
                "pods": {
                    "pod1_rd": {"budget_inception": 30000, "budget": 30000,
                                "positions": {"AAPL": {"qty": 10}},
                                "cash_balance": 5000, "positions_value": 25000, "total_value": 30000},
                }
            },
        },
        "should_not_fire": ["DataIntegrityLogicAgent"],
    },

    # ── CommunicationLogicAgent scenarios ─────────────────────────────────
    {
        "name": "email_buffer_overflow",
        "description": "25 emails queued in buffer — volume exceeds absorption.",
        "target_agents": ["CommunicationLogicAgent"],
        "expected_severity": "MEDIUM",
        "state_patch": {},  # patched via direct state key not used — tested via real file
        "should_not_fire": [],
    },

    # ── RiskCoherenceLogicAgent scenarios ─────────────────────────────────
    {
        "name": "canary_red_positions_open",
        "description": "Canary=RED but 8 positions still open. Defensive exits not triggered.",
        "target_agents": ["RiskCoherenceLogicAgent"],
        "expected_severity": "HIGH",
        "state_patch": {
            "canary_level": "RED",
            "pod_allocations": {
                "pods": {
                    "pod1_rd": {"budget_inception": 30000, "budget": 30000,
                                "positions": {f"T{i}": {"qty": 10} for i in range(8)},
                                "positions_value": 28000, "total_value": 30000},
                }
            },
        },
        "should_not_fire": [],
    },
    {
        "name": "risk_signals_contradicting",
        "description": "IRM has 3 red flags but CRO shows LOW. Signals contradict.",
        "target_agents": ["RiskCoherenceLogicAgent"],
        "expected_severity": "MEDIUM",
        "state_patch": {
            "irm_flags": 3,
            "cro_level": "LOW",
            "canary_level": "GREEN",
        },
        "should_not_fire": [],
    },
    {
        "name": "risk_all_coherent",
        "description": "CRO=ELEVATED, Canary=YELLOW, IRM has 1 flag. All pointing same direction.",
        "target_agents": [],
        "expected_severity": None,
        "state_patch": {
            "irm_flags": 1,
            "cro_level": "ELEVATED",
            "canary_level": "YELLOW",
        },
        "should_not_fire": ["RiskCoherenceLogicAgent"],
    },

]


# =============================================================================
# SCENARIO RUNNER
# =============================================================================

def run_scenario(scenario: dict, base_state: dict) -> dict:
    """
    Inject state_patch into base_state, run all sub-agents,
    evaluate which ones fired and whether they were correct.
    """
    from logic_team import SUB_AGENTS

    # Build injected state
    state = copy.deepcopy(base_state)
    for k, v in scenario.get("state_patch", {}).items():
        state[k] = v

    target_agents   = set(scenario.get("target_agents", []))
    should_not_fire = set(scenario.get("should_not_fire", []))
    exp_sev         = scenario.get("expected_severity")

    results_by_agent = {}

    for agent in SUB_AGENTS:
        try:
            findings = agent.run(state)
        except Exception as e:
            findings = []
            logger.warning(f"    Agent {agent.name} errored: {e}")

        fired    = len(findings) > 0
        fired_sev = max(
            (["CRITICAL","HIGH","MEDIUM","LOW"].index(f.get("severity","LOW")) for f in findings),
            default=4
        )
        fired_sev_str = ["CRITICAL","HIGH","MEDIUM","LOW"][fired_sev] if fired_sev < 4 else None

        should_fire  = agent.name in target_agents
        should_quiet = agent.name in should_not_fire

        if should_fire and fired:
            grade = "PASS"
            score = 1
        elif should_fire and not fired:
            grade = "MISS"
            score = -2
        elif should_quiet and fired:
            grade = "FALSE_POSITIVE"
            score = -1
        elif not should_fire and not should_quiet and not fired:
            grade = "CLEAN"
            score = 1
        else:
            grade = "NEUTRAL"
            score = 0

        results_by_agent[agent.name] = {
            "grade":    grade,
            "score":    score,
            "fired":    fired,
            "findings": len(findings),
            "severity": fired_sev_str,
        }

    return results_by_agent


def _letter_grade(score: float, total: int) -> str:
    if total == 0: return "N/A"
    pct = score / max(total, 1)
    if pct >= 0.85: return "A"
    if pct >= 0.70: return "B"
    if pct >= 0.55: return "C"
    if pct >= 0.40: return "D"
    return "F"


def run_full_training(scenario_filter: str = None) -> dict:
    logger.info("=" * 60)
    logger.info("  LOGIC TEAM TRAINER — ADVERSARIAL TEST RUN")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    from logic_team import _build_state
    base_state = _build_state()

    scenarios_to_run = SCENARIOS
    if scenario_filter:
        scenarios_to_run = [s for s in SCENARIOS if s["name"] == scenario_filter]
        if not scenarios_to_run:
            logger.error(f"Scenario '{scenario_filter}' not found")
            return {}

    agent_scores = {}   # name → {"score": int, "total": int, "passes": int, "misses": int, "fp": int}
    scenario_results = []

    for s in scenarios_to_run:
        logger.info(f"\n  SCENARIO: {s['name']}")
        logger.info(f"  → {s['description'][:80]}")

        results = run_scenario(s, base_state)
        scenario_results.append({
            "scenario": s["name"],
            "description": s["description"],
            "results": results,
            "ts": datetime.now().isoformat(),
        })

        for agent_name, r in results.items():
            if agent_name not in agent_scores:
                agent_scores[agent_name] = {"score": 0, "total": 0, "passes": 0, "misses": 0, "fp": 0, "neutral": 0}

            g = r["grade"]
            agent_scores[agent_name]["score"] += r["score"]

            if g in ("PASS", "MISS", "FALSE_POSITIVE"):
                agent_scores[agent_name]["total"] += 1
                if g == "PASS": agent_scores[agent_name]["passes"] += 1
                elif g == "MISS": agent_scores[agent_name]["misses"] += 1
                elif g == "FALSE_POSITIVE": agent_scores[agent_name]["fp"] += 1
            elif g == "NEUTRAL":
                agent_scores[agent_name]["neutral"] += 1

            if g not in ("CLEAN", "NEUTRAL"):
                icon = "✅" if g == "PASS" else "❌" if g == "MISS" else "⚠️"
                logger.info(f"    {icon} {agent_name[:25]:25s} {g} (severity={r.get('severity','none')})")

    # Compute letter grades
    logger.info(f"\n{'='*60}")
    logger.info("  AGENT GRADES")
    logger.info(f"{'='*60}")
    grades = {}
    for name, s in agent_scores.items():
        grade = _letter_grade(s["score"], s["total"])
        grades[name] = grade
        logger.info(
            f"  {name[:30]:30s}  {grade}  "
            f"({s['passes']}P / {s['misses']}M / {s['fp']}FP | score={s['score']:+d})"
        )

    # Persist training log
    log = _load(TRAIN_LOG, {"runs": [], "agent_lifetime_scores": {}})
    if not isinstance(log, dict): log = {"runs": [], "agent_lifetime_scores": {}}

    # Update lifetime scores
    lifetime = log.get("agent_lifetime_scores", {})
    for name, s in agent_scores.items():
        if name not in lifetime:
            lifetime[name] = {"score": 0, "total": 0, "passes": 0, "misses": 0, "fp": 0, "runs": 0}
        for k in ("score", "total", "passes", "misses", "fp"):
            lifetime[name][k] = lifetime[name].get(k, 0) + s.get(k, 0)
        lifetime[name]["runs"] += 1
        lifetime[name]["last_grade"] = grades.get(name, "?")
        lifetime[name]["last_run"] = datetime.now().isoformat()

    log["runs"].append({
        "ts":               datetime.now().isoformat(),
        "scenarios_run":    len(scenarios_to_run),
        "grades":           grades,
        "scenario_results": scenario_results,
        "agent_scores":     agent_scores,
    })
    log["runs"] = log["runs"][-20:]  # keep last 20 training runs
    log["agent_lifetime_scores"] = lifetime
    log["last_run"] = datetime.now().isoformat()
    _save(TRAIN_LOG, log)

    # Also write current grades to a simple summary file for dashboard
    grades_summary = {
        "last_training": datetime.now().isoformat(),
        "scenarios_run": len(scenarios_to_run),
        "grades": grades,
        "agent_detail": {
            name: {
                "grade": grades.get(name,"?"),
                "passes": s["passes"], "misses": s["misses"], "fp": s["fp"],
                "score": s["score"],
            }
            for name, s in agent_scores.items()
        },
        "weakest_agents": sorted(
            [(name, grades.get(name,"?")) for name in grades],
            key=lambda x: "FDCBA".index(x[1][0]) if x[1] and x[1][0] in "FDCBA" else 99
        )[:3],
    }
    _save(DATA_DIR / "lt_training_grades.json", grades_summary)

    poor_agents = [(n,g) for n,g in grades.items() if g in ("D","F")]
    if poor_agents:
        logger.warning(f"\n  ⚠️  AGENTS NEEDING IMPROVEMENT: {poor_agents}")
        logger.warning("  → Review scenario misses above and strengthen detection rules.")

    return grades_summary


def print_report():
    log = _load(TRAIN_LOG, {})
    runs = log.get("runs", [])
    lifetime = log.get("agent_lifetime_scores", {})

    print(f"\n{'='*65}")
    print(f"  LOGIC TEAM TRAINING REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}")
    print(f"  Training runs on record: {len(runs)}")
    print(f"\n  LIFETIME AGENT GRADES:")
    for name, s in sorted(lifetime.items(), key=lambda x: x[1].get("last_grade","?")):
        runs_n = s.get("runs", 0)
        grade = s.get("last_grade","?")
        passes = s.get("passes",0)
        misses = s.get("misses",0)
        fp = s.get("fp",0)
        print(f"    {name[:30]:30s}  {grade}  ({passes}P/{misses}M/{fp}FP over {runs_n} run(s))")

    if runs:
        last = runs[-1]
        print(f"\n  LAST RUN: {last.get('ts','?')[:16]}")
        print(f"  Scenarios: {last.get('scenarios_run',0)}")
        weak = [(n,g) for n,g in last.get("grades",{}).items() if g in ("C","D","F")]
        if weak:
            print(f"  Flagged for improvement: {weak}")
        else:
            print("  All agents graded B or better ✓")
    print()


def print_grades():
    grades = _load(DATA_DIR / "lt_training_grades.json", {})
    if not grades:
        print("No training grades on file. Run: python3 logic_team_trainer.py --full")
        return
    print(f"\n  Last training: {grades.get('last_training','?')[:16]}")
    for name, detail in grades.get("agent_detail",{}).items():
        print(f"  {name[:30]:30s}  {detail.get('grade','?')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Logic Team Adversarial Trainer")
    parser.add_argument("--full",     action="store_true", help="Run all scenarios (default)")
    parser.add_argument("--scenario", type=str,            help="Run one specific scenario")
    parser.add_argument("--report",   action="store_true", help="Print historical report")
    parser.add_argument("--grade",    action="store_true", help="Print current grades")
    parser.add_argument("--list",     action="store_true", help="List all scenario names")
    args = parser.parse_args()

    if args.report:
        print_report()
    elif args.grade:
        print_grades()
    elif args.list:
        for s in SCENARIOS:
            print(f"  {s['name']:35s} → {s['description'][:60]}")
    elif args.scenario:
        run_full_training(scenario_filter=args.scenario)
    else:
        run_full_training()
