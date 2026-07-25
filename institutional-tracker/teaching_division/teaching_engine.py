#!/usr/bin/env python3
"""
teaching_engine.py — Daily 30-Minute Session Runner
====================================================
Called by cron daily at 9:00 AM ET (13:00 UTC).
Orchestrates the full teaching session:
  1. CLO picks today's module
  2. Specialist tutor delivers lesson content
  3. Quiz Master generates and emails quiz
  4. Progress updated

Also callable on-demand: python3 teaching_engine.py --demo
"""

import json, logging, os, sys, subprocess
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(name)s -- %(message)s")
logger = logging.getLogger("TeachingEngine")

BASE_DIR = Path(__file__).parent.parent
TEACHING_DIR = BASE_DIR / "teaching_division"
DATA_DIR = BASE_DIR / "data"
PYTHON = str(BASE_DIR.parent / "venv" / "bin" / "python3")


def run_session():
    """Run today's full 30-minute teaching session."""
    logger.info("=" * 60)
    logger.info("  APEX ACADEMY — DAILY TEACHING SESSION")
    logger.info(f"  {datetime.now(timezone.utc).strftime('%A %B %d, %Y  %H:%M UTC')}")
    logger.info("=" * 60)
    
    # Step 1: CLO orchestrates and picks module
    logger.info("  Step 1/3: Chief Learning Officer — selecting module...")
    r = subprocess.run(
        [PYTHON, str(TEACHING_DIR / "chief_learning_officer.py")],
        capture_output=True, text=True, timeout=120, cwd=str(BASE_DIR)
    )
    if r.returncode != 0:
        logger.warning(f"  CLO error: {r.stderr[:200]}")
    
    # Step 2: Specialist tutor delivers lesson
    logger.info("  Step 2/3: Specialist Tutor — delivering lesson...")
    lesson_path = DATA_DIR / "todays_lesson.json"
    if lesson_path.exists():
        with open(lesson_path) as f:
            lesson = json.load(f)
        specialist = lesson.get("specialist", "finance_fundamentals_tutor")
        specialist_script = str(TEACHING_DIR / f"{specialist}.py")
        if os.path.exists(specialist_script):
            r2 = subprocess.run(
                [PYTHON, specialist_script, "--lesson", str(lesson_path)],
                capture_output=True, text=True, timeout=120, cwd=str(BASE_DIR)
            )
            if r2.returncode == 0:
                logger.info(f"  Lesson delivered by {specialist}")
            else:
                logger.warning(f"  Tutor error: {r2.stderr[:200]}")
    
    # Step 3: Quiz Master generates and emails quiz
    logger.info("  Step 3/3: Quiz Master — generating quiz...")
    lesson_content_path = DATA_DIR / "todays_lesson_content.json"
    module_id = "F1"
    if lesson_content_path.exists():
        with open(lesson_content_path) as f:
            content = json.load(f)
        module_id = content.get("module_id", "F1")
    
    r3 = subprocess.run(
        [PYTHON, str(TEACHING_DIR / "quiz_master.py"), "--module", module_id],
        capture_output=True, text=True, timeout=120, cwd=str(BASE_DIR)
    )
    if r3.returncode == 0:
        logger.info("  Quiz generated and delivered")
    else:
        logger.warning(f"  Quiz error: {r3.stderr[:200]}")
    
    # Email lesson content to student
    try:
        sys.path.insert(0, str(BASE_DIR))
        from email_buffer import buffer_email
        
        lesson_content = {}
        if lesson_content_path.exists():
            with open(lesson_content_path) as f:
                lesson_content = json.load(f)
        
        sections = lesson_content.get("sections", {})
        body = f"""APEX ACADEMY — Today's Lesson
{'='*55}
Module: {lesson_content.get('module_title', 'TBD')}
Track: {lesson_content.get('track_name', 'TBD')}
Specialist: {lesson_content.get('specialist', 'TBD')}
{'='*55}

🎯 WHY THIS MATTERS:
{sections.get('hook', '')}

📚 CORE CONCEPT (15 min):
{sections.get('core_concept', '')}

🔍 DEEP DIVE (8 min):
{sections.get('deep_dive', '')}

💼 REAL DEAL (3 min):
{sections.get('real_deal', '')}

✍️ QUIZ PREVIEW — Study these:
{sections.get('quiz_preview', '')}
"""
        subject = f"📚 Apex Academy — {lesson_content.get('module_title', 'Daily Lesson')}"
        buffer_email(subject, body, sender_name="Apex Academy")
        logger.info(f"  Lesson emailed to Jack")
    except Exception as e:
        logger.debug(f"  Email non-fatal: {e}")
    
    # Load progress for status
    progress_path = DATA_DIR / "student_progress.json"
    if progress_path.exists():
        with open(progress_path) as f:
            progress = json.load(f)
        logger.info(f"\n  📊 Progress Summary:")
        logger.info(f"     Sessions completed: {progress.get('total_sessions', 0)}")
        logger.info(f"     Current module: {progress.get('current_module', 'F1')}")
        logger.info(f"     Streak: {progress.get('streak_days', 0)} days")
        completed = progress.get("completed_modules", [])
        logger.info(f"     Modules completed: {len(completed)}/32")
        weak = progress.get("weak_topics", [])
        if weak:
            logger.info(f"     Weak topics: {[m for m,s in weak[:3]]}")
    
    logger.info("\n  ✅ APEX ACADEMY SESSION COMPLETE")
    logger.info(f"  Quiz check your email! Reply with answers (A,B,C,D,B format)")


