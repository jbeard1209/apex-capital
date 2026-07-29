# =============================================================================
# APEX CAPITAL — CIT IMPLEMENTATION AGENT
# =============================================================================
#
# Picks up approved proposals from `data/cit_implementation_queue.json` and
# attempts to implement the proposed fix. Focuses exclusively on:
#   • Tier 0 (auto-approved by deputy_board): trivial LOW-severity code fixes
#   • Deputy-board-approved proposals routed here after their vote
#
# The agent reads each proposal's `proposed_fix` string and applies the most
# common patterns:
#   1. makedirs guard      — wraps a file write with os.makedirs(..., exist_ok=True)
#   2. try/except guard    — wraps an unguarded call in a try/except with logging
#   3. None-check guard    — adds `if x is not None:` before attribute access
#   4. TypeError fix       — corrects obvious type mismatches (int vs str, etc.)
#   5. Import fix          — adds missing imports
#
# For anything it cannot auto-apply, it writes a human-readable note to
# `data/cit_implementation_report.json` explaining what it tried and why it
# could not complete the fix, so a developer (or future Claude session) can
# review and apply manually.
#
# Safety guardrails:
#   - Never touches live trading code (alpaca_trader.py, main.py risk sections)
#     without a dry_run=True first.
#   - Never modifies files unless confidence >= MIN_CONFIDENCE.
#   - Creates a backup copy before any write (data/backups/).
#   - Max 10 fixes per run to prevent runaway changes.
#   - Logs every action to firm_channel.json.
#
# Usage:
#   python3 cit_implementation_agent.py              # apply up to 10 pending fixes
#   python3 cit_implementation_agent.py --dry-run    # plan but don't write
#   python3 cit_implementation_agent.py --report     # print status report
# =============================================================================

import argparse
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cit_implementation_agent")

BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
QUEUE_PATH   = DATA_DIR / "cit_implementation_queue.json"
REPORT_PATH  = DATA_DIR / "cit_implementation_report.json"
BACKUP_DIR   = DATA_DIR / "backups" / "cit_impl"
FIRM_CHANNEL = DATA_DIR / "firm_channel.json"

# Never auto-modify these — too risky without human review
# Protected-file policy is NO LONGER defined here. It used to be a private set
# that could silently drift from the real guardrail. As of 2026-07-29 the single
# source of truth is signoff_ledger.HARD_LINE_SUBSTRINGS via
# signoff_ledger.touches_hard_line() — one policy, one place, red-teamed. The old
# set is kept only as a documented fallback if the ledger import ever fails.
try:
    import signoff_ledger as _sl
except Exception:      # pragma: no cover - defensive
    _sl = None

try:
    import irm_code_envelope as _irm_env    # the IRM lens (distinct from CIT)
except Exception:      # pragma: no cover - defensive
    _irm_env = None

_LEGACY_PROTECTED_FALLBACK = {
    "alpaca_trader.py", "main.py", "risk_manager_agent.py",
    "irm_review_agent.py", "board_meeting.py", "manager_agent.py",
}


def _is_protected(target_file_name: str) -> bool:
    """Is this file off-limits to autonomous edits? Delegates to the ledger's
    hard-line rules so there is exactly one protected-file policy in the firm.
    Falls back to the legacy set only if the ledger can't be imported."""
    if _sl is not None:
        try:
            return bool(_sl.touches_hard_line([target_file_name]))
        except Exception:
            pass
    return Path(target_file_name).name in _LEGACY_PROTECTED_FALLBACK


MIN_CONFIDENCE  = 65    # minimum confidence to attempt auto-fix
MAX_FIXES_PER_RUN = 10  # hard cap per invocation


def _load_queue() -> list:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text())
        except Exception:
            pass
    return []


def _save_queue(queue: list):
    QUEUE_PATH.write_text(json.dumps(queue, indent=2, default=str))


