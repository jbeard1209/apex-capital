#!/usr/bin/env python3
"""
cit_auto_implementer.py — safe auto-implementation of except:pass → except+log

Scans production files for bare `except: pass` or `except Exception: pass` blocks,
replaces them with proper exception logging, verifies syntax, then marks the
corresponding CIT backlog items as processed.

Usage:
    python3 cit_auto_implementer.py --dry-run     # show what would change (default)
    python3 cit_auto_implementer.py --apply        # actually apply changes
"""

import argparse
import ast
import json
import logging
import os
import py_compile
import re
import shutil
import sys
import tempfile
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cit_auto_implementer")

BASE_DIR    = "/home/jackbeard/institutional-tracker"
BACKLOG_FILE = os.path.join(BASE_DIR, "data", "cit_backlog.json")
REPORT_FILE  = os.path.join(BASE_DIR, "logs", f"cit_auto_impl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

# Files the auto-implementer is allowed to touch
ALLOWED_FILES = {
    "chief_of_staff_agent.py", "cit_director.py", "board_meeting.py", "main.py",
    "canary_agent.py", "manager_agent.py", "rd_team_leader.py", "hr_agent.py",
    "irm_review_agent.py", "earnings_predictor.py", "operations_director.py",
    "alpaca_trader.py", "risk_manager_agent.py", "firm_channel.py",
    "issue_tracker.py", "reconciliation_check.py", "morning_briefing.py",
}

# Regex: matches `except` clause that is just `pass` in the next real line
EXCEPT_RE = re.compile(
    r'^(?P<indent>\s*)except(?P<exc>\s+[^\n:]+)?\s*:\s*$',
)


def _find_except_pass_blocks(lines: list[str]) -> list[dict]:
    """
    Returns list of dicts describing each except:pass block found.
    Each dict: {except_lineno (1-based), pass_lineno (1-based), indent, exc_clause}
    """
    blocks = []
    i = 0
    while i < len(lines):
        m = EXCEPT_RE.match(lines[i])
        if m:
            indent   = m.group("indent")
            exc      = (m.group("exc") or "").strip()
            # find next non-blank, non-comment line
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                next_stripped = lines[j].strip()
                if next_stripped == "pass":
                    blocks.append({
                        "except_lineno": i + 1,     # 1-based
                        "pass_lineno":   j + 1,     # 1-based
                        "indent":        indent,
                        "exc_clause":    exc,
                    })
        i += 1
    return blocks


def _build_replacement(block: dict, body_indent: str) -> tuple[str, str]:
    """
    Returns (new_except_line, new_body_line).
    Ensures the except clause captures `as e` so we can log it.
    """
    exc = block["exc_clause"]
    indent = block["indent"]

    # Normalise the exception clause to always capture as `e`
    if not exc:
        # bare `except:` → `except Exception as e:`
        new_except = f"{indent}except Exception as e:"
    elif re.search(r'\bas\s+\w', exc):
        # already has `as X` — keep as-is
        new_except = f"{indent}except{' ' + exc if exc else ''}:"
    else:
        # has exception type(s) but no `as` → append ` as e`
        new_except = f"{indent}except {exc} as e:"

    new_body = f"{body_indent}logger.debug(f\"non-fatal: {{e}}\")"
    return new_except, new_body


def process_file(filepath: str, dry_run: bool) -> dict:
    """
    Process one file. Returns a result dict.
    """
    fname = os.path.basename(filepath)
    result = {
        "file": fname,
        "blocks_found": 0,
        "blocks_fixed": 0,
        "skipped": [],
        "errors": [],
        "applied": not dry_run,
    }

    with open(filepath, encoding="utf-8") as fh:
        original = fh.read()

    lines = original.splitlines(keepends=True)

    # Detect indentation style used in this file
    body_indent_extra = "    "   # default 4-space body indent inside except

    blocks = _find_except_pass_blocks([l.rstrip("\n") for l in lines])
    result["blocks_found"] = len(blocks)

    if not blocks:
        return result

    # Safety gate: verify logger is in scope for this file
    has_logger = bool(re.search(r'\blogger\s*=\s*logging\.getLogger|^logger\s*=', original, re.MULTILINE))
    if not has_logger:
        result["errors"].append("No logger defined in this file — skipping to avoid broken references.")
        return result

    # Build replacement — iterate in reverse so line indices stay valid
    new_lines = list(lines)
    for block in reversed(blocks):
        eli  = block["except_lineno"] - 1   # 0-based index of except line
        pli  = block["pass_lineno"]   - 1   # 0-based index of pass line

        # Determine body indent from the pass line itself
        pass_line = new_lines[pli]
        body_indent = re.match(r'(\s*)', pass_line).group(1)

        new_except_text, new_body_text = _build_replacement(block, body_indent)

        # Safety gate: replacement must be exactly 1 line and only touch the pass line
        # Reject anything that would add imports, call external functions, or expand scope
        if '\n' in new_body_text or 'import ' in new_body_text or new_body_text.strip().startswith('def '):
            result["skipped"].append(f"line {pli+1}: replacement failed safety gate")
            continue

        # Preserve original line endings
        le = "\n"
        if new_lines[eli].endswith("\r\n"):
            le = "\r\n"

        new_lines[eli] = new_except_text + le
        new_lines[pli] = new_body_text + le

        result["blocks_fixed"] += 1

    new_content = "".join(new_lines)

    # Syntax check before writing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
        tmp.write(new_content)
        tmp_path = tmp.name

    try:
        py_compile.compile(tmp_path, doraise=True)
    except py_compile.PyCompileError as exc:
        result["errors"].append(f"Syntax check FAILED: {exc}")
        result["blocks_fixed"] = 0
        os.unlink(tmp_path)
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    if not dry_run:
        # Backup original
        backup = filepath + ".auto_impl_backup"
        shutil.copy2(filepath, backup)
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        logger.info(f"  ✅ {fname}: {result['blocks_fixed']} fixes applied (backup: {backup})")
    else:
        logger.info(f"  [DRY-RUN] {fname}: would fix {result['blocks_fixed']} of {result['blocks_found']} except:pass blocks")
        # Show first 3 changes as preview
        for b in blocks[:3]:
            body_indent = re.match(r'(\s*)', new_lines[b["pass_lineno"]-1]).group(1) if new_lines else "    "
            ne, nb = _build_replacement(b, body_indent)
            logger.info(f"    line {b['except_lineno']}: except{' '+b['exc_clause'] if b['exc_clause'] else ':'} → {ne.strip()}")

    return result


def mark_backlog_items(results: list[dict], dry_run: bool) -> int:
    """Mark processed backlog items as auto-implemented."""
    if dry_run:
        return 0

    files_fixed = {r["file"] for r in results if r["blocks_fixed"] > 0}
    if not files_fixed:
        return 0

    with open(BACKLOG_FILE, encoding="utf-8") as fh:
        backlog = json.load(fh)

    changed = 0
    for item in backlog:
        title = item.get("title", "")
        # Match items whose title starts with a filename that was fixed
        for fname in files_fixed:
            if title.startswith(fname + ":") and "silent exception swallowed" in title.lower():
                if item.get("status") != "AUTO_IMPLEMENTED":
                    item["status"]        = "AUTO_IMPLEMENTED"
                    item["resolved_at"]   = datetime.utcnow().isoformat()
                    item["resolved_by"]   = "cit_auto_implementer"
                    changed += 1
                break

    with open(BACKLOG_FILE, "w", encoding="utf-8") as fh:
        json.dump(backlog, fh, indent=2)

    return changed


def main():
    # ── Inbox: process pending requests, meetings, broadcasts ──────────────────
    try:
        from agent_inbox import run_inbox as _run_inbox
        _run_inbox("CITAutoImplementer")
    except Exception:
        pass  # inbox is always non-fatal

    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="Show what would change without writing (default)")
    group.add_argument("--apply", action="store_true",
                       help="Actually apply changes to files")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info(f"cit_auto_implementer starting — mode: {mode}")

    results = []
    total_found = 0
    total_fixed = 0

    for fname in sorted(ALLOWED_FILES):
        fpath = os.path.join(BASE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        r = process_file(fpath, dry_run=dry_run)
        results.append(r)
        total_found += r["blocks_found"]
        total_fixed += r["blocks_fixed"]
        if r["errors"]:
            for e in r["errors"]:
                logger.error(f"  ❌ {fname}: {e}")

    logger.info(f"\n{'='*60}")
    logger.info(f"  Total except:pass blocks found:  {total_found}")
    logger.info(f"  Total blocks fixed/would fix:    {total_fixed}")

    if not dry_run:
        marked = mark_backlog_items(results, dry_run=False)
        logger.info(f"  Backlog items marked resolved:   {marked}")

    logger.info(f"  Mode: {mode}")
    logger.info(f"{'='*60}")

    if dry_run:
        logger.info("")
        logger.info("Run with --apply to execute the changes.")


if __name__ == "__main__":
    if "--inbox-only" in sys.argv or "--thread-id" in sys.argv:
        import logging as _log_io
        _log_io.basicConfig(level=_log_io.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S")
        from agent_inbox import main_inbox_only
        main_inbox_only("CITAutoImplementer")
        sys.exit(0)
    main()
