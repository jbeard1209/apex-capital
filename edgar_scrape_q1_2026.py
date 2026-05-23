"""
APEX CAPITAL -- EDGAR Q1 2026 INCREMENTAL SCRAPER
==================================================
Scrapes 13F-HR filings covering the period Jan-Mar 2026 (filed by May 15 2026)
for all tracked institutions, then inserts any new rows into historical_13f.db.

This script wraps the existing edgar_historical_scraper.run_scraper() for
incremental mode, then cross-checks that Q1 2026 filings (period_of_report
2026-01-01 to 2026-03-31) were actually captured. Any institution still missing
Q1 2026 data is scraped individually.

Outputs
-------
  logs/edgar_q1_2026.log           -- full run log
  data/edgar_q1_2026_report.json  -- summary JSON

Usage
-----
  python3 edgar_scrape_q1_2026.py [--full]

  --full    force re-scrape of all filings (not just new ones)
            default is incremental (only new accessions since last run)
"""

import sys
import json
import logging
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path

# --- Paths -------------------------------------------------------------------
BASE     = Path('/home/jackbeard/institutional-tracker')
LOG_DIR  = BASE / 'logs'
DATA_DIR = BASE / 'data'
DB_PATH  = DATA_DIR / 'historical_13f.db'
LOG_FILE = LOG_DIR / 'edgar_q1_2026.log'
REPORT   = DATA_DIR / 'edgar_q1_2026_report.json'

LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# --- Logging -----------------------------------------------------------------
handlers = [
    logging.FileHandler(str(LOG_FILE)),
    logging.StreamHandler(sys.stdout),
]
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=handlers,
)
log = logging.getLogger('edgar_q1_2026')

# --- Import existing scraper -------------------------------------------------
sys.path.insert(0, str(BASE))
from predictive_model.edgar_historical_scraper import (
    run_scraper,
    get_db,
    get_db_stats,
    scrape_institution,
    fetch_all_13f_accessions,
    parse_holdings_from_accession,
    resolve_ticker,
    _SEC_DELAY,
    SEC_HEADERS,
)
from config import TRACKED_INSTITUTIONS

# Q1 2026 filing window
Q1_START  = date(2026, 1, 1)
Q1_END    = date(2026, 3, 31)
FILED_BY  = date(2026, 5, 15)


# =============================================================================
# Helpers
# =============================================================================

def _q1_2026_filings(conn: sqlite3.Connection) -> dict:
    """
    Return {institution: [list of accessions]} for filings whose period_of_report
    falls within Q1 2026 (2026-01-01 to 2026-03-31).
    """
    rows = conn.execute(
        """SELECT institution, accession, filing_date, period_of_report
           FROM filings
           WHERE period_of_report >= '2026-01-01'
             AND period_of_report <= '2026-03-31'
           ORDER BY institution, filing_date"""
    ).fetchall()

    result: dict = {}
    for row in rows:
        inst = row[0]
        result.setdefault(inst, []).append({
            'accession':        row[1],
            'filing_date':      row[2],
            'period_of_report': row[3],
        })
    return result


def _holdings_for_accession(conn: sqlite3.Connection, accession: str) -> int:
    """Return holding count for an accession (0 if not yet in DB)."""
    row = conn.execute(
        'SELECT n_holdings FROM filings WHERE accession = ?', (accession,)
    ).fetchone()
    return row[0] if row else 0


def _targeted_scrape(institution: str, cik: str, conn: sqlite3.Connection,
                     full: bool = False) -> int:
    """
    Scrape one institution for Q1 2026 specifically, using the shared
    scrape_institution() function from edgar_historical_scraper.
    Returns number of new filings inserted.
    """
    log.info(f'  Targeted scrape: {institution} (CIK {cik})')
    return scrape_institution(institution, cik, conn, full=full)


# =============================================================================
# Main
# =============================================================================