def _backup_file(path: Path) -> Optional[Path]:
    """Create a timestamped backup before modifying a file."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"{path.name}.{ts}.bak"
        shutil.copy2(path, dst)
        return dst
    except Exception as e:
        logger.warning(f"  Backup failed for {path.name}: {e}")
        return None


def _post_to_firm_channel(agent: str, message: str, category: str = "CIT_IMPL"):
    try:
        fc = []
        if FIRM_CHANNEL.exists():
            fc = json.loads(FIRM_CHANNEL.read_text())
        if not isinstance(fc, list):
            fc = fc.get("messages", []) if isinstance(fc, dict) else []
        fc.append({
            "ts":       datetime.now().isoformat(),
            "date":     datetime.now().strftime("%Y-%m-%d"),
            "agent":    agent,
            "category": category,
            "content":  message,
            "priority": "NORMAL",
        })
        if len(fc) > 500:
            fc = fc[-500:]
        FIRM_CHANNEL.write_text(json.dumps(fc, indent=2, default=str))
    except Exception:
        pass


# =============================================================================
# FIX PATTERNS
# =============================================================================

def _try_makedirs_fix(file_path: Path, fix_desc: str) -> dict:
    """
    Pattern: 'missing makedirs before file write at line N'
    Add `os.makedirs(os.path.dirname(path), exist_ok=True)` before write calls.
    """
    try:
        # Extract line number from fix description
        m = re.search(r':(\d+)', fix_desc)
        if not m:
            return {"applied": False, "reason": "Could not extract line number"}

        target_line = int(m.group(1))
        lines = file_path.read_text().splitlines(keepends=True)

        if target_line < 1 or target_line > len(lines):
            return {"applied": False, "reason": f"Line {target_line} out of range"}

        # Look for an open() or write call near that line
        context = "".join(lines[max(0, target_line-3): target_line+2])
        if "open(" not in context and "write" not in context:
            return {"applied": False, "reason": "No open/write call near specified line"}

        # Insert makedirs guard before the target line
        insert_idx  = target_line - 1
        target      = lines[insert_idx]
        indent      = len(target) - len(target.lstrip())
        indent_str  = " " * indent

        # Extract path variable from the open() call if possible
        open_match = re.search(r'open\((\w+)', target)
        path_var   = open_match.group(1) if open_match else "path"

        guard_line = f"{indent_str}os.makedirs(os.path.dirname({path_var}), exist_ok=True)\n"

        # Check if guard already exists nearby
        nearby = "".join(lines[max(0, insert_idx-2): insert_idx+1])
        if "makedirs" in nearby:
            return {"applied": False, "reason": "makedirs guard already present nearby"}

        lines.insert(insert_idx, guard_line)
        # Ensure os is imported
        content = "".join(lines)
        if "import os" not in content:
            content = "import os\n" + content

        _backup_file(file_path)
        file_path.write_text(content)
        return {"applied": True, "change": f"Added makedirs guard before line {target_line}"}

    except Exception as e:
        return {"applied": False, "reason": str(e)}


def _try_try_except_wrap(file_path: Path, fix_desc: str, evidence: str) -> dict:
    """
    Pattern: wrap a specific call in try/except to prevent crashes.
    Only applies when the evidence clearly identifies a single line.
    """
    try:
        m = re.search(r'line[s]?\s+(\d+)', fix_desc + " " + evidence, re.IGNORECASE)
        if not m:
            return {"applied": False, "reason": "Cannot identify specific line to wrap"}

        target_line = int(m.group(1))
        lines = file_path.read_text().splitlines(keepends=True)

        if target_line < 1 or target_line > len(lines):
            return {"applied": False, "reason": f"Line {target_line} out of range"}

        target      = lines[target_line - 1]
        indent      = len(target) - len(target.lstrip())
        indent_str  = " " * indent
        inner_str   = " " * (indent + 4)

        # Don't double-wrap
        prev = "".join(lines[max(0, target_line-3): target_line-1])
        if "try:" in prev:
            return {"applied": False, "reason": "Already inside a try block"}

        new_lines = [
            f"{indent_str}try:\n",
            f"{inner_str}{target.lstrip()}",
            f"{indent_str}except Exception as _cit_e:\n",
            f"{inner_str}logger.warning(f'CIT auto-guard: {{_cit_e}}')\n",
        ]
        lines[target_line - 1: target_line] = new_lines

        _backup_file(file_path)
        file_path.write_text("".join(lines))
        return {"applied": True, "change": f"Wrapped line {target_line} in try/except"}

    except Exception as e:
        return {"applied": False, "reason": str(e)}


# =============================================================================
# MAIN IMPLEMENTATION LOOP
# =============================================================================

def _line_delta(before: str, after: str) -> tuple:
    """Count added/removed lines between two file contents (for blast radius)."""
    import difflib
    added = removed = 0
    for ln in difflib.ndiff(before.splitlines(), after.splitlines()):
        if ln.startswith("+ "):
            added += 1
        elif ln.startswith("- "):
            removed += 1
    return added, removed


def _run_green_tests(timeout: int = 600) -> bool:
    """The 'green tests' the ledger requires: smoke suite + wiring audit must BOTH
    pass against the (edited) working tree. Fail-closed — if we can't prove green,
    the fix does not earn its test result."""
    import sys, subprocess
    for script in ("test_pipeline_smoke.py", "wiring_audit.py"):
        try:
            p = subprocess.run([sys.executable, script], cwd=str(BASE_DIR),
                               capture_output=True, text=True, timeout=timeout)
            if p.returncode != 0:
                return False
        except Exception:
            return False
    return True


def _account_and_gate(pid, title, target_path, category, severity,
                      confidence, pre_content, applied_change) -> dict:
    """Produce the CIT + IRM sign-offs for a freshly-applied fix and run the gate.

      • Both modes: register the change, run green tests, record the two lenses
        (CIT = this agent; IRM = the risk-envelope reviewer), and call preflight
        so the decision is logged for the promotion gate.
      • SHADOW: keep the fix regardless (observation only — no behaviour change).
      • ENFORCE: an uncleared / over-budget fix is REVERTED from pre_content and
        routed to a human; a cleared one consumes the daily deploy budget.
    """
    out = {"cleared": False, "reverted": False, "reason": ""}
    if _sl is None:
        return out
    name = target_path.name
    try:
        post = target_path.read_text()
    except Exception:
        post = pre_content or ""
    added, removed = _line_delta(pre_content or "", post)
    tr = "PASS" if _run_green_tests() else "FAIL"

    cid = f"citimpl-{pid}"
    _sl.propose_change(cid, title[:120], [name], added, removed,
                       str(category), test_result=tr)
    _sl.attach_test_result(cid, tr)   # tie the green result to HEAD (staleness guard)

    # CIT lens — the implementer signs what it actually verified.
    _sl.sign(cid, "CIT-Implementation-Agent", "CIT",
             f"auto-fix ({confidence}% conf): {applied_change[:100]}; "
             f"green tests {tr}; blast {added + removed} lines/1 file")

    # IRM lens — bounded, independent risk-envelope review (distinct agent).
    verdict = {"ok": False, "reason": "IRM envelope reviewer unavailable"}
    if _irm_env is not None:
        try:
            verdict = _irm_env.review([name], added, removed, severity, category)
        except Exception as e:
            verdict = {"ok": False, "reason": f"envelope review error: {e}"}
    if verdict.get("ok"):
        _sl.sign(cid, "IRM-Risk-Envelope", "IRM", verdict.get("rationale", "within envelope"))
    else:
        _sl.veto(cid, "IRM-Risk-Envelope", verdict.get("reason", "outside envelope"), role="IRM")

    pf = _sl.preflight(cid, actuator="cit_implementation_agent")
    out["cleared"] = not pf.get("would_block", True)

    if pf.get("mode") == "ENFORCE":
        deployed = False
        if pf.get("allow"):
            deployed, _r = _sl.reserve_and_deploy(cid)
        if not deployed:
            if pre_content is not None:
                try:
                    target_path.write_text(pre_content)   # undo — must not persist
                except Exception:
                    pass
            out["reverted"] = True
            out["reason"] = "gate blocked (ENFORCE): " + "; ".join(pf.get("reasons", [])[:2])
    return out


def run_implementation(dry_run: bool = False, max_fixes: int = MAX_FIXES_PER_RUN) -> dict:
    queue   = _load_queue()
    pending = [item for item in queue if item.get("status") == "queued"]
    logger.info(f"CIT Implementation Agent: {len(pending)} pending fix(es) in queue")

    if not pending:
        return {"status": "NO_ACTION", "implemented": 0, "skipped": 0}

    implemented  = []
    skipped      = []
    report_items = []
    fixes_applied = 0

    for item in pending[:max_fixes]:
        pid        = item.get("proposal_id", "?")
        title      = item.get("title", "?")
        fix_desc   = item.get("proposed_fix", "")
        evidence   = item.get("evidence", "")
        confidence = item.get("confidence", 0)
        category   = item.get("category", "?")
        severity   = item.get("severity", "LOW")

        logger.info(f"  Processing [{pid}]: {title[:60]}")

        # Confidence gate
        if confidence < MIN_CONFIDENCE:
            reason = f"Confidence {confidence}% below minimum {MIN_CONFIDENCE}%"
            logger.info(f"    SKIP: {reason}")
            item["status"]    = "skipped"
            item["skip_reason"] = reason
            skipped.append(pid)
            report_items.append({"pid": pid, "title": title, "result": "SKIPPED", "reason": reason})
            continue

        # Identify target file from evidence, title, or fix description
        _search_text = (evidence or "") + " " + (fix_desc or "") + " " + title
        file_match = re.search(r'([\w/\-]+\.py)(?::[\d]+)?', _search_text)
        target_file_name = file_match.group(1) if file_match else None

        # If still no file, try known-pattern titles
        if not target_file_name:
            # "makedirs" findings are almost always about data writes
            if "makedirs" in (fix_desc + title).lower():
                # Extract from evidence: "file.py:line"
                ev_match = re.search(r'([\w_]+\.py):(\d+)', evidence)
                if ev_match:
                    target_file_name = ev_match.group(1)

        if not target_file_name:
            # Some findings are systemic (log noise, zero tests) — not file-specific
            # Mark as systemic rather than skipping silently
            item["status"]    = "systemic"
            item["skip_reason"] = "No specific file target — systemic finding"
            skipped.append(pid)
            report_items.append({"pid": pid, "title": title, "result": "SYSTEMIC",
                                  "reason": "Requires architectural change, not a single-file fix"})
            continue

        # Protected file check — delegated to the sign-off ledger's hard-line
        # rules (single source of truth), not a private list that can drift.
        base_name = Path(target_file_name).name
        if _is_protected(target_file_name):
            reason = f"{base_name} is a protected file — human review required"
            logger.info(f"    SKIP: {reason}")
            item["status"]    = "protected"
            item["skip_reason"] = reason
            skipped.append(pid)
            report_items.append({"pid": pid, "title": title, "result": "PROTECTED", "reason": reason, "file": base_name})
            continue

        target_path = BASE_DIR / target_file_name
        if not target_path.exists():
            # Try common subdirs
            for candidate in [BASE_DIR / base_name, BASE_DIR / "agents" / base_name]:
                if candidate.exists():
                    target_path = candidate
                    break

        if not target_path.exists():
            reason = f"File not found: {target_file_name}"
            logger.info(f"    SKIP: {reason}")
            item["status"]    = "skipped"
            item["skip_reason"] = reason
            skipped.append(pid)
            report_items.append({"pid": pid, "title": title, "result": "FILE_NOT_FOUND", "reason": reason})
            continue

        # Capture the file BEFORE we touch it. The sign-off gate runs AFTER the
        # fix is applied (so it can measure the real blast radius and run green
        # tests against the edited code); in ENFORCE a blocked fix is reverted
        # from this snapshot. See _account_and_gate().
        try:
            _pre_content = target_path.read_text()
        except Exception:
            _pre_content = None

        # Determine fix pattern
        fix_lower = (fix_desc + " " + title).lower()
        result = {"applied": False, "reason": "No matching fix pattern"}

        if "makedirs" in fix_lower or "make_dir" in fix_lower:
            if not dry_run:
                result = _try_makedirs_fix(target_path, fix_desc + " " + evidence)
            else:
                result = {"applied": False, "reason": "DRY RUN — no changes made"}

        elif "try" in fix_lower and "except" in fix_lower:
            if not dry_run:
                result = _try_try_except_wrap(target_path, fix_desc, evidence)
            else:
                result = {"applied": False, "reason": "DRY RUN — no changes made"}

        else:
            reason = f"No auto-applicable pattern matched for: {title[:50]}"
            result = {"applied": False, "reason": reason}

        if result.get("applied"):
            # Produce the CIT + IRM sign-offs and run the gate. In ENFORCE a
            # blocked fix is reverted here and routed to a human; in SHADOW the
            # fix is kept and the gate's verdict is only observed.
            acct = {"cleared": None, "reverted": False, "reason": ""}
            if not dry_run:
                try:
                    acct = _account_and_gate(pid, title, target_path, category,
                                             severity, confidence, _pre_content,
                                             result.get("change", ""))
                except Exception as _e:   # never break the pipeline on accounting
                    logger.debug(f"    sign-off accounting skipped ({_e})")

            if acct.get("reverted"):
                logger.info(f"    ROUTE_TO_HUMAN (reverted): {acct.get('reason','')}")
                item["status"]     = "routed_to_human"
                item["skip_reason"] = acct.get("reason", "gate blocked (ENFORCE)")
                skipped.append(pid)
                report_items.append({"pid": pid, "title": title,
                                     "result": "ROUTED_TO_HUMAN", "reason": acct.get("reason", "")})
                continue

            fixes_applied += 1
            item["status"]      = "implemented"
            item["implemented_at"] = datetime.now().isoformat()
            item["change"]      = result.get("change", "")
            item["gate_cleared"] = acct.get("cleared")
            implemented.append(pid)
            report_items.append({
                "pid": pid, "title": title, "result": "IMPLEMENTED",
                "change": result.get("change", ""), "file": str(target_path.name),
                "gate_cleared": acct.get("cleared"),
            })
            logger.info(f"    ✓ IMPLEMENTED: {result.get('change','')} "
                        f"(gate_cleared={acct.get('cleared')})")
        else:
            item["status"]       = "needs_human"
            item["skip_reason"]  = result.get("reason", "Pattern not matched")
            skipped.append(pid)
            report_items.append({
                "pid": pid, "title": title, "result": "NEEDS_HUMAN",
                "reason": result.get("reason", ""), "fix_hint": fix_desc[:200],
            })
            logger.info(f"    → NEEDS_HUMAN: {result.get('reason','')}")

    # Save updated queue
    _save_queue(queue)

    # Write report
    report = {
        "generated_at":   datetime.now().isoformat(),
        "implemented":    len(implemented),
        "skipped":        len(skipped),
        "total_in_queue": len(queue),
        "dry_run":        dry_run,
        "items":          report_items,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str))

    # Post to firm channel
    if implemented:
        _post_to_firm_channel(
            agent="CIT Implementation Agent",
            message=(
                f"CIT Implementation Agent completed: {len(implemented)} fix(es) applied, "
                f"{len(skipped)} skipped (needs human review). "
                f"Fixes: {', '.join(report_items[i]['title'][:40] for i, _ in enumerate(implemented))}. "
                f"Full report: data/cit_implementation_report.json"
            ),
        )

    logger.info(
        f"CIT Implementation Agent done: {len(implemented)} implemented, "
        f"{len(skipped)} skipped/needs-human"
    )
    return report


def print_report():
    if not REPORT_PATH.exists():
        print("No report yet. Run the agent first.")
        return
    report = json.loads(REPORT_PATH.read_text())
    print(f"\n=== CIT IMPLEMENTATION REPORT ===")
    print(f"Generated: {report.get('generated_at','?')}")
    print(f"Implemented: {report.get('implemented',0)}")
    print(f"Skipped/needs-human: {report.get('skipped',0)}")
    print(f"Queue total: {report.get('total_in_queue',0)}")
    for item in report.get("items", []):
        status = item.get("result","?")
        print(f"\n  [{status}] {item.get('title','?')[:70]}")
        if status == "IMPLEMENTED":
            print(f"    change: {item.get('change','')}")
        else:
            print(f"    reason: {item.get('reason',item.get('fix_hint',''))[:100]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report",  action="store_true")
    args = parser.parse_args()

    os.chdir(BASE_DIR)

    if args.report:
        print_report()
    else:
        run_implementation(dry_run=args.dry_run)
