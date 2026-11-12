#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — LESSON LIBRARY PRE-GENERATOR
# =============================================================================
# Chairman directive 2026-06-12: Fable 5 access expires June 22. Pre-generate
# all 90 days of LLM-written lesson content NOW so Jack keeps getting
# Fable-quality lessons after expiry.
#
# Writes data/lessons_library.json: {day: {topic, content, generated_at, model}}
# teaching_division.prepare_session() reads this library first and only calls
# the live LLM if the day is missing.
#
# Run in batches to spread credit spend:
#   python3 pregenerate_lessons.py --batch 20    # generate next 20 missing days
#   python3 pregenerate_lessons.py --all         # generate everything missing
# Cron (nightly until complete, removes itself from the work when done).
# =============================================================================

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("lesson_pregen")

DATA_DIR     = Path(__file__).parent / "data"
LIBRARY_PATH = DATA_DIR / "lessons_library.json"


def _load_library() -> dict:
    try:
        if LIBRARY_PATH.exists():
            return json.loads(LIBRARY_PATH.read_text())
    except Exception:
        pass
    return {}


def generate(batch: int) -> dict:
    from teaching_division import TeachingCurriculumDirector
    from apex_llm import reason, is_available, get_model

    if not is_available("teaching"):
        logger.error("LLM not available — check API key / config.")
        return {}

    director = TeachingCurriculumDirector()
    library  = _load_library()

    missing = [d for d in range(1, 91) if str(d) not in library]
    if not missing:
        logger.info("Library complete — all 90 lessons generated. Nothing to do.")
        return library

    todo = missing[:batch]
    logger.info("Library has %d/90 lessons. Generating %d more (days %s–%s)…",
                90 - len(missing), len(todo), todo[0], todo[-1])

    for day in todo:
        lesson = director.get_lesson_for_day(day)
        if not lesson:
            library[str(day)] = {"topic": f"Day {day}", "content": None,
                                 "error": "no curriculum entry"}
            continue

        raw = reason(
            "You are a patient finance teacher writing a daily mini-lesson for "
            "Jack Beard, Co-Chairman of Apex Capital (an AI paper-trading firm with "
            "$100k AUM, 5 pods, 13F-driven research), who is learning investing from "
            "scratch. Concrete examples over jargon. Where natural, connect the "
            "concept to how his own firm uses it.",
            f"Day {day} of 90. Topic: {lesson['topic']}\n"
            f"Key concepts: {', '.join(lesson.get('key_concepts', [])[:5])}\n"
            f"Teacher persona: {lesson.get('teacher', 'FinanceFundamentals')}\n"
            "Write the lesson: ~300 words of core teaching, then one worked "
            "numerical example, then one 'how Apex Capital uses this' paragraph, "
            "then 2 self-check questions (with answers below them, upside-down "
            "style is fine as 'Answer: ...').",
            agent_name="teaching",
            max_tokens=900,
        )
        if raw:
            library[str(day)] = {
                "day":          day,
                "topic":        lesson["topic"],
                "teacher":      lesson.get("teacher", ""),
                "key_concepts": lesson.get("key_concepts", []),
                "content":      raw.strip(),
                "model":        get_model(),
                "generated_at": datetime.now().isoformat(),
            }
            logger.info("  Day %2d ✓ %s (%d chars)", day, lesson["topic"][:50], len(raw))
        else:
            logger.warning("  Day %2d ✗ %s — LLM returned nothing, will retry next run",
                           day, lesson["topic"][:50])

        # Save after every lesson so progress survives interruption
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LIBRARY_PATH.write_text(json.dumps(library, indent=2))
        time.sleep(1)   # gentle pacing

    done = sum(1 for v in library.values() if v.get("content"))
    logger.info("Library now %d/90 complete → %s", done, LIBRARY_PATH)
    return library


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--all",   action="store_true")
    args = ap.parse_args()
    generate(batch=90 if args.all else args.batch)
