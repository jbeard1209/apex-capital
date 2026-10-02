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
import logging, os, shutil, subprocess, sys, glob, time
from pathlib import Path

BASE = Path(__file__).parent
logger = logging.getLogger("disk_janitor")

def bytecode_caches():
    """Where THIS interpreter actually writes .pyc files. Two hosts, two answers.

    The VM's `~/venv/bin/python3` leaves `__pycache__` beside the source, which
    is what this script always assumed. The Mac's Apple python sets
    `sys.pycache_prefix` to ~/Library/Caches/com.apple.python, so not one
    bytecode file is ever written under the repo and every repo-relative sweep
    for `__pycache__` here has been a no-op against 345MB (measured 2026-08-28).

    That is not only a disk question. A stale .pyc is served whenever the source
    mtime SECOND and byte size both match, which is exactly what a same-length
    edit inside one second produces — so a mutation test that restores a file,
    verifies the md5, and clears `./**/__pycache__` still runs the mutant on
    this Mac, because the cache it cleared was never the cache in use. That cost
    three full suite runs and a blocked push today (memory
    `harness-ran-stale-bytecode`).

    Only this project's subtree of the prefix is returned. The prefix mirrors
    the absolute source path underneath it, so the mapping is exact — and it is
    shared with every other Python project on the machine, which has no business
    being deleted by the trading firm's janitor.
    """
    globs = [BASE / "**" / "__pycache__"]
    prefix = getattr(sys, "pycache_prefix", None)
    if prefix:
        base = BASE.resolve()
        globs.append(Path(prefix) / base.relative_to(base.anchor))
    return globs

def pct_used():
    st = os.statvfs("/")
    return round(100 * (1 - st.f_bavail / st.f_blocks))

def prune(paths_globs, older_days=None):
    n = 0
    for g in paths_globs:
        # `recursive=True` or `**` silently degrades to a single `*`, matching
        # exactly one directory level. That is why the biggest cache on the VM —
        # institutional-tracker/__pycache__ itself, 8.2MB of the 8.7MB total —
        # was never once pruned on a 9.7GB disk that has twice filled to 100%
        # and truncated 35 JSON files mid-write. The pattern looked recursive
        # and was not.
        for p in glob.glob(str(g), recursive=True):
            try:
                if older_days and (time.time() - os.path.getmtime(p)) < older_days * 86400:
                    continue
                if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                else: os.remove(p)
                n += 1
            except Exception as e:
                # per-path failure must not abort the janitor, but it should log
                logger.debug(f"disk_janitor skip {p}: {e}")
    return n

if __name__ == "__main__":
    before = pct_used()
    n  = prune([BASE/"logs/*.log"], older_days=5)
    n += prune([BASE/"data/matrix_cache/*"] + bytecode_caches(), older_days=None)
    n += prune(["/tmp/yf*", "/tmp/tmp*"], older_days=2)
    if pct_used() > 80:
        # keep newest 2 backups only
        b = sorted(glob.glob(str(BASE/"data/backups/*")), key=os.path.getmtime)
        for p in b[:-2]: shutil.rmtree(p, ignore_errors=True); n += 1
        n += prune([BASE/"data/fe_fundamentals_cache/*"], older_days=14)
    after = pct_used()
    print(f"janitor: {before}% -> {after}% ({n} items removed)")
    sys.exit(1 if after > 88 else 0)