def demo_lesson(module_id: str = "F1"):
    """Demo a specific lesson without updating progress."""
    logger.info(f"  DEMO MODE: Loading lesson for {module_id}")
    
    # Map module to specialist
    module_to_specialist = {}
    curr_path = BASE_DIR / "teaching_division" / "curriculum.json"
    if curr_path.exists():
        with open(curr_path) as f:
            curriculum = json.load(f)
        for track_id, track in curriculum.get("tracks", {}).items():
            specialist_map = {
                "foundations": "finance_fundamentals_tutor",
                "valuation": "ib_tutor",
                "investment_banking": "ib_tutor",
                "markets": "investment_tutor",
            }
            for mod in track.get("modules", []):
                module_to_specialist[mod["id"]] = specialist_map.get(track_id, "finance_fundamentals_tutor")
    
    specialist = module_to_specialist.get(module_id, "finance_fundamentals_tutor")
    specialist_script = str(TEACHING_DIR / f"{specialist}.py")
    
    demo_brief = {
        "module_id": module_id,
        "module_title": f"Module {module_id}",
        "specialist": specialist,
        "session_type": "demo",
        "student_name": "Jack",
        "mastery_before": 0,
        "target_mastery": 80,
        "daily_minutes": 30,
    }
    
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(demo_brief, f)
    os.replace(tmp, str(DATA_DIR / "todays_lesson.json"))
    
    if os.path.exists(specialist_script):
        subprocess.run([PYTHON, specialist_script, "--lesson",
                       str(DATA_DIR / "todays_lesson.json")],
                      cwd=str(BASE_DIR))
    
    # Show lesson content
    content_path = DATA_DIR / "todays_lesson_content.json"
    if content_path.exists():
        with open(content_path) as f:
            content = json.load(f)
        print(f"\n{'='*60}")
        print(f"LESSON: {content.get('module_title')}")
        print(f"{'='*60}")
        for section, text in content.get("sections", {}).items():
            if text:
                print(f"\n[{section.upper().replace('_', ' ')}]")
                print(text)
    
    # Generate quiz
    subprocess.run([PYTHON, str(TEACHING_DIR / "quiz_master.py"), "--module", module_id],
                  cwd=str(BASE_DIR))
    
    quiz_path = DATA_DIR / "todays_quiz.json"
    if quiz_path.exists():
        with open(quiz_path) as f:
            quiz = json.load(f)
        print(f"\n{'='*60}")
        print("QUIZ — Answer these:")
        print(f"{'='*60}")
        for i, q in enumerate(quiz.get("questions", []), 1):
            print(f"\nQ{i}: {q['q']}")
            for choice in q["choices"]:
                print(f"  {choice}")
        print(f"\n[Reply with: A,B,C,D,B format]")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", type=str, help="Demo a specific module ID (e.g. F1, V1, M7)")
    args = parser.parse_args()
    
    if args.demo:
        demo_lesson(args.demo)
    else:
        run_session()
