#!/usr/bin/env python3
"""
APEX CAPITAL -- FIRST EAGLE RESEARCH SCHEDULER
===============================================
Daily research scheduler -- runs deep research on one company per day.
Called by cron at 2am UTC.

Crontab entry:
  0 2 * * * cd /home/jackbeard/institutional-tracker && ~/venv/bin/python3 fe_research_scheduler.py >> logs/fe_research_scheduler.log 2>&1
"""

import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("fe_research_scheduler")

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL -- FIRST EAGLE RESEARCH SCHEDULER")
    logger.info("=" * 60)
    try:
        from fe_research_orchestrator import run_scheduler
        run_scheduler()
        logger.info("Research scheduler completed successfully.")
    except Exception as e:
        logger.error(f"Research scheduler failed: {e}", exc_info=True)
        sys.exit(1)
