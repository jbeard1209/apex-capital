#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — DAILY DATA BACKUP
# =============================================================================
# Creates a timestamped snapshot of the data/ directory to data/backups/.
# Run automatically at the end of the daily pipeline (main.py Step 12+)
# or manually at any time.
#
# Retention policy: keeps the last 30 daily backups, then prunes oldest.
#
# Usage:
#   python3 backup_data.py              # backup and prune
#   python3 backup_data.py --list       # list existing backups
#   python3 backup_data.py --restore YYYY-MM-DD  # restore from a specific date
# =============================================================================

import argparse
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backup")

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
BACKUP_DIR  = DATA_DIR / "backups"
MAX_BACKUPS = 3    # rolling window. Was 30: on the prod VM's 9.7GB disk, each
                   # snapshot is ~1.3GB, so 30 days would need ~40GB — the backup
                   # job itself filled the disk to 100% twice in July 2026. Three
                   # dailies + GitHub (code) + EDGAR (rebuildable DBs) is plenty.

# Files that are runtime-only, rebuildable, or too big — not backed up
EXCLUDE_PATTERNS = {
    "backups",              # don't backup the backup directory itself
    "canary_alert.json",    # transient alert state — regenerated each run
    "historical_13f.db",    # 144MB+, fully rebuildable from EDGAR scraper
    "matrix_cache",         # regenerable model matrices (200MB+)
    "price_cache.pkl",      # re-downloadable from yfinance (23MB+)
    "edgar_cache",          # re-downloadable filings cache
    "filing_tickers.json",  # 102MB derived file, rebuilt by label builder
    "fe_fundamentals_cache",# re-downloadable EDGAR fundamentals
}


def run_backup(quiet: bool = False) -> dict:
    """
    Create a timestamped snapshot of data/ in data/backups/YYYY-MM-DD_HHMMSS/.
    Prunes backups older than MAX_BACKUPS days.
    Returns a dict with backup metadata.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest      = BACKUP_DIR / timestamp

    if not DATA_DIR.exists():
        logger.warning("  data/ directory not found — nothing to back up")
        return {"status": "skipped", "reason": "data/ not found"}

    # Copy all JSON files in data/ (not subdirectories, not excluded)
    dest.mkdir(exist_ok=True)
    copied = []
    for item in DATA_DIR.iterdir():
        if item.name in EXCLUDE_PATTERNS:
            continue
        if item.is_file() and item.suffix == ".json":
            shutil.copy2(item, dest / item.name)
            copied.append(item.name)
        elif item.is_dir() and item.name not in EXCLUDE_PATTERNS:
            shutil.copytree(item, dest / item.name, ignore=shutil.ignore_patterns("backups"))

    # Write a manifest
    manifest = {
        "backup_timestamp": timestamp,
        "files_copied":     sorted(copied),
        "file_count":       len(copied),
        "source_dir":       str(DATA_DIR),
        "backup_dir":       str(dest),
    }
    with open(dest / "_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    if not quiet:
        logger.info(f"  ✅ Backup created: {dest.name} ({len(copied)} files)")

    # ── Pruning ───────────────────────────────────────────────────────────
    all_backups = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    pruned = []
    while len(all_backups) > MAX_BACKUPS:
        oldest = all_backups.pop(0)
        shutil.rmtree(oldest)
        pruned.append(oldest.name)
        if not quiet:
            logger.info(f"  🗑  Pruned old backup: {oldest.name}")

    manifest["pruned"] = pruned
    return {"status": "ok", **manifest}


def list_backups():
    """Print all existing backups with their file counts."""
    if not BACKUP_DIR.exists():
        print("No backups found — data/backups/ does not exist yet.")
        return

    backups = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    if not backups:
        print("No backups found.")
        return

    print(f"\n  {'BACKUP TIMESTAMP':<25}  {'FILES':<8}  PATH")
    print("  " + "-" * 60)
    for b in backups:
        manifest_path = b / "_manifest.json"
        n_files = "?"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    m = json.load(f)
                n_files = m.get("file_count", "?")
            except Exception:
                pass
        print(f"  {b.name:<25}  {str(n_files):<8}  {b}")
    print(f"\n  {len(backups)} backup(s) found (max {MAX_BACKUPS} retained)\n")


def restore_backup(date_str: str):
    """
    Restore data/ from a backup whose name starts with date_str (YYYY-MM-DD).
    """
    if not BACKUP_DIR.exists():
        print(f"No backups found.")
        return

    matches = [d for d in BACKUP_DIR.iterdir()
               if d.is_dir() and d.name.startswith(date_str)]
    if not matches:
        print(f"No backup found for date: {date_str}")
        return
    if len(matches) > 1:
        print(f"Multiple backups match '{date_str}':")
        for m in matches:
            print(f"  {m.name}")
        print("Please be more specific (include time: YYYY-MM-DD_HHMMSS).")
        return

    src = matches[0]
    print(f"\n  ⚠️  RESTORE: This will overwrite data/ with backup {src.name}")
    confirm = input("  Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("  Restore cancelled.")
        return

    # Copy each file from backup back to data/
    restored = 0
    for item in src.iterdir():
        if item.name == "_manifest.json":
            continue
        target = DATA_DIR / item.name
        if item.is_file():
            shutil.copy2(item, target)
            restored += 1
        elif item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
            restored += 1

    print(f"  ✅ Restored {restored} item(s) from backup {src.name} → data/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apex Capital — Data Backup Utility")
    parser.add_argument("--list",    action="store_true", help="List all existing backups")
    parser.add_argument("--restore", metavar="DATE",      help="Restore from backup (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.list:
        list_backups()
    elif args.restore:
        restore_backup(args.restore)
    else:
        result = run_backup()
        if result["status"] == "ok":
            print(f"  Backup complete: {result.get('backup_timestamp')} ({result.get('file_count')} files)")
        else:
            print(f"  Backup skipped: {result.get('reason')}")
