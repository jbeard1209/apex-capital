#!/usr/bin/env python3
"""
chief_learning_officer.py — Teaching Division Orchestrator
===========================================================
The CLO is the brain of the Teaching Division. It:
  1. Reads student_progress.json to understand current mastery levels
  2. Decides today's 30-minute lesson based on:
     - Current module sequence
     - Weak topics needing re-emphasis
     - Spaced repetition queue (topics due for review)
  3. Dispatches to the appropriate specialist tutor
  4. After the session, reads quiz results and updates progress
  5. Sends daily briefing email to student

Runs daily via cron: 9:00 AM ET (13:00 UTC)
Also callable on-demand: python3 chief_learning_officer.py --session

The CLO adapts to the student's pace. If mastery < 60%, it:
  - Re-schedules the topic for tomorrow
  - Requests extra examples from the tutor
  - Flags for parent-topic review if fundamentals are weak
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("CLO")

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
TEACHING_DIR = BASE_DIR / "teaching_division"
PROGRESS_PATH = DATA_DIR / "student_progress.json"
CURRICULUM_PATH = TEACHING_DIR / "curriculum.json"
SESSION_LOG_PATH = DATA_DIR / "teaching_session_log.json"
TODAYS_LESSON_PATH = DATA_DIR / "todays_lesson.json"


def _load_json(path, default):
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Load error: {e}")
    return default


def _save_json(path, data):
    import tempfile
    Path(path).parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(Path(path).parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, str(path))


def select_todays_module(progress: dict, curriculum: dict) -> dict:
    """
    Decide what to teach today based on:
    - Spaced repetition queue (highest priority — don't skip reviews)
    - Current module (if mastery < mastery_target, repeat)
    - Next module in sequence
    """
    mastery = progress.get("mastery_scores", {})
    sr_queue = progress.get("spaced_repetition_queue", [])
    current = progress.get("current_module", "F1")
    
    # Check spaced repetition queue for due items
    now = datetime.now(timezone.utc)
    due_reviews = []
    new_sr_queue = []
    for item in sr_queue:
        due_dt = datetime.fromisoformat(item["due_date"])
        if due_dt <= now:
            due_reviews.append(item)
        else:
            new_sr_queue.append(item)
    progress["spaced_repetition_queue"] = new_sr_queue
    
    if due_reviews:
        # Pick highest-priority review (lowest mastery)
        due_reviews.sort(key=lambda x: x.get("mastery", 50))
        review = due_reviews[0]
        # Re-queue others
        progress["spaced_repetition_queue"].extend(due_reviews[1:])
        logger.info(f"  Spaced repetition: reviewing {review['module_id']} "
                    f"(mastery: {review.get('mastery', 50)}%)")
        return {"module_id": review["module_id"], "type": "review",
                "mastery_before": review.get("mastery", 50)}
    
    # Check if current module needs repeating (mastery below threshold)
    all_modules = {}
    tracks = curriculum.get("tracks", {})
    for track_id, track in tracks.items():
        for mod in track.get("modules", []):
            all_modules[mod["id"]] = {**mod, "track": track_id, "track_name": track["name"]}
    
    current_mod = all_modules.get(current, {})
    current_mastery = mastery.get(current, 0)
    target = current_mod.get("mastery_target", 80)
    
    if current_mastery > 0 and current_mastery < target:
        logger.info(f"  Repeating {current}: mastery {current_mastery}% < target {target}%")
        return {"module_id": current, "type": "repeat",
                "mastery_before": current_mastery, "target": target}
    
    # Progress to next module
    sequence = curriculum.get("sequence", [])
    try:
        current_idx = sequence.index(current)
        if current_mastery >= target or current_mastery == 0:
            if current_idx + 1 < len(sequence):
                next_id = sequence[current_idx + 1]
                if current_mastery >= target and current not in progress.get("completed_modules", []):
                    progress.setdefault("completed_modules", []).append(current)
                progress["current_module"] = next_id
                logger.info(f"  Advancing to {next_id}")
                return {"module_id": next_id, "type": "new",
                        "mastery_before": mastery.get(next_id, 0)}
            else:
                logger.info("  All modules complete! Switching to mastery reinforcement.")
                # Find lowest-mastery module for reinforcement
                lowest = min(sequence, key=lambda m: mastery.get(m, 0))
                return {"module_id": lowest, "type": "reinforcement",
                        "mastery_before": mastery.get(lowest, 0)}
    except ValueError:
        return {"module_id": current, "type": "new", "mastery_before": 0}
    
    return {"module_id": current, "type": "new", "mastery_before": 0}


def build_lesson_brief(module_selection: dict, curriculum: dict, progress: dict) -> dict:
    """Build the lesson brief that gets passed to the specialist tutor."""
    module_id = module_selection["module_id"]
    
    # Find module details
    all_modules = {}
    tracks = curriculum.get("tracks", {})
    for track_id, track in tracks.items():
        for mod in track.get("modules", []):
            all_modules[mod["id"]] = {**mod, "track": track_id, "track_name": track["name"]}
    
    mod = all_modules.get(module_id, {"title": module_id, "track": "unknown"})
    
    # Determine specialist
    track = mod.get("track", "foundations")
    specialist_map = {
        "foundations": "finance_fundamentals_tutor",
        "valuation": "ib_tutor",
        "investment_banking": "ib_tutor",
        "markets": "investment_tutor",
    }
    specialist = specialist_map.get(track, "finance_fundamentals_tutor")
    
    # Identify weak adjacent topics
    mastery = progress.get("mastery_scores", {})
    sequence = curriculum.get("sequence", [])
    weak_topics = [(m, mastery.get(m, 0)) for m in sequence if mastery.get(m, 0) < 70]
    weak_topics.sort(key=lambda x: x[1])
    
    # Real-world hook from portfolio
    portfolio_hooks = {
        "F1": "We'll look at the three statements through the lens of companies we hold: GS, LLY, AVGO",
        "F2": "GS's income statement — how a bank's revenue works differently from a tech company",
        "F5": "We track Herfindahl index (0.52) for our portfolio — today you'll understand why it matters",
        "V1": "DCF is how we value LLY — you'll build one from scratch",
        "M5": "Apex Capital uses Sharpe ratio (AUC 0.5804) as our ML target — you'll learn why today",
        "M7": "You're already running a fund that reads 13F filings — today is your deep dive",
        "IB1": "BA (Boeing) was in our portfolio — we'll analyze it like an M&A target",
    }
    
    return {
        "module_id": module_id,
        "module_title": mod.get("title", module_id),
        "track": track,
        "track_name": mod.get("track_name", track),
        "specialist": specialist,
        "session_type": module_selection["type"],
        "mastery_before": module_selection.get("mastery_before", 0),
        "target_mastery": mod.get("mastery_target", 80),
        "daily_minutes": curriculum.get("daily_minutes", 30),
        "weak_topics": [(m, s) for m, s in weak_topics[:3]],
        "portfolio_hook": portfolio_hooks.get(module_id, ""),
        "student_name": progress.get("student", "Jack"),
        "total_sessions": progress.get("total_sessions", 0),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def update_progress_after_session(progress: dict, quiz_result: dict, lesson: dict,
                                   curriculum: dict) -> None:
    """Update mastery scores and spaced repetition queue based on quiz performance."""
    module_id = lesson.get("module_id")
    score = quiz_result.get("score_pct", 0)
    
    # Update mastery score (weighted average with history)
    old_mastery = progress.get("mastery_scores", {}).get(module_id, 0)
    # New score weighted 60%, history 40%
    new_mastery = round(0.6 * score + 0.4 * old_mastery) if old_mastery > 0 else score
    progress.setdefault("mastery_scores", {})[module_id] = new_mastery
    
    # Add to spaced repetition queue
    sr_intervals = curriculum.get("spaced_repetition_days", [1, 3, 7, 14, 30])
    now = datetime.now(timezone.utc)
    
    if score < 80:
        # Poor score: review tomorrow
        interval = sr_intervals[0]
        progress.setdefault("spaced_repetition_queue", []).append({
            "module_id": module_id,
            "due_date": (now + timedelta(days=interval)).isoformat(),
            "mastery": new_mastery,
            "reason": f"Score {score}% below 80% threshold",
        })
    elif score >= 80 and score < 95:
        # Good score: review in 3 days
        interval = sr_intervals[1] if len(sr_intervals) > 1 else 3
        progress.setdefault("spaced_repetition_queue", []).append({
            "module_id": module_id,
            "due_date": (now + timedelta(days=interval)).isoformat(),
            "mastery": new_mastery,
            "reason": "Scheduled review",
        })
    # 95%+: mastered — review in 7 days only
    else:
        interval = sr_intervals[2] if len(sr_intervals) > 2 else 7
        progress.setdefault("spaced_repetition_queue", []).append({
            "module_id": module_id,
            "due_date": (now + timedelta(days=interval)).isoformat(),
            "mastery": new_mastery,
            "reason": "Long-term retention check",
        })
    
    # Update weak topics list
    progress["weak_topics"] = sorted(
        [(m, s) for m, s in progress.get("mastery_scores", {}).items() if s < 70],
        key=lambda x: x[1]
    )[:5]
    
    progress["total_sessions"] = progress.get("total_sessions", 0) + 1
    progress["last_session"] = now.isoformat()
    progress["streak_days"] = progress.get("streak_days", 0) + 1


def run_daily_clo_session():
    """Main entry point — orchestrate today's learning session."""
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — CHIEF LEARNING OFFICER")
    logger.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info("=" * 60)
    
    progress = _load_json(PROGRESS_PATH, {
        "student": "Jack Beard",
        "current_module": "F1",
        "mastery_scores": {},
        "completed_modules": [],
        "spaced_repetition_queue": [],
        "streak_days": 0,
        "total_sessions": 0,
    })
    curriculum = _load_json(CURRICULUM_PATH, {"tracks": {}, "sequence": []})
    
    # Select today's module
    module_selection = select_todays_module(progress, curriculum)
    lesson = build_lesson_brief(module_selection, curriculum, progress)
    
    logger.info(f"  Today's module: {lesson['module_id']} — {lesson['module_title']}")
    logger.info(f"  Specialist: {lesson['specialist']}")
    logger.info(f"  Session type: {lesson['session_type']}")
    logger.info(f"  Mastery before: {lesson['mastery_before']}%")
    
    # Save lesson brief for the specialist to pick up
    _save_json(TODAYS_LESSON_PATH, lesson)
    
    # Dispatch to specialist tutor
    specialist_script = str(TEACHING_DIR / f"{lesson['specialist']}.py")
    if os.path.exists(specialist_script):
        import subprocess
        logger.info(f"  Dispatching to {lesson['specialist']}...")
        result = subprocess.run(
            [sys.executable, specialist_script, "--lesson", str(TODAYS_LESSON_PATH)],
            capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR)
        )
        if result.returncode == 0:
            logger.info("  Lesson delivered successfully")
        else:
            logger.warning(f"  Specialist error: {result.stderr[:200]}")
    else:
        logger.warning(f"  Specialist not found: {specialist_script}")
    
    # Check for quiz results (written by quiz_master.py)
    quiz_path = DATA_DIR / "latest_quiz_result.json"
    if Path(quiz_path).exists():
        quiz_result = _load_json(quiz_path, {})
        update_progress_after_session(progress, quiz_result, lesson, curriculum)
        logger.info(f"  Quiz score: {quiz_result.get('score_pct', 0)}% | "
                    f"New mastery: {progress['mastery_scores'].get(lesson['module_id'], 0)}%")
    
    _save_json(PROGRESS_PATH, progress)
    
    # Log session
    sessions = _load_json(SESSION_LOG_PATH, [])
    sessions.append({
        "date": datetime.now(timezone.utc).isoformat()[:10],
        "module_id": lesson["module_id"],
        "module_title": lesson["module_title"],
        "session_type": lesson["session_type"],
        "mastery_before": lesson["mastery_before"],
        "mastery_after": progress["mastery_scores"].get(lesson["module_id"], 0),
    })
    _save_json(SESSION_LOG_PATH, sessions[-90:])  # keep 90 days
    
    logger.info(f"  Total sessions: {progress['total_sessions']} | "
                f"Streak: {progress['streak_days']} days")
    logger.info("  CLO session complete")


if __name__ == "__main__":
    run_daily_clo_session()
