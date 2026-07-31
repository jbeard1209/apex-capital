#!/usr/bin/env python3
"""
weekly_brainstorm.py — Weekly CIT + Claude Improvement Brainstorm
=================================================================
Every Monday 8 AM, generates a new improvement brainstorm document
covering ML, comms, risk, new agents, infrastructure, and strategic ideas.

Sources of ideas:
1. CIT backlog (unimplemented proposals from cit_backlog.json)
2. Alpha decay report (features losing predictive power)
3. ML experiment results (FAILED ideas that might be revisited with a twist)
4. Agent status registry (stale/degraded agents = improvement opportunities)
5. Recent log analysis (recurring errors = friction points)
6. Systematic scan of each pod's recent reports

Output: data/weekly_brainstorm_{date}.json + saved to ~/Claude investment strategy/
Delivers: posts as a board bulletin + prints to CoS report for user review.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("weekly_brainstorm")

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
LOCAL_DIR = os.path.expanduser("~/Claude investment strategy")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except: pass
        raise


def gather_ml_ideas() -> list[dict]:
    """Generate ML improvement ideas from experiment results and alpha decay."""
    ideas = []
    
    # Failed experiments that might be revisited. NOT ml_improvement_log.json —
    # that filename is ml_live_test_runner.py's live-pick-evaluation log (a
    # different schema); this wants ml_improvement_loop.py's idea-testing log.
    # The two used to collide on one filename — see ml_improvement_loop.py's
    # LOG_FILE comment.
    ml_log = _load_json(os.path.join(DATA_DIR, "ml_idea_experiment_log.json"), [])
    for entry in ml_log:
        if entry.get("st_result") == "FAILED" and entry.get("lt_result") == "FAILED":
            # Check if it was close to threshold
            st_auc = entry.get("st_auc", 0) or 0
            lt_auc = entry.get("lt_auc", 0) or 0
            if st_auc > 0.52 or lt_auc > 0.51:
                ideas.append({
                    "category": "ML Models",
                    "priority": "Medium",
                    "title": f"Revisit idea '{entry['name']}' with modified hyperparameters",
                    "description": f"Idea '{entry['name']}' scored ST={st_auc:.4f}/LT={lt_auc:.4f} — close to threshold. Consider: (a) sector-relative version, (b) different time window, (c) z-score normalization.",
                    "pod": "ML Pod",
                    "effort": "medium",
                })

    # Alpha decay
    alpha_decay = _load_json(os.path.join(DATA_DIR, "alpha_decay_report.json"), {})
    decaying = [f for f, v in alpha_decay.items() if isinstance(v, dict) and v.get("decaying")]
    if decaying:
        ideas.append({
            "category": "ML Models",
            "priority": "High",
            "title": f"Retire/replace decaying features: {', '.join(decaying[:3])}",
            "description": f"{len(decaying)} features show alpha decay. Run alpha_decay_report and auto-retire features below threshold.",
            "pod": "ML Pod",
            "effort": "medium",
        })

    return ideas


def gather_infra_ideas() -> list[dict]:
    """Generate infrastructure improvement ideas from log analysis."""
    import glob
    ideas = []
    
    # Check disk usage
    try:
        import shutil
        usage = shutil.disk_usage("/")
        pct = usage.used / usage.total * 100
        if pct > 80:
            ideas.append({
                "category": "Infrastructure",
                "priority": "High" if pct > 90 else "Medium",
                "title": f"Disk usage at {pct:.0f}% — optimize storage",
                "description": f"Disk at {pct:.0f}% ({usage.used/1e9:.1f}GB/{usage.total/1e9:.1f}GB). Consider: (a) archive old model checkpoints, (b) compress price_cache.pkl, (c) upgrade disk.",
                "pod": "CIT Pod",
                "effort": "quick",
            })
    except Exception:
        pass

    # Check RAM pressure
    try:
        with open("/proc/meminfo") as f:
            meminfo = dict(line.split(":") for line in f)
        available_mb = int(meminfo.get("MemAvailable", "0 kB").strip().split()[0]) / 1024
        if available_mb < 500:
            ideas.append({
                "category": "Infrastructure",
                "priority": "High",
                "title": f"RAM pressure: only {available_mb:.0f}MB available",
                "description": "Low free memory risks OOM kills. Consider: (a) upgrade VM to n1-standard-2 (7.5GB), (b) schedule ML jobs sequentially with lock file, (c) add swap.",
                "pod": "CIT Pod",
                "effort": "quick",
            })
    except Exception:
        pass

    # Check for stale agents
    try:
        sys.path.insert(0, BASE_DIR)
        from agent_meeting import get_stale_agents
        stale = get_stale_agents(threshold_hours=26.0)
        if stale:
            ideas.append({
                "category": "Agent Communication",
                "priority": "Medium",
                "title": f"Reactivate stale agents: {', '.join(stale[:3])}",
                "description": f"{len(stale)} agents haven't updated in >26h: {', '.join(stale)}. Check crontab and scripts for errors.",
                "pod": "CIT Pod",
                "effort": "quick",
            })
    except Exception:
        pass

    return ideas


def gather_risk_ideas() -> list[dict]:
    """Generate risk management ideas from recent alerts and state."""
    ideas = []
    
    # Check circuit breaker history
    cb = _load_json(os.path.join(DATA_DIR, "circuit_breaker_state.json"), {})
    if cb.get("active"):
        ideas.append({
            "category": "Risk Management",
            "priority": "Critical",
            "title": "ACTIVE: Circuit breaker requires Board vote to clear",
            "description": f"Circuit breaker activated {cb.get('activated_at','')} due to {cb.get('drawdown_pct','?')}% drawdown. Schedule emergency board session.",
            "pod": "Risk Pod",
            "effort": "urgent",
        })

    # Check position risk log
    risk_log = _load_json(os.path.join(DATA_DIR, "position_risk_log.json"), [])
    if risk_log:
        recent_breaches = [e for e in risk_log[-10:] if e.get("breaches")]
        if recent_breaches:
            breach_tickers = list(set(b["ticker"] for e in recent_breaches for b in e["breaches"]))
            ideas.append({
                "category": "Risk Management",
                "priority": "High",
                "title": f"Review stop-loss breaches: {', '.join(breach_tickers[:3])}",
                "description": f"Recurring stop-loss breaches in {len(breach_tickers)} position(s). Consider reducing position size or tightening thresholds.",
                "pod": "Risk Pod",
                "effort": "quick",
            })

    return ideas


def generate_brainstorm() -> dict:
    """Generate the weekly brainstorm document."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    all_ideas = []
    all_ideas.extend(gather_ml_ideas())
    all_ideas.extend(gather_infra_ideas())
    all_ideas.extend(gather_risk_ideas())
    
    # Always include some standing ideas as discovery prompts
    all_ideas.append({
        "category": "New Agents",
        "priority": "Medium",
        "title": "Consider: cross-pod signal correlation tracker",
        "description": "Track when ML, FI, FE, and R&D all agree on a ticker — triple-pod consensus has historically been the strongest signal. A simple counter in alt_data_signals.json would enable this.",
        "pod": "CIT Pod",
        "effort": "medium",
    })
    
    document = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date":         date_str,
        "title":        f"Weekly Improvement Brainstorm — {date_str}",
        "idea_count":   len(all_ideas),
        "ideas":        all_ideas,
        "generated_by": "weekly_brainstorm.py (CIT + automated analysis)",
        "sections":     list(set(i["category"] for i in all_ideas)),
    }
    
    return document


