#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — DISK JANITOR (the free disk solution)
# =============================================================================
# The prod VM has 9.7GB total; data grows daily and twice filled the disk to
# 100% (truncating 35 JSON files mid-write). Chairman directive: solve free —
# so instead of a bigger disk, the firm cleans up after itself:
#   - always: logs >5 days, __pycache__, matrix_cache, /tmp yfinance droppings
#   - at >80%: also prune backups beyond the newest 2 and edgar_cache
#   - alerts only if STILL >88% after pruning (self-heal first, email second)
# Cron: twice daily (before backup + mid-afternoon). Exits 1 only if still full.
# =============================================================================
import os, shutil, subprocess, sys, glob, time
from pathlib import Path

BASE = Path(__file__).parent
def pct_used():
    st = os.statvfs("/")
    return round(100 * (1 - st.f_bavail / st.f_blocks))

def prune(paths_globs, older_days=None):
    n = 0
    for g in paths_globs:
        for p in glob.glob(str(g)):
            try:
                if older_days and (time.time() - os.path.getmtime(p)) < older_days * 86400:
                    continue
                if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                else: os.remove(p)
                n += 1
            except Exception: pass
    return n

if __name__ == "__main__":
    before = pct_used()
    n  = prune([BASE/"logs/*.log"], older_days=5)
    n += prune([BASE/"data/matrix_cache/*", BASE/"**/__pycache__"], older_days=None)
    n += prune(["/tmp/yf*", "/tmp/tmp*"], older_days=2)
    if pct_used() > 80:
        # keep newest 2 backups only
        b = sorted(glob.glob(str(BASE/"data/backups/*")), key=os.path.getmtime)
        for p in b[:-2]: shutil.rmtree(p, ignore_errors=True); n += 1
        n += prune([BASE/"data/edgar_cache/*"], older_days=14)
    after = pct_used()
    print(f"janitor: {before}% -> {after}% ({n} items removed)")
    sys.exit(1 if after > 88 else 0)
