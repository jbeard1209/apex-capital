"""
db_dedup_snapshots.py — Deduplicate pipeline_snapshots in historical_13f.db

Dedup key: (ticker, institution, snapshot_date) — keep MAX(rowid).
Also reports rows where ALL feature columns are identical (no auto-delete).
Runs VACUUM, integrity_check, writes dedup_report.json, logs to logs/dedup_YYYYMMDD.log.
"""
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE      = Path('/home/jackbeard/institutional-tracker')
DB_PATH   = BASE / 'data' / 'historical_13f.db'
LOG_DIR   = BASE / 'logs'
DATA_DIR  = BASE / 'data'
TABLE     = 'pipeline_snapshots'
RUN_DATE  = datetime.now().strftime('%Y%m%d')
LOG_FILE  = LOG_DIR / f'dedup_{RUN_DATE}.log'

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('dedup')
log.setLevel(logging.INFO)

fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(fmt)
log.addHandler(fh)

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)
log.addHandler(sh)


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_row_count(cur: sqlite3.Cursor) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    return cur.fetchone()[0]


def get_exact_duplicate_count(cur: sqlite3.Cursor) -> int:
    """Rows that are byte-for-byte identical across every column (including rowid grouping)."""
    cur.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT rowid FROM {TABLE}
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM {TABLE}
                GROUP BY ticker, institution, snapshot_date
            )
        )
    """)
    return cur.fetchone()[0]


def get_key_duplicate_count(cur: sqlite3.Cursor) -> int:
    """Rows that share (ticker, institution, snapshot_date) with another row."""
    cur.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT rowid FROM {TABLE}
            WHERE rowid NOT IN (
                SELECT MAX(rowid) FROM {TABLE}
                GROUP BY ticker, institution, snapshot_date
            )
        )
    """)
    return cur.fetchone()[0]


def get_feature_columns(cur: sqlite3.Cursor) -> list:
    """Return all column names except the three key columns."""
    cur.execute(f"PRAGMA table_info({TABLE})")
    rows = cur.fetchall()
    key_cols = {'ticker', 'institution', 'snapshot_date'}
    return [r[1] for r in rows if r[1] not in key_cols]


def count_feature_identical_rows(cur: sqlite3.Cursor, feature_cols: list) -> int:
    """
    Count rows where ALL feature columns are identical to at least one other row,
    regardless of snapshot_date.  These are informational only — not deleted.
    """
    if not feature_cols:
        return 0
    col_list = ', '.join(feature_cols)
    cur.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT {col_list}, COUNT(*) AS cnt
            FROM {TABLE}
            GROUP BY {col_list}
            HAVING cnt > 1
        ) dup_groups
        JOIN {TABLE} t ON ({col_list.replace(', ', ' = t.') + ' = t.' + feature_cols[-1] if len(feature_cols) == 1 else ''})
    """)
    # Simpler approach — count all rows whose full feature vector appears more than once
    placeholders = ', '.join(feature_cols)
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE ({placeholders}) IN (
            SELECT {placeholders}
            FROM {TABLE}
            GROUP BY {placeholders}
            HAVING COUNT(*) > 1
        )
    """)
    return cur.fetchone()[0]


