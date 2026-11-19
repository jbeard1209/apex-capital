"""One-off data migration: rows filed under "Two Sigma" that are really Scion's.

config.TRACKED_INSTITUTIONS mapped "Two Sigma" to CIK 0001649339 from the start.
EDGAR says that CIK is Scion Asset Management, LLC (checked 2026-11-19 against
data.sec.gov/submissions). Every "Two Sigma" filing in historical_13f.db
(33 filings 2016-2025, their holdings, and the backfilled pipeline_snapshots)
is Scion's portfolio under the wrong name. The real Two Sigma Investments, LP
is 0001179392.

This relabels those rows to SCION_LABEL. It deletes nothing, backs the db up
first, and is idempotent. There is no state sync between hosts, so run it on
each host:  python3 migrate_two_sigma_cik.py [--apply]
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

WRONG_CIK = "0001649339"
OLD_LABEL = "Two Sigma"
SCION_LABEL = "Scion Asset Management"
DB = Path(__file__).parent / "data" / "historical_13f.db"


def migrate(conn: sqlite3.Connection, apply: bool) -> dict:
    cik_like = f"%{WRONG_CIK.lstrip('0')}"
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM filings WHERE institution=? AND cik LIKE ?", (OLD_LABEL, cik_like))]
    # Every snapshot row predates the CIK fix, so every "Two Sigma" snapshot was
    # built from these filings: there were never any real Two Sigma filings.
    real_ts = conn.execute("SELECT count(*) FROM filings WHERE institution=? AND cik NOT LIKE ?",
                           (OLD_LABEL, cik_like)).fetchone()[0]
    out = {"filings": len(ids), "real_two_sigma_filings": real_ts}
    q = ",".join("?" * len(ids)) or "NULL"
    out["holdings"] = conn.execute(
        f"SELECT count(*) FROM holdings WHERE institution=? AND filing_id IN ({q})",
        [OLD_LABEL, *ids]).fetchone()[0]
    out["pipeline_snapshots"] = (conn.execute(
        "SELECT count(*) FROM pipeline_snapshots WHERE institution=?", (OLD_LABEL,)).fetchone()[0]
        if real_ts == 0 else 0)
    if apply:
        conn.execute(f"UPDATE filings SET institution=? WHERE id IN ({q})", [SCION_LABEL, *ids])
        conn.execute(f"UPDATE holdings SET institution=? WHERE institution=? AND filing_id IN ({q})",
                     [SCION_LABEL, OLD_LABEL, *ids])
        if real_ts == 0:
            conn.execute("UPDATE pipeline_snapshots SET institution=? WHERE institution=?",
                         (SCION_LABEL, OLD_LABEL))
        conn.commit()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not DB.exists():
        print(f"no {DB}; nothing to migrate"); return 0
    if a.apply:
        bak = DB.with_name(f"historical_13f.before-two-sigma-cik.{datetime.now():%Y%m%d%H%M%S}.db")
        shutil.copy2(DB, bak)
        print(f"backup: {bak}")
    conn = sqlite3.connect(DB)
    print(("APPLIED " if a.apply else "DRY RUN ") + str(migrate(conn, a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