def format_brainstorm_md(doc: dict) -> str:
    """Format as a markdown document for user presentation."""
    lines = [
        f"# Apex Capital — Weekly Improvement Brainstorm",
        f"*Generated {doc['date']} by CIT Team + Claude*",
        "",
        "---",
        "",
    ]
    
    by_category = {}
    for idea in doc["ideas"]:
        cat = idea["category"]
        by_category.setdefault(cat, []).append(idea)
    
    idea_num = 1
    for cat, cat_ideas in sorted(by_category.items()):
        lines.append(f"## {cat.upper()}")
        lines.append("")
        for idea in cat_ideas:
            pri = idea.get("priority","Medium")
            lines.append(f"**{idea_num}. {idea['title']}**")
            lines.append(f"*Pod: {idea['pod']} | Priority: {pri} | Effort: {idea.get('effort','medium')}*")
            lines.append(f"{idea['description']}")
            lines.append("")
            idea_num += 1
    
    lines.append("---")
    lines.append(f"*Total: {len(doc['ideas'])} ideas | Auto-generated weekly · Next brainstorm: Monday 8 AM*")
    return "\n".join(lines)


def run_weekly_brainstorm():
    """Main entry point."""
    logger.info("Generating weekly improvement brainstorm...")
    
    doc = generate_brainstorm()
    date_str = doc["date"]
    
    # Save to GCP data dir
    output_path = os.path.join(DATA_DIR, f"weekly_brainstorm_{date_str}.json")
    _save_json(output_path, doc)
    logger.info(f"  Saved JSON: {output_path}")
    
    # Save markdown locally (if ~/Claude investment strategy exists)
    md_content = format_brainstorm_md(doc)
    if os.path.exists(LOCAL_DIR):
        md_path = os.path.join(LOCAL_DIR, f"IMPROVEMENT_IDEAS_{date_str.replace('-','_')}.md")
        with open(md_path, "w") as f:
            f.write(md_content)
        logger.info(f"  Saved markdown: {md_path}")
    
    # Post as board bulletin
    try:
        sys.path.insert(0, BASE_DIR)
        from agent_meeting import post_bulletin
        summary = f"Weekly Brainstorm Ready: {len(doc['ideas'])} improvement ideas across {len(doc['sections'])} categories.\nTop priority: {next((i['title'] for i in doc['ideas'] if i['priority'] in ('High','Critical')), 'See report')}"
        post_bulletin(
            author    = "CITDirector",
            topic     = "ops",
            title     = f"Weekly Improvement Brainstorm — {date_str}",
            body      = summary,
            urgency   = "NORMAL",
            ttl_hours = 168.0,
        )
    except Exception as e:
        logger.debug(f"  Bulletin post error: {e}")
    
    logger.info(f"  Brainstorm complete: {len(doc['ideas'])} ideas")
    return doc


if __name__ == "__main__":
    if "--inbox-only" in sys.argv:
        from agent_inbox import main_inbox_only
        main_inbox_only("CITDirector")
        sys.exit(0)
    result = run_weekly_brainstorm()
    print(f"\nGenerated {result['idea_count']} ideas in {len(result['sections'])} categories")
    if result.get("ideas"):
        print("\nTop ideas:")
        for idea in result["ideas"][:3]:
            print(f"  [{idea['priority']}] {idea['title']}")