def verify_no_key_duplicates(cur: sqlite3.Cursor) -> bool:
    cur.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT ticker, institution, snapshot_date, COUNT(*) AS cnt
            FROM {TABLE}
            GROUP BY ticker, institution, snapshot_date
            HAVING cnt > 1
        )
    """)
    return cur.fetchone()[0] == 0


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info('=' * 65)
    log.info(f'DB DEDUP — {TABLE}')
    log.info(f'DB path : {DB_PATH}')
    log.info(f'Started : {datetime.now().isoformat()}')
    log.info('=' * 65)

    if not DB_PATH.exists():
        log.error(f"Database not found: {DB_PATH}")
        sys.exit(1)

    report = {
        'run_date': datetime.now().isoformat(),
        'db_path': str(DB_PATH),
        'table': TABLE,
        'status': 'failed',
    }

    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")    # safer for large ops

    try:
        cur = con.cursor()

        # ── Step 1: counts before ─────────────────────────────────────────
        log.info("Step 1 — counting rows before dedup …")
        rows_before = get_row_count(cur)
        key_dups_before = get_key_duplicate_count(cur)
        log.info(f"  Total rows           : {rows_before:,}")
        log.info(f"  Key-duplicate rows   : {key_dups_before:,}  (share ticker+institution+snapshot_date)")

        report['rows_before'] = rows_before
        report['key_duplicates_before'] = key_dups_before

        # ── Step 2: feature-identical rows (informational) ────────────────
        log.info("Step 2 — checking for feature-identical rows (info only) …")
        feature_cols = get_feature_columns(cur)
        log.info(f"  Feature columns ({len(feature_cols)}): {feature_cols}")

        if feature_cols:
            feat_identical = count_feature_identical_rows(cur, feature_cols)
            log.info(f"  Feature-identical rows (any snapshot_date): {feat_identical:,}")
            log.info("  NOTE: these will NOT be auto-deleted — manual review recommended.")
        else:
            feat_identical = 0
            log.info("  No feature columns found beyond key columns.")

        report['feature_identical_rows'] = feat_identical
        report['feature_columns'] = feature_cols

        if key_dups_before == 0:
            log.info("No key duplicates found — nothing to delete. Running integrity check anyway.")
            rows_deleted = 0
        else:
            # ── Step 3: delete duplicates ─────────────────────────────────
            log.info(f"Step 3 — deleting {key_dups_before:,} duplicate rows (keeping MAX rowid per key) …")
            log.info("  This may take a moment on large tables …")

            con.execute("BEGIN")
            cur.execute(f"""
                DELETE FROM {TABLE}
                WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM {TABLE}
                    GROUP BY ticker, institution, snapshot_date
                )
            """)
            rows_deleted = cur.rowcount
            con.commit()
            log.info(f"  Deleted: {rows_deleted:,} rows")

        report['rows_deleted'] = rows_deleted

        # ── Step 4: integrity check ───────────────────────────────────────
        log.info("Step 4 — PRAGMA integrity_check …")
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        log.info(f"  integrity_check: {integrity}")
        if integrity != 'ok':
            log.error(f"  INTEGRITY FAILURE: {integrity} — aborting before VACUUM")
            report['integrity_check'] = integrity
            report['status'] = 'integrity_failure'
            con.close()
            _write_report(report)
            sys.exit(2)

        report['integrity_check'] = integrity

        # ── Step 5: VACUUM ────────────────────────────────────────────────
        log.info("Step 5 — VACUUM (reclaiming disk space) …")
        # VACUUM cannot run inside a transaction
        con.isolation_level = None
        con.execute("VACUUM")
        con.isolation_level = ''
        log.info("  VACUUM complete.")

        # ── Step 6: row count after ───────────────────────────────────────
        log.info("Step 6 — counting rows after dedup …")
        cur = con.cursor()
        rows_after = get_row_count(cur)
        log.info(f"  Total rows after     : {rows_after:,}")
        log.info(f"  Rows removed         : {rows_before - rows_after:,}")

        report['rows_after'] = rows_after
        report['rows_removed'] = rows_before - rows_after

        # ── Step 7: verify no duplicates remain ───────────────────────────
        log.info("Step 7 — verifying no key-duplicates remain …")
        clean = verify_no_key_duplicates(cur)
        log.info(f"  Duplicate-free on (ticker, institution, snapshot_date): {clean}")

        report['verification_passed'] = clean
        if not clean:
            log.warning("  WARNING: duplicates still present — investigate manually.")

        report['status'] = 'success' if clean else 'partial'

    except Exception as exc:
        log.exception(f"Unexpected error — rolling back: {exc}")
        try:
            con.rollback()
        except Exception:
            pass
        report['status'] = 'error'
        report['error'] = str(exc)
        con.close()
        _write_report(report)
        sys.exit(1)
    finally:
        try:
            con.close()
        except Exception:
            pass

    # ── Step 8: write JSON report ─────────────────────────────────────────
    _write_report(report)

    log.info('=' * 65)
    log.info(f'DEDUP COMPLETE — status={report["status"]}')
    log.info(f'  Before  : {rows_before:,} rows')
    log.info(f'  Deleted : {rows_deleted:,} rows')
    log.info(f'  After   : {rows_after:,} rows')
    log.info(f'  Feature-identical (not deleted): {feat_identical:,}')
    log.info(f'  Report  : {DATA_DIR / "dedup_report.json"}')
    log.info(f'  Log     : {LOG_FILE}')
    log.info('=' * 65)


def _write_report(report: dict):
    report_path = DATA_DIR / 'dedup_report.json'
    try:
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        log.info(f"Step 8 — report written to {report_path}")
    except Exception as e:
        log.error(f"Failed to write report: {e}")


if __name__ == '__main__':
    main()