def main(full: bool = False) -> dict:
    started_at = datetime.now()
    log.info('=' * 65)
    log.info('EDGAR Q1 2026 INCREMENTAL SCRAPER')
    log.info(f'Started: {started_at}')
    log.info(f'Mode: {"FULL re-scrape" if full else "INCREMENTAL (new filings only)"}')
    log.info(f'Target period: {Q1_START} to {Q1_END}  (filed by {FILED_BY})')
    log.info('=' * 65)

    # --- Step 1: run incremental (or full) scrape for all institutions ------
    log.info('')
    log.info('Step 1: Running edgar_historical_scraper.run_scraper() ...')
    summary = run_scraper(full=full)
    log.info(f'  run_scraper complete: {summary.get("total_inserted", 0)} new filings inserted')

    # --- Step 2: verify Q1 2026 coverage ------------------------------------
    log.info('')
    log.info('Step 2: Checking Q1 2026 filing coverage ...')
    conn = get_db()
    q1_filings = _q1_2026_filings(conn)

    covered     = set(q1_filings.keys())
    all_tracked = set(TRACKED_INSTITUTIONS.keys())
    missing     = all_tracked - covered

    log.info(f'  Institutions with Q1 2026 filings: {len(covered)} / {len(all_tracked)}')
    if covered:
        log.info(f'    Have data: {", ".join(sorted(covered))}')
    if missing:
        log.info(f'    Missing  : {", ".join(sorted(missing))}')

    # --- Step 3: targeted scrape for any institution still missing ----------
    targeted_results: dict = {}
    if missing:
        log.info('')
        log.info(f'Step 3: Targeted scrape for {len(missing)} institution(s) without Q1 2026 data ...')
        for inst in sorted(missing):
            cik = TRACKED_INSTITUTIONS.get(inst)
            if not cik:
                log.warning(f'  {inst}: no CIK configured -- skipping')
                continue
            try:
                n = _targeted_scrape(inst, cik, conn, full=full)
                targeted_results[inst] = {'inserted': n, 'status': 'OK'}
                log.info(f'  {inst}: {n} new filing(s) inserted')
            except Exception as e:
                log.error(f'  {inst}: scrape failed: {e}')
                targeted_results[inst] = {'inserted': 0, 'status': f'ERROR: {e}'}
    else:
        log.info('Step 3: Skipped -- all institutions have Q1 2026 data.')

    # --- Step 4: re-check coverage after targeted scrape -------------------
    q1_filings_final = _q1_2026_filings(conn)
    covered_final    = set(q1_filings_final.keys())
    still_missing    = all_tracked - covered_final

    # --- Step 5: build holdings summary for Q1 2026 filings ----------------
    log.info('')
    log.info('Step 5: Holdings summary for Q1 2026 filings ...')
    holdings_summary: dict = {}
    for inst, filings_list in q1_filings_final.items():
        total_holdings = 0
        for f in filings_list:
            n = _holdings_for_accession(conn, f['accession'])
            total_holdings += n
        holdings_summary[inst] = {
            'n_filings':  len(filings_list),
            'n_holdings': total_holdings,
            'filings':    filings_list,
        }
        log.info(
            f'  {inst}: {len(filings_list)} filing(s)  '
            f'{total_holdings} holdings'
        )

    # --- Step 6: DB stats after run -----------------------------------------
    db_stats = get_db_stats()
    conn.close()

    # --- Build report -------------------------------------------------------
    finished_at = datetime.now()
    duration_s  = (finished_at - started_at).total_seconds()

    report = {
        'run_at':           started_at.isoformat(),
        'finished_at':      finished_at.isoformat(),
        'duration_seconds': round(duration_s, 1),
        'mode':             'FULL' if full else 'INCREMENTAL',
        'q1_2026_period':   {'start': str(Q1_START), 'end': str(Q1_END)},
        'initial_scrape': {
            'total_inserted': summary.get('total_inserted', 0),
            'institutions':   summary.get('institutions', {}),
        },
        'coverage': {
            'total_tracked':      len(all_tracked),
            'with_q1_2026_data':  len(covered_final),
            'missing_q1_2026':    sorted(still_missing),
        },
        'targeted_scrape':    targeted_results,
        'q1_2026_filings':    holdings_summary,
        'db_stats_after':     db_stats,
    }

    REPORT.write_text(json.dumps(report, indent=2, default=str))
    log.info('')
    log.info(f'Report written to: {REPORT}')
    log.info(f'Log written to   : {LOG_FILE}')

    log.info('')
    log.info('=' * 65)
    log.info('EDGAR Q1 2026 SCRAPE COMPLETE')
    log.info(f'  Duration          : {duration_s:.0f}s')
    log.info(f'  Initial inserts   : {summary.get("total_inserted", 0)}')
    log.info(f'  Q1 2026 coverage  : {len(covered_final)}/{len(all_tracked)} institutions')
    if still_missing:
        log.warning(f'  Still missing     : {", ".join(sorted(still_missing))}')
    log.info(f'  DB total filings  : {db_stats.get("filings", "N/A")}')
    log.info(f'  DB total holdings : {db_stats.get("holdings", "N/A")}')
    log.info('=' * 65)

    return report


if __name__ == '__main__':
    full_mode = '--full' in sys.argv
    report    = main(full=full_mode)
    print(json.dumps(report, indent=2, default=str))
