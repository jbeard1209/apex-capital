#!/usr/bin/env python3
"""
weekly_health_report.py — Weekly Agent Health Report
Runs Sunday 8 AM ET. Posts agent health summary to bulletin board and firm_channel.
"""
import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger("weekly_health_report")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    try:
        from agent_meeting import generate_agent_health_report
        report = generate_agent_health_report("Chief of Staff")
        logger.info(
            f"Health report: {report['agents_ok']} OK, "
            f"{report['agents_degraded']} degraded, "
            f"{report['agents_offline']} offline. "
            f"Escalations: {report['open_escalations']}"
        )
        if report['degraded_list']:
            for d in report['degraded_list']:
                logger.warning(f"  DEGRADED: {d['agent']} — {d.get('note','')}")
        if report['offline_list']:
            logger.warning(f"  OFFLINE: {', '.join(report['offline_list'])}")
    except Exception as e:
        logger.error(f"Health report failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
