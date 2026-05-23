#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — CONTINUOUS IMPROVEMENT TEAM (CIT)
# =============================================================================
#
# MISSION
# ───────
# Five elite agents operating 24/7. Their mandate is intentionally broad:
# find every bug, logic error, data gap, strategic weakness, and improvement
# opportunity across the entire firm — code, pipeline, strategy, and architecture.
#
# TEAM
# ────
#   CITDirector            Orchestrator. Synthesises all findings, applies the
#                          sensitivity filter, manages the board proposal queue,
#                          and routes confidential items to the Chairman-only track.
#
#   CITCodeInspector       Source-code audit: bugs, silent failures, dead code,
#                          data-contract mismatches, hardcoded values, missing
#                          error handling, inconsistent field names.
#
#   CITPipelineMonitor     Live pipeline health: file freshness, dependency order,
#                          cadence failures, log error patterns, missing cron jobs.
#
#   CITStrategyAnalyst     Trading performance: win/loss patterns, sector biases,
#                          research-to-trade conversion, systematic approval biases.
#
#   CITArchitectureAdvisor System design: missing capabilities, redundancies,
#                          observability gaps, new signals, industry best practices.
#
#   CITThinkTank           Radical ideas. Assumes everything works correctly, then
#                          asks whether we're doing the right thing. Proposes
#                          structural overhauls, strategy pivots, unconventional
#                          signal sources, and first-principles challenges to the
#                          firm's operating model. Findings go to a separate
#                          Chairman-review queue (data/cit_think_tank.json).
#
# SCHEDULE
# ────────
#   Runs twice daily (6:00 AM ET + 6:00 PM ET) via cron.
#   Findings queue up in data/cit_pending_proposals.json.
#   Board reviews the queue at its next scheduled meeting.
#
# PRECISION MANDATE
# ─────────────────
#   Every finding reaching the board must cite specific, verifiable evidence:
#   a file:line reference, a concrete metric, or a named data path. Vague
#   findings ("this might fail", "seems slow") are automatically demoted to
#   the backlog regardless of confidence score.
#
# ROUTING
# ───────
#   Proposals ≥70% confidence + precise evidence → data/cit_pending_proposals.json (board vote)
#   Proposals  <70% confidence or vague evidence → data/cit_backlog.json (Director backlog)
#   VISION proposals (ThinkTank)                 → data/cit_think_tank.json (Chairman review)
#   Sensitive findings                           → data/cit_sensitive.json (Director + Chairman only)
#   Urgent sensitive items                       → email to Chairman immediately
#
# SENSITIVITY RULES (hard-coded — never route these to the board)
# ───────────────────────────────────────────────────────────────
#   • IRM independently evaluating other board members
#   • HR secretly evaluating the IRM (sealed base64 annex mechanism)
#   • The CoS not knowing it is being evaluated
#   • Any content from hr_irm_sealed.json
#   • The specific identities in any evaluation chain
#
# BOARD VOTE TIERS
# ────────────────
#   QUICK_WIN   → trivial change; Director (Claude) implements at next interaction
#                 without board vote
#   BUG_FIX     → board votes; simple majority (3/4) approves
#   IMPROVEMENT → board votes; simple majority (3/4) approves
#   STRATEGY    → board votes; unanimous (4/4) required (capital/strategy impact)
#   NEW_FEATURE → board votes; unanimous (4/4) required
#   SECURITY    → Director implements immediately; board notified after
#
# =============================================================================

import glob
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cit")

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
DATA_DIR         = BASE_DIR / "data"
SRC_FILES        = sorted(BASE_DIR.glob("*.py"))
LOG_DIR          = BASE_DIR / "logs"

CIT_MORNING_RPT  = DATA_DIR / "cit_morning_report.json"
CIT_EVENING_RPT  = DATA_DIR / "cit_evening_report.json"
CIT_PENDING      = DATA_DIR / "cit_pending_proposals.json"
CIT_BACKLOG      = DATA_DIR / "cit_backlog.json"
CIT_SENSITIVE    = DATA_DIR / "cit_sensitive.json"
CIT_APPROVED     = DATA_DIR / "cit_approved_proposals.json"
CIT_HISTORY      = DATA_DIR / "cit_history.json"     # acceptance rate per agent
CIT_HR_MEMO      = DATA_DIR / "cit_hr_memo.json"     # CIT → HR context channel
CIT_THINK_TANK   = DATA_DIR / "cit_think_tank.json"  # ThinkTank radical proposals

# ── Confidence threshold ─────────────────────────────────────────────────────
# Raised to 70 — precision requirement: findings must have specific, verifiable
# evidence (file:line, data value, or metric) before reaching the board.
BOARD_CONFIDENCE_THRESHOLD = 70

# ── Precision rules ───────────────────────────────────────────────────────────
# A finding is "precise" if its evidence field contains at least one of:
#   • a file:line reference (e.g. "main.py:42")
#   • a concrete metric (e.g. "124h", "5577 ERROR", "$97k")
#   • a specific data path (e.g. "data/hr_report.json")
# Vague evidence ("seems slow", "might fail") drops the finding to backlog.
_PRECISE_EVIDENCE_RE = re.compile(
    r'(\w+\.py:\d+|data/\w+\.json|\d+h\s+old|\d+\s+ERROR|\d+\s+WARNING'
    r'|\$[\d,]+|\d+\.\d+%|\d+/\d+\s+agent|\bline\s+\d+\b)',
    re.IGNORECASE,
)

# ── Vague proposed_fix patterns — routes to backlog ───────────────────────────
# A proposed_fix that only says "review", "consider", or "look at" without
# naming a specific function, variable, or code action is not actionable.
_VAGUE_FIX_RE = re.compile(
    r'^(review|consider|check|look at|investigate|examine|inspect|monitor|'
    r'ensure|verify|update|improve|fix|address)\s+\w+[\w\s]*\.$',
    re.IGNORECASE,
)

def _fix_is_specific(proposed_fix: str) -> bool:
    """Return True if proposed_fix names something concrete enough to act on."""
    if not proposed_fix or len(proposed_fix) < 30:
        return False
    # Must mention a file, function, variable, or concrete code action
    concrete = re.compile(
        r'(\w+\.py|def\s+\w+|\w+\(\)|data/\w+|`[^`]+`|:\s*\d+|'
        r'line\s+\d+|import\s+\w+|except\s+\w+|if\s+\w+|return\s+)',
        re.IGNORECASE,
    )
    return bool(concrete.search(proposed_fix))

# ── Sensitivity patterns (any match → confidential route only) ───────────────
# These describe the secret evaluation chains within the firm.
# The CIT must never surface these to the board — only the Director sees them.
SENSITIVE_PATTERNS: list = [
    r"irm.*seal",          r"seal.*irm",
    r"hr_irm",             r"irm_sealed",
    r"sealed.*b64",        r"b64.*sealed",
    r"sealed.*annex",      r"annex.*sealed",
    r"irm.*grade",         r"grade.*irm",
    r"irm.*evaluat",       r"evaluat.*irm",
    r"secret.*evaluat",    r"evaluat.*secret",
    r"cos.*not.*know",     r"not.*know.*cos",
    r"irm.*board.*member", r"board.*member.*irm",
    r"hr.*evaluat.*irm",   r"irm.*evaluat.*hr",
    r"independent.*risk.*manager.*grade",
    r"four.*person.*irm",  r"five.*person.*irm",
    r"hr_irm_sealed",
]

_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]


def _is_sensitive(text: str) -> bool:
    """Return True if any sensitive pattern matches the text."""
    return any(rx.search(text) for rx in _SENSITIVE_RE)


def _load_json_safe(path) -> dict:
    """Load a JSON file, returning {} on any failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_json_list_safe(path) -> list:
    """Load a JSON file expected to be a list, returning [] on any failure."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _file_age_hours(path) -> Optional[float]:
    """Return how old a file's generated_at timestamp is in hours, or None."""
    try:
        data = _load_json_safe(path)
        ts   = data.get("generated_at") or data.get("timestamp") or data.get("last_updated")
        if ts:
            dt = datetime.fromisoformat(str(ts)[:19])
            return (datetime.now() - dt).total_seconds() / 3600
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except Exception:
        return None


def _next_id(prefix: str, existing: list) -> str:
    """Generate the next sequential finding ID (e.g. CIT-2026-042)."""
    year = datetime.now().year
    used = {f.get("id", "") for f in existing}
    n    = 1
    while True:
        candidate = f"{prefix}-{year}-{n:03d}"
        if candidate not in used:
            return candidate
        n += 1


# =============================================================================
# AGENT 1 — CODE INSPECTOR
# =============================================================================

class CITCodeInspector:
    """
    Reads every Python source file and flags code-quality issues:
    bugs, silent failures, dead code, data-contract mismatches,
    hardcoded magic values, and missing error handling.
    """

    name = "CITCodeInspector"

    # File patterns that indicate the finding refers to sensitive topics
    _SKIP_FILES = {"head_agent.py", "cit_director.py"}   # archived or self — don't audit

    def analyze(self) -> list[dict]:
        findings = []

        for src_path in SRC_FILES:
            if src_path.name in self._SKIP_FILES:
                continue
            try:
                with open(src_path) as f:
                    lines = f.readlines()
                src_text = "".join(lines)
            except Exception:
                continue

            fname = src_path.name

            # ── 1. Bare `except: pass` or `except Exception: pass` ──────────
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped in ("except: pass", "except Exception: pass",
                                "except Exception as e: pass"):
                    findings.append({
                        "agent":        self.name,
                        "category":     "BUG_FIX",
                        "severity":     "MEDIUM",
                        "confidence":   80,
                        "title":        f"Silent exception in {fname}:{i}",
                        "description":  f"`{stripped}` swallows all errors silently — downstream code may produce wrong results without any log entry.",
                        "evidence":     f"{fname} line {i}: `{stripped}`",
                        "proposed_fix": f"Replace with `except Exception as e: logger.warning(f'...: {{e}}')` at minimum.",
                        "auto_implementable": False,
                    })

            # ── 2. `print()` calls (should be logger) ──────────────────────
            # Only flag print() calls that are in real production logic —
            # NOT in dedicated display/CLI functions (print_*, *_print, print_report,
            # print_status, _print_*) or __main__ blocks, which are intentional.
            _DISPLAY_FUNC_PREFIXES = ("def print_", "def _print_", "def print_report",
                                      "def print_status", "def _print_report")

            # Find line ranges for __main__ block and display functions to exclude
            main_start = next(
                (i for i, l in enumerate(lines) if "__name__" in l and "__main__" in l),
                len(lines) + 1
            )
            # Find display function ranges — everything inside a def print_*() block
            _display_lines: set = set()
            _in_display = False
            _display_indent: int | None = None
            for i, l in enumerate(lines):
                stripped = l.rstrip()
                if any(stripped.lstrip().startswith(pfx) for pfx in _DISPLAY_FUNC_PREFIXES):
                    _in_display = True
                    _display_indent = len(l) - len(l.lstrip())
                    _display_lines.add(i + 1)
                elif _in_display:
                    if stripped and (len(l) - len(l.lstrip())) <= _display_indent and not stripped.lstrip().startswith(("#", ")")):
                        _in_display = False   # back to same/outer indent = exited function
                    else:
                        _display_lines.add(i + 1)

            print_lines = [
                (i, l.strip()) for i, l in enumerate(lines, 1)
                if re.match(r'^\s*print\(', l)
                and not l.strip().startswith("#")
                and i < main_start
                and i not in _display_lines
            ]
            for lineno, text in print_lines:
                findings.append({
                    "agent":        self.name,
                    "category":     "QUICK_WIN",
                    "severity":     "LOW",
                    "confidence":   75,
                    "title":        f"print() in production code: {fname}:{lineno}",
                    "description":  "print() calls bypass the structured logging system — output won't appear in log files or be captured by monitoring.",
                    "evidence":     f"{fname} line {lineno}: `{text[:80]}`",
                    "proposed_fix": f"Replace `print(...)` with `logger.info(...)` or `logger.debug(...)`.",
                    "auto_implementable": True,
                })

            # ── 3. TODO / FIXME / HACK comments ─────────────────────────────
            for i, line in enumerate(lines, 1):
                m = re.search(r'#\s*(TODO|FIXME|HACK|XXX)\b(.{0,80})', line, re.IGNORECASE)
                if m:
                    tag   = m.group(1).upper()
                    note  = m.group(2).strip()
                    findings.append({
                        "agent":        self.name,
                        "category":     "BUG_FIX",
                        "severity":     "LOW",
                        "confidence":   65,
                        "title":        f"{tag} unresolved in {fname}:{i}",
                        "description":  f"Unresolved {tag} comment: '{note}'. These accumulate and become technical debt.",
                        "evidence":     f"{fname} line {i}",
                        "proposed_fix": f"Address the {tag} or convert to a tracked issue.",
                        "auto_implementable": False,
                    })

            # ── 4. Missing makedirs before open(..., "w") ────────────────────
            for i, line in enumerate(lines, 1):
                if re.search(r'open\(.+,\s*["\']w["\']\)', line) and "makedirs" not in src_text:
                    findings.append({
                        "agent":        self.name,
                        "category":     "BUG_FIX",
                        "severity":     "MEDIUM",
                        "confidence":   55,
                        "title":        f"Possible missing makedirs before file write in {fname}:{i}",
                        "description":  f"File opened for writing but `os.makedirs` not found anywhere in this file. If the output directory doesn't exist, the write will crash.",
                        "evidence":     f"{fname} line {i}: `{line.strip()[:80]}`",
                        "proposed_fix": "Add `os.makedirs(os.path.dirname(path), exist_ok=True)` before the write.",
                        "auto_implementable": False,
                    })
                    break   # one finding per file is enough

            # ── 5. f-string in logger with heavy computation ─────────────────
            for i, line in enumerate(lines, 1):
                if re.search(r'logger\.(debug|info)\(f["\']', line) and len(line) > 120:
                    findings.append({
                        "agent":        self.name,
                        "category":     "QUICK_WIN",
                        "severity":     "LOW",
                        "confidence":   60,
                        "title":        f"Long f-string in logger.debug/info: {fname}:{i}",
                        "description":  "Very long log strings are evaluated even when the log level means they won't be emitted. Use lazy % formatting or guard with `if logger.isEnabledFor(...)`.",
                        "evidence":     f"{fname} line {i}: `{line.strip()[:80]}...`",
                        "proposed_fix": "Convert to `logger.info('message %s', var)` style.",
                        "auto_implementable": False,
                    })
                    break

        # ── 6a. Hardcoded ticker names in action IDs ─────────────────────────
        # Detects action IDs with a literal VERB_TICKER form (e.g. CLOSE_BA, TRIM_SH)
        # that do not use an f-string — causes actions on wrong tickers when the
        # position changes. Fix: use f"CLOSE_{ticker}" with the actual variable.
        for src_path in SRC_FILES:
            if src_path.name in self._SKIP_FILES:
                continue
            try:
                with open(src_path) as f:
                    lines = f.readlines()
                fname = src_path.name
                for i, line in enumerate(lines, 1):
                    # Match "id": "VERB_TICKER" where ticker is 2-5 uppercase letters
                    # and the line does NOT contain an f-string or variable
                    m = re.search(
                        r'"id"\s*:\s*"((?:CLOSE|SELL|TRIM|BUY|ADD|REDUCE)_[A-Z]{1,5})"',
                        line
                    )
                    if m and 'f"' not in line and "f'" not in line and '{' not in line:
                        action_id = m.group(1)
                        findings.append({
                            "agent":        self.name,
                            "category":     "BUG_FIX",
                            "severity":     "HIGH",
                            "confidence":   85,
                            "title":        f"Hardcoded ticker in action ID: {fname}:{i}",
                            "description":  (
                                f"Action ID `{action_id}` uses a hardcoded ticker name. "
                                "If the worst/flagged position changes, this ID won't match "
                                "the actual ticker and the board action will be a no-op."
                            ),
                            "evidence":     f"{fname} line {i}: `{line.strip()[:80]}`",
                            "proposed_fix": (
                                f"Use an f-string with the actual variable: "
                                f'`f"CLOSE_{{worst_ticker}}"` instead of `"{action_id}"`.'
                            ),
                            "auto_implementable": False,
                        })
            except Exception:
                continue

        # ── 6b. Action IDs identical across a loop (vote_weight key collision) ─
        for src_path in SRC_FILES:
            if src_path.name in self._SKIP_FILES:
                continue
            try:
                with open(src_path) as f:
                    src_text = f.read()
                fname = src_path.name
                # Look for for-loop bodies that add to vote_weight with a non-unique key
                loop_blocks = re.findall(
                    r'for\s+\w+\s+in\s+.+?(?=\nfor\s|\ndef\s|\nclass\s|\Z)',
                    src_text, re.DOTALL
                )
                for block in loop_blocks:
                    # Find vote_weight assignments with static strings (not f-strings)
                    static_keys = re.findall(
                        r'vote_weight\[["\'](TRIM_\w+|CLOSE_\w+|SELL_\w+)["\'\]]\s*=',
                        block
                    )
                    if len(static_keys) > 1:
                        findings.append({
                            "agent":        self.name,
                            "category":     "BUG_FIX",
                            "severity":     "HIGH",
                            "confidence":   75,
                            "title":        f"vote_weight key collision in loop: {fname}",
                            "description":  (
                                f"vote_weight is assigned with static keys {static_keys} "
                                "inside a loop. Later iterations will silently overwrite "
                                "earlier entries — only the last agent's vote is recorded."
                            ),
                            "evidence":     f"{fname}: static vote_weight keys in loop body",
                            "proposed_fix": (
                                "Use a dynamic key per iteration: "
                                "`vote_weight[f'TRIM_{{ticker}}'] = ...`"
                            ),
                            "auto_implementable": False,
                        })
                        break  # one finding per file
            except Exception:
                continue

        # ── 6. Cross-file data-contract spot checks ──────────────────────────
        # Check that hr_agent.py and irm_review_agent.py agree on key field names
        hr_src = (BASE_DIR / "hr_agent.py").read_text() if (BASE_DIR / "hr_agent.py").exists() else ""
        irm_src = (BASE_DIR / "irm_review_agent.py").read_text() if (BASE_DIR / "irm_review_agent.py").exists() else ""

        if hr_src and irm_src:
            # IRM should read hr_report.json; check field names
            hr_writes  = set(re.findall(r'"(\w+)":\s*(?:action_required|firm_health)', hr_src))
            irm_reads  = set(re.findall(r'hr_report\.get\(["\'](\w+)["\']', irm_src))
            mismatches = irm_reads - set(re.findall(r'"([a-z_]+)":', hr_src))
            for key in mismatches:
                if not _is_sensitive(key):
                    findings.append({
                        "agent":        self.name,
                        "category":     "BUG_FIX",
                        "severity":     "HIGH",
                        "confidence":   70,
                        "title":        f"Possible data contract mismatch: irm_review_agent reads '{key}' from hr_report",
                        "description":  f"irm_review_agent.py calls `hr_report.get('{key}')` but this key may not be written by hr_agent.py. Silent None returns cause downstream logic to silently skip HR data.",
                        "evidence":     "irm_review_agent.py + hr_agent.py",
                        "proposed_fix": f"Verify hr_agent.py writes '{key}' into hr_report.json output, or update irm_review_agent.py to use the correct key.",
                        "auto_implementable": False,
                    })

        # ── Save findings to data/cit_code_inspection.json ────────────────
        try:
            DATA_DIR.mkdir(exist_ok=True)
            _inspection_record = {
                "generated_at":  datetime.now().isoformat(),
                "files_scanned": len(SRC_FILES),
                "total_findings": len(findings),
                "findings":       findings,
                "severity_counts": {
                    "CRITICAL": sum(1 for f in findings if f.get("severity") == "CRITICAL"),
                    "HIGH":     sum(1 for f in findings if f.get("severity") == "HIGH"),
                    "MEDIUM":   sum(1 for f in findings if f.get("severity") == "MEDIUM"),
                    "LOW":      sum(1 for f in findings if f.get("severity") == "LOW"),
                },
            }
            with open(DATA_DIR / "cit_code_inspection.json", "w") as _cf:
                json.dump(_inspection_record, _cf, indent=2, default=str)
        except Exception as _save_err:
            logger.debug(f"  [CITCodeInspector] Could not save inspection report: {_save_err}")

        return findings


# =============================================================================
# AGENT 2 — PIPELINE MONITOR
# =============================================================================

class CITPipelineMonitor:
    """
    Checks live pipeline health: file freshness, dependency order,
    log error/warning patterns, and missing scheduled jobs.
    Complements PipelineOpsAnalyst (IRM) but focuses on improvement
    opportunities rather than just compliance.
    """

    name = "CITPipelineMonitor"

    # Expected output files and max acceptable age (hours)
    EXPECTED_FILES = {
        "data/risk_bulletin.json":            26,
        "data/review_recommendations.json":   26,
        "data/manager_decisions.json":        26,
        "data/capital_efficiency_report.json":26,
        "data/rd_system_performance.json":    26,
        "data/chief_of_staff_report.json":    26,
        "data/hr_report.json":                26,
        "data/irm_daily_check.json":          26,
        "data/canary_state.json":             13,
        "data/research_team_report.json":     96,
        "data/energy_research_report.json":   96,
        "data/cit_pending_proposals.json":    50,  # CIT itself should run every 12h
    }

    def analyze(self) -> list[dict]:
        findings = []

        # ── 1. Stale output files ────────────────────────────────────────────
        for path_str, max_age in self.EXPECTED_FILES.items():
            path = BASE_DIR / path_str
            if not path.exists():
                if path_str != "data/cit_pending_proposals.json":  # new — ok if missing
                    findings.append({
                        "agent":        self.name,
                        "category":     "BUG_FIX",
                        "severity":     "MEDIUM",
                        "confidence":   75,
                        "title":        f"Missing output file: {path_str}",
                        "description":  f"Expected output file {path_str} does not exist. The agent that produces this may not have run yet, or its path has changed.",
                        "evidence":     f"{path_str} does not exist on disk",
                        "proposed_fix": "Run the pipeline once to generate the file, or verify the output path in the agent.",
                        "auto_implementable": False,
                    })
                continue
            age = _file_age_hours(path)
            if age is not None and age > max_age * 1.5:
                findings.append({
                    "agent":        self.name,
                    "category":     "BUG_FIX",
                    "severity":     "HIGH" if max_age <= 26 else "MEDIUM",
                    "confidence":   85,
                    "title":        f"Severely stale output: {path_str} ({age:.0f}h old)",
                    "description":  f"{path_str} is {age:.0f}h old against a {max_age}h threshold. This is 50% beyond tolerance — the producing agent likely missed multiple cycles.",
                    "evidence":     f"File age: {age:.0f}h. Threshold: {max_age}h.",
                    "proposed_fix": "Check cron logs for this agent. Verify it is scheduled and running without silent failures.",
                    "auto_implementable": False,
                })

        # ── 2. Recent log errors ─────────────────────────────────────────────
        error_count = 0
        warning_count = 0
        error_samples = []
        if LOG_DIR.exists():
            today = datetime.now().strftime("%Y%m%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            for log_file in LOG_DIR.glob("*.log"):
                if today in log_file.name or yesterday in log_file.name:
                    # Skip CIT's own log — it contains meta-lines like
                    # "142 ERROR lines in recent logs" which inflate the count.
                    if "cit_" in log_file.name:
                        continue
                    try:
                        text = log_file.read_text(errors="replace")
                        for line in text.splitlines():
                            if "  ERROR " in line and "ERROR lines" not in line:
                                error_count += 1
                                if len(error_samples) < 3:
                                    error_samples.append(line.strip()[:120])
                            elif "  WARNING " in line:
                                warning_count += 1
                    except Exception:
                        pass

        if error_count > 5:
            # Group samples by error type to identify the dominant failure mode
            # and generate a specific, actionable proposed_fix.
            error_types: dict[str, list[str]] = {}
            for s in error_samples:
                m = re.search(r'ERROR\s+([A-Za-z][\w.]+(?:Error|Exception|Warning|Fail\w*)?)', s)
                key = m.group(1) if m else s.split()[-1][:30]
                error_types.setdefault(key, []).append(s)
            top_type = max(error_types, key=lambda k: len(error_types[k]))
            top_sample = error_types[top_type][0]
            specific_fix = (
                f"Most frequent error type: `{top_type}` "
                f"({len(error_types[top_type])}/{len(error_samples)} sampled). "
                f"Locate the call site in logs/run_*.log matching "
                f"`{top_sample[:60]}`, wrap it in `except {top_type}` "
                f"and demote to `logger.warning()` if this is expected noise, "
                f"or add retry logic with `time.sleep(2)` before re-raising."
            )
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "HIGH" if error_count > 20 else "MEDIUM",
                "confidence":   90,
                "title":        f"{error_count} ERROR lines — dominant: `{top_type}`",
                "description":  (
                    f"The last 24-48h of logs contain {error_count} ERROR-level entries "
                    f"({warning_count} WARNINGs). Most frequent error class: `{top_type}` "
                    f"({len(error_types[top_type])} of {len(error_samples)} sampled). "
                    f"Systematic errors indicate an unhandled failure mode."
                ),
                "evidence":     "Recent log samples:\n" + "\n".join(f"  • {s}" for s in error_samples),
                "proposed_fix": specific_fix,
                "auto_implementable": False,
            })

        # ── 3. State.json growth check ───────────────────────────────────────
        state_path = DATA_DIR / "state.json"
        if state_path.exists():
            state = _load_json_safe(state_path)
            buy_log  = state.get("buy_log",  [])
            sell_log = state.get("sell_log", [])
            if len(buy_log) > 500 or len(sell_log) > 500:
                findings.append({
                    "agent":        self.name,
                    "category":     "IMPROVEMENT",
                    "severity":     "LOW",
                    "confidence":   80,
                    "title":        f"state.json logs growing large ({len(buy_log)} buys, {len(sell_log)} sells)",
                    "description":  "buy_log and sell_log accumulate indefinitely in state.json. Large logs slow JSON parse time and increase memory usage. After 30 days of trading, old entries have no operational value.",
                    "evidence":     f"buy_log: {len(buy_log)} entries, sell_log: {len(sell_log)} entries",
                    "proposed_fix": "Archive logs older than 90 days to data/trade_archive.json and keep only the last 100 entries in state.json.",
                    "auto_implementable": False,
                })

        # ── 4. Backup directory size ─────────────────────────────────────────
        backup_dir = DATA_DIR / "backups"
        if backup_dir.exists():
            n_backups = len([d for d in backup_dir.iterdir() if d.is_dir()])
            if n_backups == 0:
                findings.append({
                    "agent":        self.name,
                    "category":     "BUG_FIX",
                    "severity":     "MEDIUM",
                    "confidence":   85,
                    "title":        "No data backups found in data/backups/",
                    "description":  "backup_data.py is wired into the pipeline but data/backups/ is empty. Either the pipeline hasn't run since backup_data.py was added, or the backup step is silently failing.",
                    "evidence":     f"data/backups/ exists but contains 0 backup directories",
                    "proposed_fix": "Run `python3 backup_data.py` manually to verify it works, then check pipeline logs for backup errors.",
                    "auto_implementable": False,
                })

        # ── 5. Agent zero-pick streak audit (CIT → HR memo) ─────────────────
        # Read agent registry and cross-reference canary + CRO state so HR gets
        # proper context before penalising agents for market-condition blockages.
        registry   = _load_json_safe(DATA_DIR / "agent_registry.json")
        canary     = _load_json_safe(DATA_DIR / "canary_state.json")
        risk_rpt   = _load_json_safe(DATA_DIR / "risk_manager_report.json")
        sector_asgn = _load_json_safe(DATA_DIR / "sector_assignments.json")

        canary_level = canary.get("last_level", "GREEN")
        # CRO level is nested under risk_assessment
        cro_level    = (risk_rpt.get("risk_assessment") or {}).get("risk_level") or risk_rpt.get("risk_level", "UNKNOWN")
        # Also treat as blocked if canary fired ORANGE/RED at any point today
        alerts_today = canary.get("alerts_today", [])
        canary_fired_today = any(a.get("level") in ("ORANGE", "RED") for a in alerts_today)
        buys_blocked = (
            canary_level in ("ORANGE", "RED")
            or canary_fired_today
            or cro_level in ("CRITICAL",)
        )

        assigned_sectors = {
            a.get("sector_name", "").lower()
            for a in sector_asgn.get("assignments", [])
        }

        # Build sector → agent_class mapping for R&D research agents
        SECTOR_MAP = {
            "biotechnology":          "BiotechResearchAgent",
            "biotech":                "BiotechResearchAgent",
            "consumer discretionary": "ConsumerDiscretionaryAgent",
            "financials":             "FinancialSectorAgent",
            "technology":             "GlobalTechAgent",
            "healthcare":             "HealthcarePharmaAgent",
            "industrials":            "IndustrialsDefenseAgent",
            "materials":              "MaterialsMiningAgent",
            "small cap":              "SmallCapGrowthAgent",
            "communications":         "CommunicationsMediaAgent",
            "crypto":                 "CryptoEquitiesAgent",
        }
        assigned_agent_classes = set()
        for sector in assigned_sectors:
            for key, cls in SECTOR_MAP.items():
                if key in sector:
                    assigned_agent_classes.add(cls)

        streak_agents   = []
        exonerated      = []
        genuine_concern = []

        for agent in registry.get("agents", []):
            czc = agent.get("consecutive_zero_cycles", 0)
            if czc < 4:
                continue
            cn     = agent.get("class_name", "?")
            role   = agent.get("role", "")
            reason = []

            if buys_blocked:
                reason.append(
                    f"canary={canary_level} (fired ORANGE today={canary_fired_today}), CRO={cro_level} — "
                    f"all capital deployment blocked system-wide; zero trades expected regardless of pick quality"
                )
            is_research_agent = any(k in role.lower() for k in SECTOR_MAP)
            if is_research_agent and cn not in assigned_agent_classes:
                reason.append("sector was not in top-8 momentum ranking this cycle — agent was never assigned")

            entry = {"class_name": cn, "consecutive_zero_cycles": czc,
                     "status": agent.get("status", "active"), "role": role,
                     "exoneration_reasons": reason}
            streak_agents.append(entry)
            if reason:
                exonerated.append(entry)
            else:
                genuine_concern.append(entry)

        # Write HR memo so HR can factor this context into evaluations
        memo = {
            "generated_at":    datetime.now().isoformat(),
            "canary_level":    canary_level,
            "cro_level":       cro_level,
            "buys_blocked":    buys_blocked,
            "assigned_sectors": sorted(assigned_sectors),
            "streak_agents":   streak_agents,
            "exonerated":      exonerated,
            "genuine_concern": genuine_concern,
            "cit_note": (
                "These agents show zero-pick streaks. CIT has cross-referenced "
                "canary state, CRO risk level, and sector assignments. "
                "Exonerated agents should NOT be penalised — their sectors are "
                "either blocked market-wide or simply weren't assigned this cycle. "
                "Genuine concern agents had no such mitigating factor."
            ),
        }
        with open(CIT_HR_MEMO, "w") as f:
            json.dump(memo, f, indent=2, default=str)

        if streak_agents:
            n_exon = len(exonerated)
            n_real = len(genuine_concern)
            severity = "HIGH" if n_real > 0 else "MEDIUM"
            findings.append({
                "agent":       self.name,
                "category":    "BUG_FIX",
                "severity":    severity,
                "confidence":  88,
                "title":       f"Agent zero-pick streaks: {n_exon} exonerated, {n_real} genuine concern — HR memo written",
                "description": (
                    f"{len(streak_agents)} agent(s) have ≥4 consecutive zero-pick cycles. "
                    f"CIT diagnosis: {n_exon} are exonerated (buys blocked system-wide by "
                    f"canary={canary_level}/CRO={cro_level}, or sector not assigned this cycle). "
                    f"{n_real} show genuine underperformance with no mitigating factor. "
                    f"Full context written to data/cit_hr_memo.json for HR to read."
                ),
                "evidence": (
                    f"Canary: {canary_level} | CRO: {cro_level} | Buys blocked: {buys_blocked}\n"
                    + (f"Exonerated: {[e['class_name'] for e in exonerated]}\n" if exonerated else "")
                    + (f"Genuine concern: {[g['class_name'] for g in genuine_concern]}" if genuine_concern else "")
                ),
                "proposed_fix": (
                    "HR should read data/cit_hr_memo.json before penalising zero-pick agents. "
                    "Exonerated agents need no action — they are blocked by market conditions, not underperforming. "
                    "Genuine concern agents should receive targeted development plans."
                ),
                "auto_implementable": False,
                "for_hr": True,
            })

        return findings


# =============================================================================
# AGENT 3 — STRATEGY ANALYST
# =============================================================================

class CITStrategyAnalyst:
    """
    Analyses trading performance data to identify patterns, biases,
    and improvement opportunities in the firm's strategy execution.
    Reads state.json, manager_decisions.json, and review output.
    """

    name = "CITStrategyAnalyst"

    def analyze(self) -> list[dict]:
        findings = []

        state      = _load_json_safe(DATA_DIR / "state.json")
        mgr_data   = _load_json_safe(DATA_DIR / "manager_decisions.json")
        review_data= _load_json_safe(DATA_DIR / "review_recommendations.json")

        buy_log  = state.get("buy_log",  [])
        sell_log = state.get("sell_log", [])

        # ── 1. Win rate calculation ──────────────────────────────────────────
        if sell_log:
            wins   = [t for t in sell_log if (t.get("pnl_pct") or 0) > 0]
            losses = [t for t in sell_log if (t.get("pnl_pct") or 0) < 0]
            win_rate = len(wins) / len(sell_log) * 100 if sell_log else 0
            avg_win  = sum(t.get("pnl_pct", 0) for t in wins)  / len(wins)  if wins   else 0
            avg_loss = sum(t.get("pnl_pct", 0) for t in losses)/ len(losses) if losses else 0

            if win_rate < 40 and len(sell_log) >= 5:
                findings.append({
                    "agent":        self.name,
                    "category":     "STRATEGY",
                    "severity":     "HIGH",
                    "confidence":   85,
                    "title":        f"Win rate below 40% ({win_rate:.0f}% across {len(sell_log)} closed trades)",
                    "description":  f"The firm's closed-trade win rate is {win_rate:.0f}% (avg win: +{avg_win:.1f}%, avg loss: {avg_loss:.1f}%). Below-40% win rate with these average magnitudes suggests the strategy is accepting too many marginal setups.",
                    "evidence":     f"{len(wins)} winners, {len(losses)} losers out of {len(sell_log)} closed trades",
                    "proposed_fix": "Tighten BUY_NEW approval criteria — require higher conviction scores (4★+ only) and stronger PDO signal. Review the 5 most recent losers for common entry patterns.",
                    "auto_implementable": False,
                })
            elif win_rate > 70 and len(sell_log) >= 5:
                findings.append({
                    "agent":        self.name,
                    "category":     "IMPROVEMENT",
                    "severity":     "LOW",
                    "confidence":   70,
                    "title":        f"High win rate ({win_rate:.0f}%) — possible opportunity to loosen position sizing",
                    "description":  f"Win rate of {win_rate:.0f}% suggests the current entry criteria may be too conservative — we're winning often but potentially leaving returns on the table by sizing too small.",
                    "evidence":     f"{len(wins)} winners out of {len(sell_log)} trades",
                    "proposed_fix": "Consider modestly increasing MAX_POSITION_SIZE_PCT or the conviction multiplier for 4★+ picks. Run a backtest with 12% max position size to quantify the delta.",
                    "auto_implementable": False,
                })

        # ── 2. Hold time analysis ────────────────────────────────────────────
        if sell_log:
            hold_times = []
            for t in sell_log:
                # Approximate hold time from buy_log match
                ticker = t.get("ticker")
                sell_date = t.get("date", "")
                buy_entry = next(
                    (b for b in reversed(buy_log) if b.get("ticker") == ticker), None
                )
                if buy_entry and sell_date and buy_entry.get("date"):
                    try:
                        d1 = datetime.fromisoformat(buy_entry["date"])
                        d2 = datetime.fromisoformat(sell_date)
                        hold_times.append((d2 - d1).days)
                    except Exception:
                        pass
            if hold_times:
                avg_hold = sum(hold_times) / len(hold_times)
                if avg_hold < 5:
                    findings.append({
                        "agent":        self.name,
                        "category":     "STRATEGY",
                        "severity":     "MEDIUM",
                        "confidence":   72,
                        "title":        f"Average hold time very short ({avg_hold:.1f} days)",
                        "description":  "The firm is closing positions very quickly on average. Short holds increase transaction costs and may indicate the Review Agent is too trigger-happy with SELL signals.",
                        "evidence":     f"Average hold across {len(hold_times)} measured trades: {avg_hold:.1f} days",
                        "proposed_fix": "Review the SELL criteria in review_agent.py — consider raising the minimum hold period for non-stop-loss sells from 0 to 3 days.",
                        "auto_implementable": False,
                    })

        # ── 3. Manager rejection rate analysis ──────────────────────────────
        mgr_decisions = mgr_data.get("decisions", []) if isinstance(mgr_data, dict) else []
        if not mgr_decisions and isinstance(mgr_data, list) and mgr_data:
            mgr_decisions = mgr_data[-1].get("decisions", [])

        if mgr_decisions:
            total     = len(mgr_decisions)
            approved  = sum(1 for d in mgr_decisions if d.get("manager_decision") == "APPROVED")
            rejected  = sum(1 for d in mgr_decisions if d.get("manager_decision") == "REJECTED")
            rej_rate  = rejected / total * 100 if total else 0

            if rej_rate > 70:
                findings.append({
                    "agent":        self.name,
                    "category":     "STRATEGY",
                    "severity":     "MEDIUM",
                    "confidence":   68,
                    "title":        f"Manager rejecting {rej_rate:.0f}% of recommendations this cycle",
                    "description":  f"The Manager Agent rejected {rejected}/{total} recommendations. High rejection rates may indicate overly conservative thresholds or that the Review Agent and Manager Agent are systematically misaligned.",
                    "evidence":     f"{approved} approved, {rejected} rejected out of {total} recommendations",
                    "proposed_fix": "Check if CRO or PDO signals are suppressing approvals. If market conditions are normal, consider whether the buy_bp_threshold in evaluate_recommendation is calibrated correctly.",
                    "auto_implementable": False,
                })

        # ── 4. Streak-based risk flag ────────────────────────────────────────
        loss_streak = state.get("loss_streak", 0)
        win_streak  = state.get("win_streak",  0)
        if loss_streak >= 4:
            findings.append({
                "agent":        self.name,
                "category":     "STRATEGY",
                "severity":     "HIGH",
                "confidence":   90,
                "title":        f"Loss streak of {loss_streak} — strategy review warranted",
                "description":  f"The firm has logged {loss_streak} consecutive losing closes. This is statistically meaningful and may indicate the current strategy is misaligned with the prevailing market regime.",
                "evidence":     f"loss_streak={loss_streak} in state.json",
                "proposed_fix": "Convene a board review of recent losing trades. Consider temporarily shifting to HOLD_CASH mode until the losing pattern breaks. Review sector exposure vs. current macro regime.",
                "auto_implementable": False,
            })

        # ── 5. Research-to-trade conversion check ────────────────────────────
        research_data = _load_json_safe(DATA_DIR / "research_team_report.json")
        research_picks = research_data.get("recommendations", [])
        if research_picks and buy_log:
            recent_buys = {b.get("ticker") for b in buy_log[-20:]}
            research_tickers = {r.get("ticker") for r in research_picks}
            traded = research_tickers & recent_buys
            conversion_rate = len(traded) / len(research_tickers) * 100 if research_tickers else 0

            if conversion_rate < 15 and len(research_tickers) >= 5:
                findings.append({
                    "agent":        self.name,
                    "category":     "STRATEGY",
                    "severity":     "MEDIUM",
                    "confidence":   65,
                    "title":        f"Research picks have low trade conversion ({conversion_rate:.0f}%)",
                    "description":  f"Only {len(traded)} of {len(research_tickers)} recent research picks were actually traded. Low conversion suggests either the Manager Agent thresholds are blocking good picks, or R&D picks need quality improvement.",
                    "evidence":     f"Traded: {sorted(traded)[:5]}. Untraded picks: {sorted(research_tickers - recent_buys)[:5]}",
                    "proposed_fix": "Audit the top 5 untraded research picks — were they blocked by PDO, CVaR, canary, or buying power? If blocked by capital constraints, consider whether the portfolio is too concentrated.",
                    "auto_implementable": False,
                })

        return findings


# =============================================================================
# AGENT 4 — ARCHITECTURE ADVISOR
# =============================================================================

class CITArchitectureAdvisor:
    """
    Thinks about the system at the architectural level: missing capabilities,
    redundancies, observability gaps, and structural improvements.
    Does not audit specific bugs — focuses on what the firm doesn't have yet.
    """

    name = "CITArchitectureAdvisor"

    def analyze(self) -> list[dict]:
        findings = []

        # ── 1. No circuit breaker for consecutive pipeline failures ──────────
        state = _load_json_safe(DATA_DIR / "state.json")
        findings.append({
            "agent":        self.name,
            "category":     "NEW_FEATURE",
            "severity":     "MEDIUM",
            "confidence":   75,
            "title":        "No consecutive pipeline failure circuit breaker",
            "description":  "If main.py crashes 3 runs in a row, there is no mechanism to automatically pause trading and alert the Chairman. The canary monitors market conditions but not pipeline health.",
            "evidence":     "No `consecutive_failures` counter in state.json; no alert on repeated cron failures.",
            "proposed_fix": "Add a `pipeline_failure_streak` counter to state.json. If it reaches 3, send a Chairman email and pause new buy orders for the next cycle.",
            "auto_implementable": False,
        })

        # ── 2. No portfolio attribution by signal source ─────────────────────
        buy_log = state.get("buy_log", [])
        sources = {b.get("source", "unknown") for b in buy_log}
        if len(sources) > 1 or (buy_log and "source" not in buy_log[0]):
            findings.append({
                "agent":        self.name,
                "category":     "IMPROVEMENT",
                "severity":     "MEDIUM",
                "confidence":   80,
                "title":        "No P&L attribution by signal source (13F vs R&D vs Energy)",
                "description":  "Trades come from multiple sources (13F signals, R&D picks, Energy Research picks) but sell_log doesn't track which source generated each position. We can't measure which signal generator is actually making money.",
                "evidence":     f"buy_log sources seen: {sorted(sources)}. sell_log has no source field.",
                "proposed_fix": "Add `source` to sell_log entries (carry forward from buy_log). Add a weekly P&L-by-source breakdown to the trade review report.",
                "auto_implementable": False,
            })

        # ── 3. No portfolio heat map / concentration dashboard ───────────────
        cos_report = _load_json_safe(DATA_DIR / "chief_of_staff_report.json")
        if not cos_report.get("sector_concentration"):
            findings.append({
                "agent":        self.name,
                "category":     "IMPROVEMENT",
                "severity":     "LOW",
                "confidence":   70,
                "title":        "No sector concentration heat map in daily reports",
                "description":  "The daily HTML report shows positions but not sector concentration. The CRO tracks concentration risk internally but it never appears visually in the Chairman's daily email.",
                "evidence":     "chief_of_staff_report.json has no sector_concentration key. report_generator.py doesn't render a concentration chart.",
                "proposed_fix": "Add a sector concentration table to report_generator.py's HTML output. Group open positions by sector, show % of portfolio per sector, and flag any sector above 25%.",
                "auto_implementable": False,
            })

        # ── 4. No integration test / smoke test for the pipeline ─────────────
        test_files = list(BASE_DIR.glob("test_*.py")) + list(BASE_DIR.glob("*_test.py"))
        if not test_files:
            findings.append({
                "agent":        self.name,
                "category":     "NEW_FEATURE",
                "severity":     "HIGH",
                "confidence":   95,
                "title":        "Zero test coverage — no integration tests for the pipeline",
                "description":  "There are no test files (test_*.py or *_test.py) in the codebase. Changes to core logic (evaluate_recommendation, enforce_risk_rules, _implement_decisions) can silently break the trading pipeline with no automated detection.",
                "evidence":     f"Searched {BASE_DIR} for test_*.py and *_test.py — none found.",
                "proposed_fix": "Create test_pipeline_smoke.py with at minimum: (1) mock evaluate_recommendation with known inputs and assert outputs, (2) mock enforce_risk_rules with a known position set, (3) verify state.json round-trips correctly.",
                "auto_implementable": False,
            })

        # ── 5. Cron schedule has no health check / heartbeat ─────────────────
        findings.append({
            "agent":        self.name,
            "category":     "IMPROVEMENT",
            "severity":     "MEDIUM",
            "confidence":   72,
            "title":        "No cron heartbeat — silent scheduler failure goes undetected",
            "description":  "The crontab runs main.py 4× daily but there is no external heartbeat ping (e.g. to healthchecks.io or a similar service). If the Mac goes to sleep, the cron misses its window silently. The only detection mechanism is the dead-man's switch (30h threshold), which fires too late.",
            "evidence":     "No healthchecks.io or similar ping call in main.py or cron.",
            "proposed_fix": "Add a 1-line `curl` call to a free healthchecks.io endpoint at the end of main.py's successful run. If the check misses two consecutive windows, healthchecks.io emails an alert immediately.",
            "auto_implementable": False,
        })

        # ── 6. No position-level risk/reward tracking ─────────────────────────
        findings.append({
            "agent":        self.name,
            "category":     "IMPROVEMENT",
            "severity":     "LOW",
            "confidence":   68,
            "title":        "Risk/reward ratio not tracked per position",
            "description":  "The firm tracks win/loss streak and P&L percentage, but not the risk/reward ratio at entry (potential upside vs downside based on stop-loss and take-profit levels). Over time, a low average R/R ratio erodes profitability even with a high win rate.",
            "evidence":     "state.json buy_log entries have no target_price or stop_price fields.",
            "proposed_fix": "At BUY execution, calculate and store `stop_price = entry_price × (1 - STOP_LOSS_PCT)` and `target_price = entry_price × (1 + TAKE_PROFIT_PCT)` in buy_log. Report average R/R in the trade review.",
            "auto_implementable": False,
        })

        # ── 7. Email sending is synchronous — blocks pipeline ────────────────
        findings.append({
            "agent":        self.name,
            "category":     "IMPROVEMENT",
            "severity":     "LOW",
            "confidence":   62,
            "title":        "SMTP email sends are synchronous and block the pipeline",
            "description":  "Every email in the pipeline (daily report, CoS brief, IRM review, board minutes) blocks the main thread until SMTP completes. Gmail SMTP timeout is 30 seconds. If Gmail is slow or the network is flaky, the entire pipeline stalls at every email step.",
            "evidence":     "_send_raw() in email_sender.py uses a synchronous smtplib.SMTP context manager.",
            "proposed_fix": "Wrap email sends in a background thread (threading.Thread) or use asyncio. Store emails in a queue and flush at pipeline end. This would also allow retry on transient SMTP failures.",
            "auto_implementable": False,
        })

        return findings


# =============================================================================
# SENSITIVITY FILTER + ROUTING
# =============================================================================

def _classify_finding(finding: dict) -> str:
    """
    Returns 'board', 'backlog', 'sensitive', or 'think_tank'.
    - 'sensitive':   touches confidential evaluation chains → Chairman-only
    - 'think_tank':  VISION category → separate ThinkTank queue
    - 'board':       confidence >= threshold AND evidence is precise → board vote
    - 'backlog':     confidence too low or evidence too vague → Director backlog
    """
    text = " ".join([
        finding.get("title", ""),
        finding.get("description", ""),
        finding.get("evidence", ""),
        finding.get("proposed_fix", ""),
    ])
    if _is_sensitive(text):
        return "sensitive"
    if finding.get("category") == "VISION":
        return "board"   # board votes; also logged to cit_think_tank.json separately
    confidence = finding.get("confidence", 0)
    if confidence < BOARD_CONFIDENCE_THRESHOLD:
        return "backlog"
    # Gate 1: evidence must contain something verifiable.
    # QUICK_WIN and for_hr findings bypass this — they're self-evident or contextual.
    evidence = finding.get("evidence", "")
    if (finding.get("category") not in ("QUICK_WIN",)
            and not finding.get("for_hr")
            and not _PRECISE_EVIDENCE_RE.search(evidence)):
        return "backlog"
    # Specificity check on proposed_fix: if vague, the idea still reaches the board
    # but is flagged "needs_clarification" so the board can vote NEEDS_INFO rather
    # than the idea being silently buried. A good idea with a vague fix is better
    # than a lost idea — the board and Director can refine it.
    proposed_fix = finding.get("proposed_fix", "")
    if (finding.get("category") not in ("QUICK_WIN", "VISION")
            and not finding.get("for_hr")
            and not _fix_is_specific(proposed_fix)):
        finding["needs_clarification"] = True
        finding["clarification_note"] = (
            "proposed_fix is too vague to implement directly. "
            "Board should vote NEEDS_INFO and request: specific file, function name, "
            "or pseudocode showing the exact change needed."
        )
    return "board"


# =============================================================================
# AGENT 5 — THINK TANK
# =============================================================================

class CITThinkTank:
    """
    The firm's internal think tank. Proposes ideas that the other four CIT
    agents would never generate — structural overhauls, strategy pivots,
    unconventional signal sources, and first-principles challenges to how
    the firm operates. This agent is deliberately contrarian.

    It does NOT audit existing code or flag bugs. Its mandate is: assume
    everything works correctly, then ask whether we're doing the right thing.

    Findings go to a separate data/cit_think_tank.json queue — they do not
    compete with the board's normal bug-fix and improvement workload. The
    Chairman reviews them on a separate cadence.

    All proposals are category "VISION" and require unanimous board vote.
    Confidence represents how well-evidenced the idea is, NOT whether it
    should definitely be done. The bar for a VISION proposal is: Is this
    worth the board's serious consideration?
    """

    name = "CITThinkTank"

    def analyze(self) -> list[dict]:
        findings = []

        state       = _load_json_safe(DATA_DIR / "state.json")
        canary      = _load_json_safe(DATA_DIR / "canary_state.json")
        risk_rpt    = _load_json_safe(DATA_DIR / "risk_manager_report.json")
        sector_asgn = _load_json_safe(DATA_DIR / "sector_assignments.json")
        registry    = _load_json_safe(DATA_DIR / "agent_registry.json")
        research    = _load_json_safe(DATA_DIR / "research_team_report.json")
        buy_log     = state.get("buy_log", [])
        sell_log    = state.get("sell_log", [])
        paper_day   = state.get("paper_day", 0)

        cro_level   = (risk_rpt.get("risk_assessment") or {}).get("risk_level", "UNKNOWN")
        canary_lvl  = canary.get("last_level", "GREEN")
        alerts_today = canary.get("alerts_today", [])
        n_agents    = len(registry.get("agents", []))

        # ── 1. Regime-adaptive strategy switching ─────────────────────────────
        # The firm runs a single momentum strategy regardless of market regime.
        # In CRITICAL/HIGH risk periods (which we're in now), momentum names
        # get hit hardest. A regime-aware switcher would rotate to defensive
        # factors (low-beta, high-dividend, quality) automatically.
        orange_days = sum(1 for a in canary.get("alerts_today", []) if a.get("level") in ("ORANGE","RED"))
        if cro_level in ("CRITICAL", "HIGH") or orange_days >= 2:
            findings.append({
                "agent":       self.name,
                "category":    "VISION",
                "severity":    "HIGH",
                "confidence":  82,
                "title":       "Add a regime-adaptive strategy layer: momentum → defensive factor rotation",
                "description": (
                    f"The firm runs a pure momentum strategy in all market conditions. "
                    f"CRO is currently {cro_level} and the canary fired ORANGE {orange_days}× today. "
                    f"In CRITICAL/HIGH regimes, momentum names are systematically the hardest hit — "
                    f"we're fighting the market instead of adapting to it. "
                    f"A regime layer would detect the risk environment from canary + CRO signals "
                    f"and auto-rotate the research mandate: in GREEN regimes, pursue momentum; "
                    f"in ORANGE/CRITICAL, mandate defensive picks (low-beta, dividend yield, "
                    f"quality factor) and reduce position sizing. This doesn't require a new "
                    f"strategy — it's a wrapper around the existing one."
                ),
                "evidence": (
                    f"CRO level: {cro_level} | Canary alerts today: {len(alerts_today)} | "
                    f"Current portfolio down from start; all buys blocked for extended period."
                ),
                "proposed_fix": (
                    "Add `regime` field to canary_state.json (BULL/NEUTRAL/DEFENSIVE/CRISIS). "
                    "R&D leader reads regime at assignment time and shifts sector scoring weights: "
                    "in DEFENSIVE mode, boost low-volatility and dividend sectors; "
                    "in CRISIS, assign only capital-preservation mandates. "
                    "Manager Agent adjusts MAX_POSITION_SIZE_PCT down 30% in DEFENSIVE mode."
                ),
                "auto_implementable": False,
            })

        # ── 2. Eliminate the R&D 3-day fixed cadence — move to event-driven ──
        # The R&D team runs every 3 days on a fixed clock. But markets don't move
        # on a 3-day clock. Major sector moves, earnings, and macro events happen
        # any time. We're often running stale research during the most important windows.
        last_research = state.get("last_research_run", "")
        if last_research:
            try:
                age_h = (datetime.now() - datetime.fromisoformat(last_research)).total_seconds() / 3600
                if age_h > 48:
                    findings.append({
                        "agent":       self.name,
                        "category":    "VISION",
                        "severity":    "MEDIUM",
                        "confidence":  78,
                        "title":       "Replace fixed R&D cadence with event-triggered research cycles",
                        "description": (
                            f"R&D last ran {age_h:.0f}h ago and fires on a fixed 3-day clock. "
                            f"Markets don't care about our schedule. High-conviction opportunities "
                            f"emerge after earnings beats, sector breakouts, and macro prints — "
                            f"not on a Tuesday-every-three-days basis. "
                            f"Proposal: keep the 3-day heartbeat, but add event triggers: "
                            f"any sector ETF moving >3% in a day forces an immediate targeted "
                            f"R&D cycle for that sector only. Canary YELLOW triggers a full cycle. "
                            f"This would have caught the DIS move that fired ORANGE today."
                        ),
                        "evidence": f"last_research_run: {last_research} ({age_h:.0f}h ago). Canary fired {len(alerts_today)} alert(s) today without fresh research.",
                        "proposed_fix": (
                            "In canary_agent.py: when a sector ETF moves >3% intraday, write "
                            "`data/rd_trigger.json` with the sector name. "
                            "In main.py Step 3c: if rd_trigger.json exists and is <4h old, "
                            "run rd_team_leader.py --sector <name> regardless of 3-day cadence. "
                            "Delete the trigger file after the cycle completes."
                        ),
                        "auto_implementable": False,
                    })
            except Exception:
                pass

        # ── 3. Conviction-weighted position sizing (replace flat % model) ─────
        # Every approved pick gets the same position size regardless of how
        # strong the thesis is. A 5-star pick and a 3-star pick get identical
        # allocations. This flattens alpha.
        if buy_log:
            findings.append({
                "agent":       self.name,
                "category":    "VISION",
                "severity":    "MEDIUM",
                "confidence":  75,
                "title":       "Replace flat position sizing with conviction-weighted Kelly-derived sizing",
                "description": (
                    f"All approved picks receive the same capital allocation regardless of "
                    f"conviction score (3★ vs 5★ get identical sizing). "
                    f"This treats a high-conviction thesis identically to a marginal one, "
                    f"compressing returns. A Kelly-fraction approach — sizing proportional "
                    f"to edge estimate derived from conviction score, historical approval rate "
                    f"per agent, and sector momentum score — would concentrate capital in "
                    f"the firm's best ideas and reduce it in weaker ones. "
                    f"Even a half-Kelly implementation would improve capital efficiency."
                ),
                "evidence": f"buy_log has {len(buy_log)} entries; all use flat MAX_POSITION_SIZE_PCT with no conviction modifier.",
                "proposed_fix": (
                    "In manager_agent.py: at BUY execution, compute "
                    "`size_multiplier = 0.5 + (conviction - 3) * 0.25` for conviction 3–5. "
                    "Multiply MAX_POSITION_SIZE_PCT by this factor (floor 0.5×, ceiling 1.5×). "
                    "Log `conviction_size_multiplier` in buy_log for post-trade attribution."
                ),
                "auto_implementable": False,
            })

        # ── 4. Add a dedicated short book for CRITICAL regimes ────────────────
        # The firm's only short instrument is SH (inverse ETF), which is blunt.
        # In CRITICAL regimes, individual names often fall 20-40% while the broad
        # index falls 5-10%. Targeted shorts on the weakest names in assigned
        # sectors would generate alpha the SH hedge can't.
        if cro_level in ("CRITICAL", "HIGH"):
            findings.append({
                "agent":       self.name,
                "category":    "VISION",
                "severity":    "MEDIUM",
                "confidence":  70,
                "title":       "Build a targeted short book for CRITICAL/HIGH risk regimes",
                "description": (
                    f"In CRITICAL risk regimes (current: {cro_level}), the firm deploys SH "
                    f"as a broad inverse hedge. SH tracks the S&P 500 inverse — it captures "
                    f"index-level moves but misses the outsized drawdowns in weakest names. "
                    f"Proposal: when CRO declares CRITICAL, the R&D leader runs a 'short scan' "
                    f"pass alongside the normal long scan, looking for names with: "
                    f"deteriorating fundamentals, failed support levels, high short interest already, "
                    f"and negative sector momentum. These enter a separate 'short book' with "
                    f"its own position limits and stop-loss rules (inverted: stop-gain not stop-loss)."
                ),
                "evidence": f"CRO level: {cro_level}. SH is current only short vehicle. Individual name shorts in CRITICAL regime could 3-5× the hedge alpha vs. index-level SH.",
                "proposed_fix": (
                    "Add `short_book_enabled` flag to config.py, gated on CRO level. "
                    "R&D leader gets a `--scan-shorts` mode that inverts scoring criteria. "
                    "Manager Agent executes short sells via Alpaca (supported in paper mode). "
                    "Separate MAX_SHORT_POSITION_PCT (suggest 3%) and SHORT_STOP_GAIN_PCT (15%)."
                ),
                "auto_implementable": False,
            })

        # ── 5. Multi-agent debate before high-conviction buys ─────────────────
        # Currently, a BUY recommendation flows from Review Agent → Manager Agent
        # with a single path. There's no adversarial challenge. The IRM reviews
        # after the fact. Proposal: for any BUY above 4★ conviction, spawn a
        # 'devil's advocate' agent that argues against the trade before execution.
        findings.append({
            "agent":       self.name,
            "category":    "VISION",
            "severity":    "LOW",
            "confidence":  72,
            "title":       "Pre-trade devil's advocate: adversarial challenge before high-conviction buys",
            "description": (
                f"The BUY pipeline has no adversarial check. Review Agent recommends → "
                f"Manager Agent approves → trade executes. The IRM reviews decisions post-hoc "
                f"but can't stop a trade in-flight. "
                f"Proposal: for any BUY with conviction ≥4★, spawn a 'BearCaseAgent' that "
                f"reads the same thesis and produces a structured counter-argument: "
                f"what would have to be true for this trade to fail? What's the base rate "
                f"of success for this sector/pattern? Does the canary context invalidate the thesis? "
                f"Manager Agent sees both the bull and bear case before deciding. "
                f"This won't block good trades — it will surface hidden risks on marginal ones."
            ),
            "evidence": f"Current pipeline: Review → Manager (single path). {len(buy_log)} historical buys with no pre-trade adversarial check in pipeline.",
            "proposed_fix": (
                "Add BearCaseAgent class to manager_agent.py. "
                "Instantiate it inline when conviction >= 4 before placing the order. "
                "It reads the pick's sector, RSI, recent news context (from canary/geopolitical data), "
                "and writes a structured counter-case to `data/bear_cases.json`. "
                "Manager Agent's final decision logs both cases for IRM post-trade review."
            ),
            "auto_implementable": False,
        })

        # ── 6. Replace the 30-day paper trading horizon with rolling evaluation─
        # The firm runs a 30-day paper trading window (day {paper_day}/30).
        # After day 30, the evaluation ends. This creates two problems:
        # 1. Decisions in the final week are distorted by the approaching deadline.
        # 2. There's no continuity — insights from the 30-day run don't feed forward.
        # Proposal: replace with a rolling window — always evaluating the last 30 days,
        # never "ending", with quarterly benchmarking reports instead.
        if paper_day >= 20:
            findings.append({
                "agent":       self.name,
                "category":    "VISION",
                "severity":    "MEDIUM",
                "confidence":  80,
                "title":       f"Replace 30-day fixed evaluation window with a rolling perpetual model (currently day {paper_day}/30)",
                "description": (
                    f"The firm is on day {paper_day}/30 of a fixed trading window. "
                    f"Fixed-window evaluation creates end-of-horizon distortions: agents "
                    f"and managers behave differently when the 'clock is running out'. "
                    f"More importantly, when day 30 ends, institutional memory resets. "
                    f"Proposal: remove the 30-day limit. Run perpetually. "
                    f"Generate a quarterly benchmark report (vs. SPY, QQQ, sector ETFs) "
                    f"every 63 trading days. Evaluate the firm on rolling 30/63/252-day "
                    f"Sharpe ratio, not a single fixed window. "
                    f"This is how real hedge funds measure themselves."
                ),
                "evidence": f"paper_day={paper_day}/30 in state.json. Fixed-window evaluation ends in {30 - paper_day} day(s).",
                "proposed_fix": (
                    "In state.json, replace `paper_day` counter with `trading_day_total` (never resets). "
                    "Add `quarterly_benchmark_day` that triggers a benchmark report every 63 days. "
                    "In main.py, remove the day-30 termination condition. "
                    "Report generator shows trailing 30/63-day Sharpe vs. benchmark instead of 'Day X/30'."
                ),
                "auto_implementable": False,
            })

        return findings


# =============================================================================
# AGENT 6 — DATA QUALITY ANALYST
# =============================================================================

class CITDataQualityAnalyst:
    """
    Monitors the quality of all incoming market data.
    yfinance silently returns None, stale prices, or bad data — this agent
    catches it before it corrupts downstream analysis.
    """

    name = "CITDataQualityAnalyst"

    def analyze(self) -> list[dict]:
        findings = []

        # ── 1. international_report.json — None price/pct from yfinance ─────
        # Only applies to international_report (index/ETF data with pct and price fields).
        # earnings_predictions uses a different schema (prediction/confidence/beat_rate).
        for fname in ("international_report.json",):
            path = DATA_DIR / fname
            if not path.exists():
                continue
            data = _load_json_safe(path)
            items = data if isinstance(data, list) else data.get("results", data.get("indices", []))
            if isinstance(items, dict):
                items = list(items.values())
            null_entries = []
            for item in items:
                if isinstance(item, dict):
                    if item.get("pct") is None or item.get("price") is None:
                        null_entries.append(item.get("ticker") or item.get("index") or str(item)[:40])
            if null_entries:
                findings.append({
                    "agent":        self.name,
                    "category":     "BUG_FIX",
                    "severity":     "HIGH",
                    "confidence":   90,
                    "title":        f"None price/pct values in {fname} ({len(null_entries)} ticker(s))",
                    "description":  (
                        f"yfinance silently returned None for price or pct fields in {fname}. "
                        f"Downstream analysis reading these values will compute wrong signals. "
                        f"Affected: {null_entries[:5]}"
                    ),
                    "evidence":     f"data/{fname}: {len(null_entries)} entries with null pct or price",
                    "proposed_fix": "Add null-guard in the data fetch layer: skip or flag tickers that return None from yfinance rather than storing None.",
                    "auto_implementable": False,
                })

        # ── 1b. earnings_predictions.json — beat_rate None for all tickers ───
        ep_path = DATA_DIR / "earnings_predictions.json"
        if ep_path.exists():
            ep_data = _load_json_safe(ep_path)
            ep_items = ep_data if isinstance(ep_data, list) else ep_data.get("predictions", [])
            ep_no_beat_rate = [p.get("ticker","?") for p in ep_items
                               if isinstance(p, dict) and p.get("beat_rate") is None]
            if len(ep_no_beat_rate) == len(ep_items) and ep_items:
                findings.append({
                    "agent":       self.name,
                    "category":    "BUG_FIX",
                    "severity":    "MEDIUM",
                    "confidence":  75,
                    "title":       f"earnings_predictions: beat_rate None for all {len(ep_no_beat_rate)} tickers",
                    "description": "yfinance is not returning historical EPS beat rate data. beat_rate is used to adjust BEAT/MISS conviction — None means it falls back to base reasoning only.",
                    "evidence":    f"data/earnings_predictions.json: all {len(ep_no_beat_rate)} entries have beat_rate=null",
                    "proposed_fix": "Check yfinance earningsHistory availability; fall back to SEC EDGAR or a fixed historical beat-rate average per sector.",
                    "auto_implementable": False,
                })

        # ── 2. research_team_report.json — sector ETF price history < 30 days ─
        research = _load_json_safe(DATA_DIR / "research_team_report.json")
        sector_results = research.get("sector_results", research.get("sectors", {}))
        if isinstance(sector_results, dict):
            sector_results = list(sector_results.values())
        short_history = []
        for sector in sector_results:
            if isinstance(sector, dict):
                hist_len = sector.get("price_history_days") or sector.get("history_length") or 0
                if 0 < hist_len < 30:
                    short_history.append(f"{sector.get('sector', sector.get('name', '?'))} ({hist_len}d)")
        if short_history:
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "MEDIUM",
                "confidence":   80,
                "title":        f"Sector ETFs with <30 days price history in research_team_report.json ({len(short_history)} sector(s))",
                "description":  (
                    f"The following sectors returned less than 30 days of price history — "
                    f"likely stale or missing yfinance data: {short_history[:5]}. "
                    f"Momentum scores computed on <30 days of data are unreliable."
                ),
                "evidence":     f"data/research_team_report.json: short-history sectors: {short_history[:3]}",
                "proposed_fix": "Add a minimum history check in research_team.py: skip sector ETF if fewer than 30 trading days returned. Log a warning and use cached data if available.",
                "auto_implementable": False,
            })

        # ── 3. sector_assignments.json — verify scores are not None ───────────
        sector_asgn = _load_json_safe(DATA_DIR / "sector_assignments.json")
        bad_sectors = []
        for assignment in sector_asgn.get("assignments", []):
            if isinstance(assignment, dict) and assignment.get("score") is None:
                bad_sectors.append(assignment.get("sector_name", "?"))
        if bad_sectors:
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "MEDIUM",
                "confidence":   85,
                "title":        f"None score in sector_assignments.json ({len(bad_sectors)} sector(s))",
                "description":  (
                    f"Sectors assigned with None scores: {bad_sectors}. "
                    f"Agent assignment decisions based on null scores may produce arbitrary sector priority ordering."
                ),
                "evidence":     f"data/sector_assignments.json: {len(bad_sectors)} assignments with score=None",
                "proposed_fix": "In rd_team_leader.py: filter out sectors with None scores before ranking. Treat None as score=0 or skip entirely.",
                "auto_implementable": False,
            })

        # ── 4. canary_state.json — None indicators in last_readings ──────────
        canary = _load_json_safe(DATA_DIR / "canary_state.json")
        last_readings = canary.get("last_readings", {})
        null_indicators = []
        for key in ("spy_pct", "vix_level"):
            if last_readings and last_readings.get(key) is None:
                null_indicators.append(key)
        # Only flag if last_readings exists but key indicators are None (real data gap)
        if null_indicators and last_readings:
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "HIGH",
                "confidence":   92,
                "title":        f"None canary readings: {null_indicators}",
                "description":  (
                    f"yfinance failed to return data for SPY/VIX indicators: {null_indicators}. "
                    f"The canary uses these to gate all trading — a None VIX or SPY means "
                    f"the canary may have defaulted to GREEN when the market was actually stressed."
                ),
                "evidence":     f"data/canary_state.json last_readings: {null_indicators} are None",
                "proposed_fix": "If SPY or VIX is None, default canary to ORANGE (fail-safe) and log the data gap rather than silently defaulting to GREEN.",
                "auto_implementable": False,
            })

        # ── 5. risk_manager_report.json — empty positions but positive value ──
        risk_rpt = _load_json_safe(DATA_DIR / "risk_manager_report.json")
        positions_list = risk_rpt.get("positions", [])
        portfolio_val  = risk_rpt.get("portfolio_value", 0) or 0
        if isinstance(positions_list, list) and len(positions_list) == 0 and portfolio_val > 1000:
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "MEDIUM",
                "confidence":   78,
                "title":        f"risk_manager_report.json shows empty positions with portfolio_value=${portfolio_val:,.0f}",
                "description":  (
                    f"The risk report lists 0 positions but portfolio_value=${portfolio_val:,.0f}. "
                    f"This is a data gap: either Alpaca positions failed to load, or the report "
                    f"was generated before positions were refreshed. CVaR and concentration risk "
                    f"calculations based on an empty position list will be wrong."
                ),
                "evidence":     f"data/risk_manager_report.json: positions=[] but portfolio_value=${portfolio_val:,.0f}",
                "proposed_fix": "In risk_manager_agent.py: if positions list is empty but portfolio_value>0, abort and log ERROR rather than writing a misleading zero-position risk report.",
                "auto_implementable": False,
            })

        # ── 6. Files older than 26 hours that should have been updated ────────
        STALE_FILES = {
            "data/international_report.json": 26,
            "data/earnings_predictions.json": 26,
            "data/canary_state.json":         13,
            "data/risk_manager_report.json":  26,
            "data/sector_assignments.json":   72,
        }
        for rel_path, max_age in STALE_FILES.items():
            full_path = BASE_DIR / rel_path
            if not full_path.exists():
                continue
            age = _file_age_hours(full_path)
            if age is not None and age > max_age:
                findings.append({
                    "agent":        self.name,
                    "category":     "BUG_FIX",
                    "severity":     "MEDIUM" if age < max_age * 2 else "HIGH",
                    "confidence":   88,
                    "title":        f"Stale data file: {rel_path} ({age:.0f}h old, threshold {max_age}h)",
                    "description":  (
                        f"{rel_path} has not been updated in {age:.0f}h. "
                        f"Expected refresh interval: {max_age}h. "
                        f"Data from this file may be leading to decisions based on stale market state."
                    ),
                    "evidence":     f"{rel_path}: file age {age:.0f}h. Max acceptable: {max_age}h.",
                    "proposed_fix": f"Check that the agent producing {rel_path} ran successfully in recent logs. Verify its cron schedule is active.",
                    "auto_implementable": False,
                })

        # ── Data source gap tracker ───────────────────────────────────────────
        # Monitors the known yfinance beat_rate coverage gap.
        # Samples 5 tickers each session, logs coverage over time.
        # Escalates if gap exceeds 30 days or celebrates if coverage returns.
        try:
            import yfinance as _yf
            _sample_tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
            _beat_rate_available = 0
            for _tk in _sample_tickers:
                try:
                    _info = _yf.Ticker(_tk).info
                    # yfinance earningsHistory or similar field that implies beat rate
                    _hist = _yf.Ticker(_tk).earnings_history
                    if _hist is not None and not _hist.empty and len(_hist) >= 2:
                        _beat_rate_available += 1
                except Exception:
                    pass
            _coverage_pct = round(_beat_rate_available / len(_sample_tickers) * 100)

            _gap_log_path = DATA_DIR / "cit_data_source_gaps.json"
            try:
                with open(_gap_log_path) as _f:
                    _gap_log = json.load(_f)
            except Exception:
                _gap_log = {"first_detected": datetime.now().isoformat(), "history": []}

            _gap_log["history"].append({
                "ts": datetime.now().isoformat(),
                "beat_rate_coverage_pct": _coverage_pct,
                "sample_size": len(_sample_tickers),
            })
            _gap_log["history"] = _gap_log["history"][-60:]  # keep last 60 readings
            _gap_log["latest_coverage_pct"] = _coverage_pct

            with open(_gap_log_path, "w") as _f:
                json.dump(_gap_log, _f, indent=2, default=str)

            _days_since_first = (
                datetime.now() - datetime.fromisoformat(_gap_log["first_detected"])
            ).days

            if _coverage_pct >= 60:
                # Coverage improved — alert board
                findings.append({
                    "agent":       self.name,
                    "category":    "NEW_FEATURE",
                    "severity":    "HIGH",
                    "confidence":  90,
                    "title":       f"yfinance beat_rate coverage restored: {_coverage_pct}% of sample tickers",
                    "description": (
                        f"yfinance earnings history now available for {_beat_rate_available}/{len(_sample_tickers)} "
                        f"sample tickers. Earnings predictor beat_rate can now be re-enabled. "
                        f"Gap lasted {_days_since_first} day(s)."
                    ),
                    "proposed_fix": "Re-enable beat_rate in earnings_predictor.py and remove the MEDIUM known-gap finding.",
                    "auto_implementable": False,
                })
            elif _days_since_first >= 30:
                # Gap has persisted 30+ days — recommend paid data source
                findings.append({
                    "agent":       self.name,
                    "category":    "BUG_FIX",
                    "severity":    "MEDIUM",
                    "confidence":  85,
                    "title":       f"beat_rate data gap: {_days_since_first} days at {_coverage_pct}% coverage — consider paid feed",
                    "description": (
                        f"yfinance has not reliably provided EPS beat rate history for {_days_since_first} days. "
                        f"Coverage: {_coverage_pct}% of sample. "
                        f"Recommend evaluating a paid data feed (e.g. Financial Modeling Prep, Polygon.io) "
                        f"for earnings history before live trading launch."
                    ),
                    "proposed_fix": "Evaluate Financial Modeling Prep /v3/earning_surprises endpoint or Polygon.io earnings data.",
                    "auto_implementable": False,
                })
        except Exception as _gap_e:
            logger.debug(f"  [CITDataQualityAnalyst] Gap tracker error (non-fatal): {_gap_e}")

        # ── Save findings ─────────────────────────────────────────────────────
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(DATA_DIR / "cit_data_quality.json", "w") as f:
                json.dump({
                    "generated_at":   datetime.now().isoformat(),
                    "total_findings": len(findings),
                    "findings":       findings,
                }, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"  [CITDataQualityAnalyst] Could not save report: {e}")

        return findings


# =============================================================================
# AGENT 7 — PROPOSAL VALIDATOR
# =============================================================================

class CITProposalValidator:
    """
    Before CIT proposals reach the board, validate them against historical
    evidence. The board should vote on evidence, not theory.
    """

    name = "CITProposalValidator"

    # Keywords that indicate a trading-strategy proposal (needs backtest evidence)
    _STRATEGY_KEYWORDS = [
        "short book", "kelly sizing", "kelly-derived", "regime rotation",
        "position sizing", "conviction-weighted", "momentum", "backtest",
        "factor rotation", "stop-loss", "take-profit", "regime-adaptive",
    ]

    def analyze(self) -> list[dict]:
        findings = []

        pending = _load_json_list_safe(CIT_PENDING)
        validation_log = []
        now = datetime.now()

        backtester_available = (BASE_DIR / "backtester.py").exists()

        for proposal in pending:
            pid    = proposal.get("id", "?")
            title  = proposal.get("title", "")
            desc   = proposal.get("description", "")
            cat    = proposal.get("category", "")
            text   = (title + " " + desc).lower()

            entry = {
                "id":    pid,
                "title": title,
                "category": cat,
            }

            # Check for staleness (proposals queued for 3+ cycles without action)
            queued_at_str = proposal.get("queued_at") or proposal.get("generated_at")
            if queued_at_str:
                try:
                    queued_at = datetime.fromisoformat(str(queued_at_str)[:19])
                    age_cycles = int((now - queued_at).total_seconds() / (12 * 3600))
                    if age_cycles >= 3:
                        entry["stale"]       = True
                        entry["age_cycles"]  = age_cycles
                        entry["recommendation"] = "Stale proposal — recommend implementing or formally closing"
                        findings.append({
                            "agent":        self.name,
                            "category":     "BUG_FIX",
                            "severity":     "LOW",
                            "confidence":   75,
                            "title":        f"Stale CIT proposal: {title[:60]} ({age_cycles} cycles unactioned)",
                            "description":  (
                                f"Proposal {pid} has been in the queue for {age_cycles} CIT cycles "
                                f"without a board vote or implementation. Stale proposals clog the "
                                f"queue and reduce signal quality."
                            ),
                            "evidence":     f"data/cit_pending_proposals.json: proposal {pid} queued {age_cycles} cycles ago",
                            "proposed_fix": "Board should either vote on this proposal or formally close it as 'not actioning'.",
                            "auto_implementable": False,
                        })
                except Exception:
                    pass

            # Classify and validate
            is_strategy = any(kw in text for kw in self._STRATEGY_KEYWORDS)
            is_technical = cat in ("BUG_FIX", "QUICK_WIN") and not is_strategy

            if is_technical:
                entry["pre_validated"] = True
                entry["validation_note"] = "Technical/code fix — no backtest required"
            elif is_strategy:
                if backtester_available:
                    entry["backtest_evidence"] = (
                        "backtester.py is available. A 1-year backtest of this strategy "
                        "change should be run before board vote. See backtester.py for usage."
                    )
                    entry["pre_validated"] = False
                else:
                    entry["backtest_evidence"] = (
                        "No historical validation available — recommend backtesting before implementation. "
                        "backtester.py not found in project root."
                    )
                    entry["pre_validated"] = False
            else:
                entry["pre_validated"] = False
                entry["validation_note"] = "Improvement/architectural proposal — review manually before board vote"

            validation_log.append(entry)

        # Save annotated proposals and log
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(DATA_DIR / "cit_proposal_validation.json", "w") as f:
                json.dump({
                    "generated_at":          datetime.now().isoformat(),
                    "proposals_reviewed":    len(pending),
                    "backtester_available":  backtester_available,
                    "validation_log":        validation_log,
                }, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"  [CITProposalValidator] Could not save validation log: {e}")

        return findings


# =============================================================================
# AGENT 8 — SELF IMPROVEMENT (META-AGENT)
# =============================================================================

class CITSelfImprovement:
    """
    The ONLY job of this agent is to think about how to improve the CIT team
    itself. Nothing else. It is self-referential by design.
    This agent should be candid and self-critical — it exists to prevent the
    CIT from becoming a self-congratulatory bureaucracy.
    """

    name = "CITSelfImprovement"

    def analyze(self) -> list[dict]:
        findings = []

        history = _load_json_safe(CIT_HISTORY)
        pending = _load_json_list_safe(CIT_PENDING)
        backlog = _load_json_list_safe(CIT_BACKLOG)
        approved = _load_json_list_safe(CIT_APPROVED)

        agent_stats = history.get("agents", {})

        # ── 1. Acceptance rate analysis ───────────────────────────────────────
        for agent_name, stats in agent_stats.items():
            total   = stats.get("total_findings", 0)
            board   = stats.get("board_proposals", 0)
            if total > 10 and board > 0:
                # Estimate how many board proposals were actually approved
                # (approximate from approved list)
                agent_approved = sum(
                    1 for p in approved if p.get("agent") == agent_name
                )
                approval_rate = agent_approved / board * 100 if board > 0 else 0
                if approval_rate < 20 and board >= 5:
                    findings.append({
                        "agent":        self.name,
                        "category":     "IMPROVEMENT",
                        "severity":     "MEDIUM",
                        "confidence":   72,
                        "title":        f"Low board approval rate for {agent_name}: {approval_rate:.0f}% ({agent_approved}/{board} proposals acted on)",
                        "description":  (
                            f"{agent_name} has submitted {board} board proposals but only {agent_approved} "
                            f"have been approved and implemented ({approval_rate:.0f}% rate). "
                            f"Low approval rates may indicate proposals are too vague, too ambitious, "
                            f"or misaligned with the firm's current priorities. "
                            f"This agent should recalibrate its proposal quality standards."
                        ),
                        "evidence":     f"data/cit_history.json: {agent_name} board_proposals={board}, approved≈{agent_approved}",
                        "proposed_fix": (
                            f"Review the last 5 unimplemented {agent_name} proposals. "
                            f"Are they too abstract? Too large in scope? Not evidence-backed enough? "
                            f"Retask the agent to produce smaller, more actionable proposals."
                        ),
                        "auto_implementable": False,
                    })

        # ── 2. Coverage gap analysis ──────────────────────────────────────────
        agent_names = set(agent_stats.keys())
        # Check for gaps in coverage
        coverage_gaps = []
        if not any("Security" in n or "security" in n for n in agent_names):
            coverage_gaps.append("Security monitoring (file permissions, credential scanning)")
        if not any("Data" in n or "Quality" in n for n in agent_names):
            coverage_gaps.append("Data quality monitoring (yfinance None returns, stale data)")
        if not any("Proposal" in n or "Validator" in n for n in agent_names):
            coverage_gaps.append("Proposal validation (pre-backtest before board votes)")

        if coverage_gaps:
            findings.append({
                "agent":        self.name,
                "category":     "NEW_FEATURE",
                "severity":     "MEDIUM",
                "confidence":   70,
                "title":        f"CIT coverage gaps identified: {len(coverage_gaps)} area(s) unmonitored",
                "description":  (
                    f"The following operational areas have no dedicated CIT monitoring: "
                    f"{coverage_gaps}. These gaps mean real risks could go undetected until "
                    f"they cause a failure."
                ),
                "evidence":     f"data/cit_history.json: agent roster = {sorted(agent_names)}",
                "proposed_fix": "Add dedicated CIT agents for each uncovered area, or expand existing agent mandates to include these checks.",
                "auto_implementable": False,
            })

        # ── 3. Queue clog check ───────────────────────────────────────────────
        if len(pending) > 30:
            findings.append({
                "agent":        self.name,
                "category":     "IMPROVEMENT",
                "severity":     "MEDIUM",
                "confidence":   85,
                "title":        f"CIT proposal queue clogged: {len(pending)} pending proposals",
                "description":  (
                    f"The board proposal queue has {len(pending)} unvoted items. "
                    f"A bloated queue means the board cannot focus on the highest-priority items "
                    f"and good proposals get buried. The CIT team may be generating too many "
                    f"low-value proposals."
                ),
                "evidence":     f"data/cit_pending_proposals.json: {len(pending)} items",
                "proposed_fix": (
                    "Raise the board confidence threshold from 70% to 80% temporarily. "
                    "Run a queue triage: close all proposals older than 60 days without a board vote. "
                    "Consider merging related proposals into single action items."
                ),
                "auto_implementable": False,
            })

        # ── 4. Vague proposal rate ────────────────────────────────────────────
        # Only BUG_FIX / CODE_BUG / DATA_QUALITY / PERFORMANCE proposals are
        # required to carry precise evidence (file:line, metric, data path).
        # VISION / IMPROVEMENT / STRATEGY / NEW_FEATURE proposals are
        # architectural in nature and should not count against evidence quality.
        _EVIDENCE_REQUIRED_CATS = {"BUG_FIX", "CODE_BUG", "DATA_QUALITY", "PERFORMANCE"}
        all_proposals = pending + backlog
        evidence_required = [
            p for p in all_proposals
            if p.get("category", "BUG_FIX").upper() in _EVIDENCE_REQUIRED_CATS
        ]
        vague = [
            p for p in evidence_required
            if not _PRECISE_EVIDENCE_RE.search(p.get("evidence", ""))
        ]
        if evidence_required and len(vague) / len(evidence_required) > 0.4:
            vague_pct = len(vague) / len(evidence_required) * 100
            findings.append({
                "agent":        self.name,
                "category":     "IMPROVEMENT",
                "severity":     "MEDIUM",
                "confidence":   80,
                "title":        f"{vague_pct:.0f}% of CIT bug proposals lack precise evidence — quality degradation",
                "description":  (
                    f"{len(vague)} of {len(evidence_required)} BUG_FIX/DATA_QUALITY proposals ({vague_pct:.0f}%) "
                    f"have vague evidence strings (no file:line, metric, or data path). "
                    f"VISION/IMPROVEMENT proposals are excluded from this check — they are "
                    f"architectural and don't require code-level evidence."
                ),
                "evidence":     f"data/cit_pending_proposals.json + cit_backlog.json: {len(vague)}/{len(evidence_required)} bug proposals vague",
                "proposed_fix": (
                    "Each CIT agent's analyze() method should be reviewed. "
                    "BUG_FIX findings must include a specific file:line, metric, or data path. "
                    "VISION/IMPROVEMENT proposals may be qualitative."
                ),
                "auto_implementable": False,
            })

        # ── 5. Self-referential check: were prior self-improvement proposals implemented? ─
        prior_self = [p for p in approved if p.get("agent") == self.name]
        if prior_self:
            findings.append({
                "agent":        self.name,
                "category":     "IMPROVEMENT",
                "severity":     "LOW",
                "confidence":   70,
                "title":        f"CITSelfImprovement has {len(prior_self)} approved proposal(s) — verify implementation",
                "description":  (
                    f"Previous self-improvement proposals were board-approved but may not have "
                    f"been implemented: {[p.get('title','?')[:50] for p in prior_self[:3]]}. "
                    f"An approved-but-unimplemented proposal is wasted board attention."
                ),
                "evidence":     f"data/cit_approved_proposals.json: {len(prior_self)} CITSelfImprovement entries",
                "proposed_fix": "Director should verify each approved self-improvement proposal has a corresponding code change or process update. Close or reopen as needed.",
                "auto_implementable": False,
            })

        # ── Save findings ─────────────────────────────────────────────────────
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(DATA_DIR / "cit_self_improvement.json", "w") as f:
                json.dump({
                    "generated_at":      datetime.now().isoformat(),
                    "total_findings":    len(findings),
                    "queue_depth":       len(pending),
                    "backlog_depth":     len(backlog),
                    "approved_total":    len(approved),
                    "agent_stats":       agent_stats,
                    "findings":          findings,
                }, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"  [CITSelfImprovement] Could not save report: {e}")

        return findings


# =============================================================================
# AGENT 9 — SECURITY MONITOR
# =============================================================================

class CITSecurityMonitor:
    """
    Monitors the firm for security risks. The firm will be trading real money
    soon — security posture must be production-grade now.
    """

    name = "CITSecurityMonitor"

    # Regex patterns that suggest an accidentally logged credential
    _CRED_PATTERNS = [
        re.compile(r'ghp_[A-Za-z0-9]{36}'),                       # GitHub PAT
        re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]{20,}'),          # Bearer token
        re.compile(r'(?:api[_-]?key|secret|password)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}', re.IGNORECASE),
        re.compile(r'(?:APCA-API-KEY-ID|APCA-API-SECRET-KEY)\s*:\s*[A-Za-z0-9]{16,}', re.IGNORECASE),
        re.compile(r'sk-[A-Za-z0-9]{32,}'),                        # OpenAI-style key
        re.compile(r'[A-Z0-9]{20}[A-Z0-9/+]{40}'),                 # AWS-style key
    ]

    def analyze(self) -> list[dict]:
        findings = []

        # ── 1. Scan log files for accidentally logged credentials ─────────────
        if LOG_DIR.exists():
            for log_file in sorted(LOG_DIR.glob("*.log"))[-7:]:   # last 7 log files
                try:
                    text = log_file.read_text(errors="replace")
                    for pat in self._CRED_PATTERNS:
                        m = pat.search(text)
                        if m:
                            snippet = text[max(0, m.start()-20):m.end()+20].replace("\n", " ")
                            findings.append({
                                "agent":        self.name,
                                "category":     "SECURITY",
                                "severity":     "CRITICAL",
                                "confidence":   88,
                                "title":        f"Possible credential leaked in log: {log_file.name}",
                                "description":  (
                                    f"A pattern matching a credential or API key was found in {log_file.name}. "
                                    f"Log files are often world-readable and may be synced to remote servers. "
                                    f"Snippet (redacted): ...{snippet[:80]}..."
                                ),
                                "evidence":     f"logs/{log_file.name}: credential pattern matched",
                                "proposed_fix": "Rotate the credential immediately. Add redaction to the logging layer for API keys and Bearer tokens.",
                                "auto_implementable": False,
                            })
                            break   # one finding per file
                except Exception:
                    pass

        # ── 2. .env file permissions ──────────────────────────────────────────
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            mode = oct(env_path.stat().st_mode)[-3:]
            if mode not in ("600", "400"):
                findings.append({
                    "agent":        self.name,
                    "category":     "SECURITY",
                    "severity":     "HIGH",
                    "confidence":   95,
                    "title":        f".env file permissions are {mode} — should be 600",
                    "description":  (
                        f"The .env file (containing API keys and secrets) has permissions {mode}. "
                        f"It should be 600 (owner read/write only). World-readable .env files "
                        f"expose credentials to other users on the same system."
                    ),
                    "evidence":     f".env file mode: {mode}. Required: 600.",
                    "proposed_fix": "Run: chmod 600 .env",
                    "auto_implementable": True,
                })

        # ── 3. Scan Python source files for hardcoded credentials ─────────────
        _HARDCODED_RE = re.compile(
            r'(?:api_key|secret_key|password|token)\s*=\s*["\'][A-Za-z0-9_\-]{16,}["\']',
            re.IGNORECASE
        )
        for src_path in SRC_FILES:
            if src_path.name in ("config.py",):
                continue   # config.py reads from env — false positives expected
            try:
                text = src_path.read_text()
                m = _HARDCODED_RE.search(text)
                if m:
                    lineno = text[:m.start()].count("\n") + 1
                    findings.append({
                        "agent":        self.name,
                        "category":     "SECURITY",
                        "severity":     "CRITICAL",
                        "confidence":   80,
                        "title":        f"Possible hardcoded credential in {src_path.name}:{lineno}",
                        "description":  (
                            f"A pattern matching a hardcoded API key or password was found in "
                            f"{src_path.name} at line {lineno}. Hardcoded credentials get committed "
                            f"to version control and are extremely difficult to fully purge."
                        ),
                        "evidence":     f"{src_path.name} line {lineno}: matches credential pattern",
                        "proposed_fix": "Move the credential to .env and load via os.getenv(). Rotate the key immediately.",
                        "auto_implementable": False,
                    })
            except Exception:
                continue

        # ── 4. OCI key permissions ────────────────────────────────────────────
        oci_key = BASE_DIR / "oracle_setup" / "oci_key.pem"
        if oci_key.exists():
            mode = oct(oci_key.stat().st_mode)[-3:]
            if mode not in ("600", "400"):
                findings.append({
                    "agent":        self.name,
                    "category":     "SECURITY",
                    "severity":     "HIGH",
                    "confidence":   95,
                    "title":        f"oracle_setup/oci_key.pem permissions are {mode} — should be 600",
                    "description":  (
                        f"The OCI private key has permissions {mode}. "
                        f"Private keys must be 600 (owner-only) or they cannot be used by most "
                        f"SSH/TLS clients and are at risk of exposure."
                    ),
                    "evidence":     f"oracle_setup/oci_key.pem mode: {mode}",
                    "proposed_fix": "Run: chmod 600 oracle_setup/oci_key.pem",
                    "auto_implementable": True,
                })

        # ── 5. Data files that are world-readable ─────────────────────────────
        sensitive_data_files = [
            "state.json", "manager_decisions.json", "irm_daily_check.json",
            "cit_sensitive.json", "hr_irm_sealed.json",
        ]
        for fname in sensitive_data_files:
            fpath = DATA_DIR / fname
            if not fpath.exists():
                continue
            mode = oct(fpath.stat().st_mode)[-3:]
            if mode[-1] != "0":   # world has any permission
                findings.append({
                    "agent":        self.name,
                    "category":     "SECURITY",
                    "severity":     "MEDIUM",
                    "confidence":   80,
                    "title":        f"Sensitive data file world-readable: data/{fname} (mode {mode})",
                    "description":  (
                        f"data/{fname} has permissions {mode} — the world-readable bit is set. "
                        f"This file contains sensitive trading or governance data and should "
                        f"be readable only by the owner."
                    ),
                    "evidence":     f"data/{fname} mode: {mode}",
                    "proposed_fix": f"Run: chmod 640 data/{fname}  (or 600 for most sensitive files)",
                    "auto_implementable": True,
                })

        # ── 6. Alpaca API call volume anomaly check ────────────────────────────
        mgr_data = _load_json_safe(DATA_DIR / "manager_decisions.json")
        decisions = mgr_data.get("decisions", []) if isinstance(mgr_data, dict) else []
        buy_count = sum(1 for d in decisions if "BUY" in str(d.get("manager_action", "")))
        if buy_count > 20:
            findings.append({
                "agent":        self.name,
                "category":     "SECURITY",
                "severity":     "HIGH",
                "confidence":   75,
                "title":        f"Unusual buy order volume in manager_decisions.json: {buy_count} BUY orders this cycle",
                "description":  (
                    f"The most recent manager decisions show {buy_count} BUY actions in a single cycle. "
                    f"This is an unusually high order volume that could indicate account compromise, "
                    f"a runaway strategy loop, or a bug in the approval logic."
                ),
                "evidence":     f"data/manager_decisions.json: {buy_count} BUY decisions in last cycle",
                "proposed_fix": "Review manager_decisions.json manually. If orders were not intentional, revoke Alpaca API keys immediately and audit the approval logic in manager_agent.py.",
                "auto_implementable": False,
            })

        # ── Save findings ─────────────────────────────────────────────────────
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(DATA_DIR / "cit_security_report.json", "w") as f:
                json.dump({
                    "generated_at":   datetime.now().isoformat(),
                    "total_findings": len(findings),
                    "severity_counts": {
                        "CRITICAL": sum(1 for fnd in findings if fnd.get("severity") == "CRITICAL"),
                        "HIGH":     sum(1 for fnd in findings if fnd.get("severity") == "HIGH"),
                        "MEDIUM":   sum(1 for fnd in findings if fnd.get("severity") == "MEDIUM"),
                    },
                    "findings":       findings,
                }, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"  [CITSecurityMonitor] Could not save report: {e}")

        return findings


# =============================================================================
# CODE-AUDITOR SHARED UTILITY
# =============================================================================

import ast as _ast
import re as _re

def _audit_files(agent_name: str, filenames: list) -> list[dict]:
    """
    Scan Python source files for common dangerous patterns.
    Returns a list of CIT findings dicts.
    """
    findings = []
    base = BASE_DIR  # already defined in cit_director.py

    for fname in filenames:
        fpath = base / fname
        if not fpath.exists():
            continue
        try:
            source = fpath.read_text()
            lines  = source.splitlines()
        except Exception as e:
            findings.append({
                "source": agent_name, "severity": "LOW", "category": "SYSTEM",
                "title": f"Could not read {fname}: {e}", "detail": str(e),
            })
            continue

        # ── Pattern 1: fcntl lock acquired without try/finally ──────────
        # Detects: fcntl.flock(f, LOCK_EX) ... fcntl.flock(f, LOCK_UN)
        # without a try/finally in between (lock leaks on exception)
        in_lock_block = False
        has_finally   = False
        lock_line     = 0
        for i, line in enumerate(lines, 1):
            # Skip lines where LOCK_EX/LOCK_SH appears as a string literal
            # (e.g. inspector code checking for "LOCK_EX" in a line)
            _lock_ex_real = ("LOCK_EX" in line or "LOCK_SH" in line) \
                and '"LOCK_EX"' not in line and '"LOCK_SH"' not in line
            if _lock_ex_real:
                in_lock_block = True
                has_finally   = False
                lock_line     = i
            if in_lock_block and "finally" in line:
                has_finally = True
            # Also skip LOCK_UN when it appears as a string literal
            _lock_un_real = in_lock_block and "LOCK_UN" in line \
                and '"LOCK_UN"' not in line
            if _lock_un_real:
                if not has_finally:
                    findings.append({
                        "source":    agent_name,
                        "severity":  "HIGH",
                        "category":  "BUG_FIX",
                        "title":     f"{fname}:{lock_line} — fcntl lock not released in try/finally",
                        "detail":    f"fcntl.LOCK_EX acquired at line {lock_line} but LOCK_UN not in try/finally. Lock leaks if exception occurs.",
                        "proposed_fix": "Wrap the lock body in try/finally: fcntl.flock(f, LOCK_EX) → try: ... finally: fcntl.flock(f, LOCK_UN)",
                    })
                in_lock_block = False  # noqa: reset after lock block

        # ── Pattern 2: open() without 'with' statement ──────────────────
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if _re.search(r'\bopen\s*\(', stripped) and not stripped.startswith('#'):
                if not stripped.startswith('with ') and 'with open' not in stripped:
                    # Check it's an assignment or standalone open() call (not a with statement continued)
                    if _re.search(r'=\s*open\s*\(|^\s*open\s*\(', line):
                        findings.append({
                            "source":   agent_name,
                            "severity": "MEDIUM",
                            "category": "BUG_FIX",
                            "title":    f"{fname}:{i} — open() without 'with' statement (file handle leak)",
                            "detail":   f"Line {i}: {stripped[:80]}",
                            "proposed_fix": "Use 'with open(...) as f:' to ensure file handle is always closed.",
                        })

        # ── Pattern 3: bare except: pass (silent failure) ───────────────
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped in ("except:", "except Exception:") or stripped == "except Exception as e:":
                # Check if the very next non-empty line is just 'pass'
                for j in range(i, min(i + 3, len(lines))):
                    next_stripped = lines[j].strip() if j < len(lines) else ""
                    if next_stripped == "pass":
                        findings.append({
                            "source":   agent_name,
                            "severity": "MEDIUM",
                            "category": "BUG_FIX",
                            "title":    f"{fname}:{i} — silent exception swallowed (except: pass)",
                            "detail":   f"Line {i}: {stripped}  →  pass. Errors are hidden from all logging.",
                            "proposed_fix": "At minimum: except Exception as e: logger.debug(f'non-fatal: {e}')",
                        })
                        break
                    elif next_stripped:
                        break

        # ── Pattern 4: MarketOrderRequest with qty=None ─────────────────
        for i, line in enumerate(lines, 1):
            # Skip lines where these names appear as string literals (e.g. inspector code)
            if '"MarketOrderRequest"' in line or '"LimitOrderRequest"' in line:
                continue
            if "MarketOrderRequest" in line or "LimitOrderRequest" in line:
                # Check next 5 lines for qty=None (skip if it appears as a string literal)
                for j in range(i, min(i + 5, len(lines))):
                    if "qty=None" in lines[j] and '"qty=None"' not in lines[j]:
                        findings.append({
                            "source":   agent_name,
                            "severity": "CRITICAL",
                            "category": "BUG_FIX",
                            "title":    f"{fname}:{j+1} — OrderRequest with qty=None (will raise Pydantic error)",
                            "detail":   f"Alpaca rejects orders where qty is None. Always fetch position qty before building the order.",
                            "proposed_fix": "Fetch position qty first, then build MarketOrderRequest(qty=qty) where qty is a positive int.",
                        })

        # ── Pattern 5: int() or float() on value that could be None ─────
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Look for int(x) or float(x) where x is a .get() return (can be None)
            matches = _re.findall(r'\b(?:int|float)\s*\(\s*(\w+)\.get\s*\(', stripped)
            if matches:
                findings.append({
                    "source":   agent_name,
                    "severity": "MEDIUM",
                    "category": "BUG_FIX",
                    "title":    f"{fname}:{i} — int/float() on .get() result without None guard",
                    "detail":   f"Line {i}: {stripped[:80]} — .get() can return None, causing TypeError.",
                    "proposed_fix": "Use: x = obj.get('key'); val = float(x) if x is not None else 0.0",
                })

        # ── Pattern 6: DataFrame access without .empty check ────────────
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Detect df["col"] or df.col after a get_positions() / get_all_positions() call
            if _re.search(r'\bpositions\s*\[', stripped) and '.empty' not in stripped:
                if not stripped.startswith('#') and not stripped.startswith('if '):
                    if _re.search(r'positions\s*\[\s*["\']', stripped):
                        findings.append({
                            "source":   agent_name,
                            "severity": "MEDIUM",
                            "category": "BUG_FIX",
                            "title":    f"{fname}:{i} — DataFrame column access without .empty check",
                            "detail":   f"Line {i}: {stripped[:80]}",
                            "proposed_fix": "Add 'if not positions.empty:' guard before accessing DataFrame columns.",
                        })

    # Deduplicate by title
    seen   = set()
    unique = []
    for f in findings:
        key = f.get("title", "")[:80]
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


# =============================================================================
# CODE-AUDITOR SUB-AGENTS
# =============================================================================

class CITTradingAuditor:
    """
    Continuously audits the trading execution layer for dangerous code patterns.
    Scans: alpaca_trader.py, main.py, manager_agent.py, board_meeting.py,
           reconciliation_check.py
    """
    name = "CIT Trading Auditor"
    TARGET_FILES = [
        "alpaca_trader.py",
        "main.py",
        "manager_agent.py",
        "board_meeting.py",
        "reconciliation_check.py",
    ]

    def analyze(self) -> list[dict]:
        return _audit_files(self.name, self.TARGET_FILES)


class CITRiskAuditor:
    """
    Continuously audits the risk and oversight layer for dangerous code patterns.
    Scans: canary_agent.py, risk_manager_agent.py, chief_of_staff_agent.py,
           irm_review_agent.py, hr_agent.py, morning_briefing.py
    """
    name = "CIT Risk Auditor"
    TARGET_FILES = [
        "canary_agent.py",
        "risk_manager_agent.py",
        "chief_of_staff_agent.py",
        "irm_review_agent.py",
        "hr_agent.py",
        "morning_briefing.py",
    ]

    def analyze(self) -> list[dict]:
        return _audit_files(self.name, self.TARGET_FILES)


class CITInfraAuditor:
    """
    Continuously audits the infrastructure and research layer for dangerous code patterns.
    Scans: firm_channel.py, issue_tracker.py, rd_team_leader.py,
           earnings_predictor.py, operations_director.py, international_analyst.py,
           cit_director.py
    """
    name = "CIT Infra Auditor"
    TARGET_FILES = [
        "firm_channel.py",
        "issue_tracker.py",
        "rd_team_leader.py",
        "earnings_predictor.py",
        "operations_director.py",
        "international_analyst.py",
        "cit_director.py",
    ]

    def analyze(self) -> list[dict]:
        return _audit_files(self.name, self.TARGET_FILES)


class CITIntegrationAuditor:
    """
    Detects pipeline integration gaps: data written by one module but read
    by nobody, and decision-collection lists that are never persisted.

    This is the class of bug we keep finding — e.g. board engineering votes
    going into requires_code but never reaching cos_meeting_requests.json,
    or ops_director_report.json being written but never read by morning_briefing.

    Runs a cross-file static analysis rather than per-file pattern matching.
    """
    name = "CIT Integration Auditor"

    # Data files we expect every module to produce AND consume.
    # Keys are file paths; values are the modules expected to READ them.
    # If a file is written but its expected readers don't reference it → flag.
    EXPECTED_READERS = {
        "data/reconciliation_report.json":    ["morning_briefing.py"],
        "data/ops_director_report.json":      ["morning_briefing.py"],
        "data/board_meeting_report.json":     ["morning_briefing.py"],
        "data/risk_bulletin.json":            ["manager_agent.py", "morning_briefing.py"],
        "data/canary_state.json":             ["morning_briefing.py", "main.py"],
        "data/chief_of_staff_report.json":    ["morning_briefing.py"],
        "data/hr_report.json":                ["morning_briefing.py"],
        "data/irm_daily_check.json":          ["morning_briefing.py"],
        "data/capital_efficiency_report.json":["morning_briefing.py"],
        "data/cit_pending_proposals.json":    ["morning_briefing.py"],
    }

    # Variables that collect decisions and MUST be persisted to a queue file.
    # Format: (variable_name_fragment, expected_destination_fragment)
    MUST_PERSIST = [
        ("requires_code",   "cos_meeting_requests"),
        ("engineering",     "cos_meeting_requests"),
    ]

    def analyze(self) -> list[dict]:
        findings = []
        base = BASE_DIR

        # ── Check 1: expected readers actually reference the file ──────────
        all_sources = {}
        for fpath in base.glob("*.py"):
            try:
                all_sources[fpath.name] = fpath.read_text()
            except Exception:
                continue

        for data_file, expected_readers in self.EXPECTED_READERS.items():
            fname_fragment = data_file.split("/")[-1]  # e.g. "reconciliation_report.json"
            for reader in expected_readers:
                reader_src = all_sources.get(reader, "")
                if fname_fragment not in reader_src:
                    findings.append({
                        "source":       self.name,
                        "severity":     "HIGH",
                        "category":     "INTEGRATION",
                        "title":        f"{data_file} not read by {reader}",
                        "detail":       (
                            f"{data_file} is written by the pipeline but '{fname_fragment}' "
                            f"does not appear in {reader}. This output is siloed — "
                            f"findings are written to disk but never surfaced."
                        ),
                        "proposed_fix": (
                            f"Add a section in {reader} that loads and surfaces "
                            f"{data_file} so it is visible in the daily briefing."
                        ),
                        "auto_implementable": False,
                    })

        # ── Check 2: decision-collection lists that are never persisted ────
        for py_file, src in all_sources.items():
            for var_fragment, dest_fragment in self.MUST_PERSIST:
                # If a file appends to a matching variable but never writes to the dest
                has_append = (
                    f'["{var_fragment}"]' in src or
                    f"['{var_fragment}']" in src or
                    f'results["{var_fragment}"].append' in src or
                    f"results['{var_fragment}'].append" in src
                )
                has_persist = dest_fragment in src
                if has_append and not has_persist:
                    findings.append({
                        "source":       self.name,
                        "severity":     "HIGH",
                        "category":     "INTEGRATION",
                        "title":        f"{py_file} — '{var_fragment}' collected but never written to {dest_fragment}",
                        "detail":       (
                            f"{py_file} appends to a '{var_fragment}' collection but "
                            f"never writes it to {dest_fragment}. These decisions are "
                            f"logged in the report but never acted on."
                        ),
                        "proposed_fix": (
                            f"After populating '{var_fragment}', write each entry to "
                            f"data/{dest_fragment}.json so the auto-fixer queue picks it up."
                        ),
                        "auto_implementable": False,
                    })

        return findings


class CITWatchdog:
    """
    Scans for problems that were observed but never resolved.
    Checks: stale issue_tracker issues, unacknowledged firm-channel task requests,
    and agent output files for warnings that were never formally filed.
    When something is stuck, escalates to CoS via meeting request.
    """
    name = "CIT Watchdog"

    STALE_ISSUE_HOURS   = 12   # issue open with no update for this long → escalate
    STALE_TASK_HOURS    = 6    # task request with no ack for this long → escalate to CoS
    COS_REQUEST_HOURS   = 24   # if issue still stuck after this long → CoS meeting request

    # Agent output files to cross-check against issue log
    AGENT_OUTPUT_FILES = {
        "canary_agent":    "data/canary_state.json",
        "risk_manager":    "data/risk_bulletin.json",
        "manager_agent":   "data/manager_decisions.json",
        "chief_of_staff":  "data/chief_of_staff_report.json",
    }

    def analyze(self) -> list:
        findings = []
        now = datetime.now()

        # ── 1. Stale issues ───────────────────────────────────────────────
        try:
            from issue_tracker import get_stale_issues, get_open_issues, increment_escalation
            stale = get_stale_issues(hours=self.STALE_ISSUE_HOURS)
            for issue in stale:
                age_h = (now - datetime.fromisoformat(issue["created_at"])).total_seconds() / 3600
                # ── Meta-issue guard: skip issues that are themselves escalation
                # artifacts.  Without this, a CMR creates an issue, the issue
                # goes stale, CIT creates another CMR for it, the CMR creates
                # another issue, ad infinitum (exponential cascade).
                _meta_keywords = (
                    "CoS meeting request", "Stale issue ISS-",
                    "BOARD VOTE", "Market alert: ORANGE", "Market alert: RED",
                )
                _issue_text = issue.get("title", "") + " " + issue.get("description", "")
                if any(kw in _issue_text for kw in _meta_keywords):
                    continue   # never escalate recursive/meta issues
                # Bump escalation count and escalate.
                # Issue records use "issue_id" not "id" — wrong key caused KeyError dropping all escalations.
                increment_escalation(issue["issue_id"])
                findings.append({
                    "source":    self.name,
                    "severity":  issue.get("severity", "MEDIUM"),
                    "category":  "ESCALATION",
                    "title":     f"Stale issue {issue['issue_id']}: {issue['title'][:60]}",
                    "detail":    f"Open for {age_h:.0f}h with no progress. Escalations: {issue.get('escalation_count',0)+1}.",
                    "proposed_fix": "Assign owner and drive to resolution, or call CoS meeting.",
                })
                # If stuck ≥ COS_REQUEST_HOURS, request CoS meeting
                if age_h >= self.COS_REQUEST_HOURS:
                    try:
                        from firm_channel import request_cos_meeting
                        request_cos_meeting(
                            from_agent=self.name,
                            summary=(
                                f"Unresolved issue for {age_h:.0f}h: [{issue.get('severity','?')}] "
                                f"{issue['title']}. Reporter: {issue.get('reporter','?')}. "
                                f"Description: {issue.get('description','')[:120]}"
                            ),
                            urgency="HIGH" if issue.get("severity") in ("HIGH","CRITICAL") else "NORMAL",
                        )
                    except Exception:
                        pass
        except Exception as e:
            findings.append({
                "source": self.name, "severity": "LOW", "category": "SYSTEM",
                "title": f"Watchdog stale-issue scan error: {e}", "detail": str(e),
            })

        # ── 2. Unacknowledged task requests ───────────────────────────────
        # Only watches for tasks sent TO a named recipient that haven't been
        # acknowledged.  Outbound assignments with no to_agent are *broadcast*
        # entries — they are never "pending acknowledgment" from a specific
        # agent and must be skipped to prevent escalation-flood.
        try:
            from firm_channel import get_pending_tasks, request_cos_meeting
            import json as _json, os as _os
            fc_path = "data/firm_channel.json"
            # Load the set of task timestamps already escalated this session
            # (persistent across runs via a small sidecar file)
            _escalated_path = "data/watchdog_escalated_tasks.json"
            _already_escalated: set = set()
            if _os.path.exists(_escalated_path):
                try:
                    with open(_escalated_path) as _ef:
                        _already_escalated = set(_json.load(_ef))
                except Exception:
                    pass
            if _os.path.exists(fc_path):
                with open(fc_path) as f:
                    fc_entries = _json.load(f)
                cutoff = now.timestamp() - self.STALE_TASK_HOURS * 3600
                stale_tasks = [
                    e for e in fc_entries
                    if e.get("category") == "TASK_REQUEST"
                    and bool(e.get("to_agent"))          # must have a named recipient
                    and e.get("to_agent") != "?"         # skip malformed entries
                    and datetime.fromisoformat(e["ts"]).timestamp() < cutoff
                    and e.get("status", "PENDING") == "PENDING"
                    and e.get("ts") not in _already_escalated  # deduplicate across runs
                ]
                _new_escalated = list(_already_escalated)
                for task in stale_tasks[:5]:  # cap at 5 per run
                    findings.append({
                        "source":    self.name,
                        "severity":  "MEDIUM",
                        "category":  "ESCALATION",
                        "title":     f"Unacknowledged task request from {task.get('agent','?')} to {task.get('to_agent','?')}",
                        "detail":    f"Task sitting >{self.STALE_TASK_HOURS}h: {task.get('content','')[:100]}",
                        "proposed_fix": "Recipient should acknowledge or escalate to CoS.",
                    })
                    request_cos_meeting(
                        from_agent=self.name,
                        summary=(
                            f"Task request from {task.get('agent','?')} to {task.get('to_agent','?')} "
                            f"unacknowledged for >{self.STALE_TASK_HOURS}h: {task.get('content','')[:100]}"
                        ),
                        urgency="NORMAL",
                    )
                    _new_escalated.append(task["ts"])
                # Persist updated escalation log (keep last 500 entries to bound size)
                with open(_escalated_path, "w") as _ef:
                    _json.dump(_new_escalated[-500:], _ef)
        except Exception as e:
            findings.append({
                "source": self.name, "severity": "LOW", "category": "SYSTEM",
                "title": f"Watchdog task-request scan error: {e}", "detail": str(e),
            })

        # ── 3. Cross-check: warnings in agent outputs not filed as issues ─
        try:
            from issue_tracker import get_open_issues, log_issue
            import json as _json, os as _os
            open_titles = {i["title"] for i in get_open_issues()}
            for agent_key, filepath in self.AGENT_OUTPUT_FILES.items():
                if not _os.path.exists(filepath):
                    continue
                with open(filepath) as f:
                    data = _json.load(f)
                # Look for RED canary or HIGH/CRITICAL risk not already filed
                if agent_key == "canary_agent":
                    # canary_state.json stores the level under "last_level", not "level".
                    level = data.get("last_level", "GREEN")
                    if level in ("RED", "ORANGE"):
                        title = f"Canary {level} alert — unfiled in issue tracker"
                        if title not in open_titles:
                            log_issue("CIT Watchdog", title,
                                      f"Canary shows {level} but no matching issue found. Triggers: {data.get('triggers',[])}",
                                      severity="HIGH" if level == "RED" else "MEDIUM",
                                      category="RISK")
                            findings.append({
                                "source": self.name, "severity": "HIGH" if level == "RED" else "MEDIUM",
                                "category": "RISK",
                                "title": f"Filed missing canary alert: {title}",
                                "detail": f"Canary level {level} was not recorded in issue tracker.",
                            })
                elif agent_key == "risk_manager":
                    # risk_bulletin.json nests the level under risk_assessment.risk_level.
                    risk_level = (data.get("risk_assessment") or {}).get("risk_level") or data.get("risk_level", "LOW")
                    if risk_level in ("HIGH", "CRITICAL"):
                        title = f"Risk Manager {risk_level} — unfiled in issue tracker"
                        if title not in open_titles:
                            log_issue("CIT Watchdog", title,
                                      f"Risk Manager reported {risk_level} but no matching issue filed.",
                                      severity=risk_level, category="RISK")
                            findings.append({
                                "source": self.name, "severity": risk_level, "category": "RISK",
                                "title": f"Filed missing risk alert: {title}",
                                "detail": f"Risk Manager {risk_level} was not in issue tracker.",
                            })
        except Exception as e:
            findings.append({
                "source": self.name, "severity": "LOW", "category": "SYSTEM",
                "title": f"Watchdog cross-check error: {e}", "detail": str(e),
            })

        return findings


# =============================================================================
# CONTEXT SNAPSHOT — attached to every proposal so deputies have full picture
# =============================================================================

def _build_context_snapshot() -> dict:
    """
    Gather current firm state and attach it to every proposal heading to the board.
    Deputies should never have to ask "what was the system state when this was filed?"
    """
    snap: dict = {"captured_at": datetime.now().isoformat()}

    # Market / risk environment
    try:
        canary = _load_json_safe(DATA_DIR / "canary_state.json")
        snap["canary_level"]     = canary.get("last_level", "UNKNOWN")
        snap["canary_triggers"]  = canary.get("triggers", [])[:3]
    except Exception:
        snap["canary_level"] = "UNKNOWN"

    try:
        risk_rpt = _load_json_safe(DATA_DIR / "risk_manager_report.json")
        ra = risk_rpt.get("risk_assessment") or {}
        snap["cro_level"]       = ra.get("risk_level") or risk_rpt.get("risk_level", "UNKNOWN")
        snap["portfolio_value"] = risk_rpt.get("portfolio_value")
        snap["open_positions"]  = len(risk_rpt.get("positions", []))
    except Exception:
        snap["cro_level"] = "UNKNOWN"

    # Trading state
    try:
        state = _load_json_safe(DATA_DIR / "state.json")
        snap["paper_day"]        = state.get("paper_day", "?")
        snap["cash_balance"]     = state.get("cash_balance")
        snap["loss_streak"]      = state.get("loss_streak", 0)
        snap["win_streak"]       = state.get("win_streak", 0)
        snap["buys_blocked"]     = state.get("buys_blocked", False)
        snap["total_closed_trades"] = len(state.get("sell_log", []))
        last_buy = state.get("buy_log", [{}])[-1]
        snap["last_buy"]         = {
            "ticker": last_buy.get("ticker"),
            "date":   last_buy.get("date"),
        } if last_buy else None
    except Exception:
        pass

    # Pipeline health
    try:
        issues_path = DATA_DIR / "issue_tracker.json"
        if issues_path.exists():
            issues_raw = _load_json_safe(issues_path)
            issues_list = issues_raw if isinstance(issues_raw, list) else issues_raw.get("issues", [])
            open_issues = [i for i in issues_list if i.get("status") not in ("RESOLVED", "CLOSED")]
            snap["open_issues_count"]    = len(open_issues)
            snap["open_critical_issues"] = sum(1 for i in open_issues if i.get("severity") == "CRITICAL")
    except Exception:
        pass

    # Most recent signals
    try:
        import json as _j
        sig_path = DATA_DIR / "latest_signals.json"
        if sig_path.exists():
            sigs = _j.loads(sig_path.read_text())
            if isinstance(sigs, list):
                snap["top_signals"] = [
                    {"ticker": s.get("ticker"), "score": s.get("conviction_score")}
                    for s in sorted(sigs, key=lambda x: x.get("conviction_score", 0), reverse=True)[:5]
                ]
    except Exception:
        pass

    # Active CIT proposals summary
    try:
        pending = _load_json_list_safe(CIT_PENDING)
        snap["pending_proposals_count"] = len(pending)
        snap["critical_pending"]        = sum(1 for p in pending if p.get("severity") == "CRITICAL")
    except Exception:
        pass

    return snap


def _enrich_proposal(finding: dict, context: dict) -> dict:
    """
    Attach the system context snapshot to a proposal.
    Also builds a human-readable context_summary so deputies can read it at a glance.
    """
    finding["context"] = context

    parts = []
    if context.get("canary_level"):
        parts.append(f"Canary: {context['canary_level']}")
    if context.get("cro_level"):
        parts.append(f"CRO: {context['cro_level']}")
    if context.get("portfolio_value"):
        parts.append(f"Portfolio: ${context['portfolio_value']:,.0f}")
    if context.get("open_positions") is not None:
        parts.append(f"Positions: {context['open_positions']} open")
    if context.get("paper_day"):
        parts.append(f"Day {context['paper_day']}")
    if context.get("loss_streak", 0) >= 2:
        parts.append(f"Loss streak: {context['loss_streak']}")
    if context.get("open_critical_issues", 0) > 0:
        parts.append(f"CRITICAL issues open: {context['open_critical_issues']}")

    finding["context_summary"] = " | ".join(parts) if parts else "Context unavailable"
    return finding


# =============================================================================
# CLARIFICATION ENGINE
# Deputies ask → CIT Director answers → proposal updated → re-vote triggered
# =============================================================================

def _deliberate_with_team(question: str, proposal: dict, source: str = "deputy_board") -> list:
    """
    CIT Director goes back to the relevant team member(s) for fresh findings
    before composing the clarification answer.

    Steps:
      1. Identify which CIT agent originated the proposal (proposal["agent"]).
      2. Instantiate that agent and re-run analyze() to get the latest data.
      3. Filter results to findings that overlap keywords with the question/title.
      4. For main_board requests, also consult CITCodeInspector + CITPipelineMonitor.
      5. Return a list of finding strings the director can cite in the answer.
    """
    originator_name = proposal.get("agent", "")
    title_kws = set(w.lower() for w in re.split(r'\W+', proposal.get("title", "")) if len(w) > 4)
    q_kws     = set(w.lower() for w in re.split(r'\W+', question) if len(w) > 4)
    all_kws   = title_kws | q_kws

    # Map agent name → class
    _agent_map = {
        "CITCodeInspector":       CITCodeInspector,
        "CITPipelineMonitor":     CITPipelineMonitor,
        "CITStrategyAnalyst":     CITStrategyAnalyst,
        "CITArchitectureAdvisor": CITArchitectureAdvisor,
        "CITThinkTank":           CITThinkTank,
        "CITDataQualityAnalyst":  CITDataQualityAnalyst,
        "CITProposalValidator":   CITProposalValidator,
        "CITSelfImprovement":     CITSelfImprovement,
        "CITSecurityMonitor":     CITSecurityMonitor,
        "CITTradingAuditor":      CITTradingAuditor,
        "CITRiskAuditor":         CITRiskAuditor,
        "CITInfraAuditor":        CITInfraAuditor,
        "CITIntegrationAuditor":  CITIntegrationAuditor,
        "CITWatchdog":            CITWatchdog,
    }

    # Agents to consult — always include originator; main_board also gets breadth
    agents_to_consult = []
    if originator_name in _agent_map:
        agents_to_consult.append(originator_name)

    if source == "main_board":
        # Board wants maximum rigor — also consult code + pipeline specialists
        for extra in ("CITCodeInspector", "CITPipelineMonitor", "CITDataQualityAnalyst"):
            if extra not in agents_to_consult:
                agents_to_consult.append(extra)

    team_findings = []
    for aname in agents_to_consult:
        cls = _agent_map.get(aname)
        if cls is None:
            continue
        try:
            fresh = cls().analyze()
        except Exception as _ae:
            logger.debug(f"  [CIT Deliberate] {aname}.analyze() failed: {_ae}")
            continue

        for f in fresh:
            f_text = f"{f.get('title','')} {f.get('description','')} {f.get('evidence','')}".lower()
            # Keep findings that share keywords with the question/proposal
            if any(kw in f_text for kw in all_kws):
                snippet = (
                    f"[{aname}] {f.get('title','?')}: {f.get('evidence','')[:200]} "
                    f"| fix: {f.get('proposed_fix','')[:200]}"
                )
                team_findings.append(snippet)
                if len(team_findings) >= 6:
                    break
        if len(team_findings) >= 6:
            break

    return team_findings


def _answer_clarification_question(question: str, proposal: dict,
                                   source: str = "deputy_board") -> str:
    """
    Given a specific question and the original proposal, generate a concrete
    answer by:
      1. Deliberating with the team — re-running relevant CIT agents.
      2. Inspecting actual source files for file:line evidence.
      3. Extracting metrics and function signatures.

    The 'source' parameter distinguishes deputy_board vs main_board requests;
    main_board requests trigger broader team consultation.
    """
    ev    = proposal.get("evidence", "")
    fix   = proposal.get("proposed_fix", "")
    desc  = proposal.get("description", "")
    title = proposal.get("title", "")
    q     = question.lower()

    parts = []

    # ── 1. Deliberate with the team ───────────────────────────────────────
    team_input = _deliberate_with_team(question, proposal, source=source)
    if team_input:
        parts.append("CIT team deliberation findings:")
        for finding in team_input:
            parts.append(f"  • {finding}")

    # ── 2. Verify file:line reference ─────────────────────────────────────
    file_match = re.search(r'(\w+\.py):?(\d+)?', ev + " " + title)
    if file_match:
        fname  = file_match.group(1)
        lineno = int(file_match.group(2)) if file_match.group(2) else None
        fpath  = BASE_DIR / fname
        if fpath.exists():
            try:
                src_lines = fpath.read_text().splitlines()
                if lineno and lineno <= len(src_lines):
                    actual = src_lines[lineno - 1].strip()
                    parts.append(f"Verified location — {fname}:{lineno}: `{actual}`")
                    ctx = [f"  {i+1}: {src_lines[i].rstrip()}"
                           for i in range(max(0, lineno-2), min(len(src_lines), lineno+2))]
                    parts.append("Context:\n" + "\n".join(ctx))
                elif lineno is None:
                    keywords = [w for w in re.split(r'\W+', title) if len(w) > 4]
                    for kw in keywords:
                        hits = [(i+1, l.strip()) for i, l in enumerate(src_lines)
                                if kw.lower() in l.lower() and l.strip()]
                        if hits:
                            n, code = hits[0]
                            parts.append(f"Found in {fname}:{n}: `{code[:80]}`")
                            break
            except Exception:
                pass

    # ── 3. Fix specificity — pull function signatures ─────────────────────
    if any(kw in q for kw in ("specific", "function", "code", "exact", "file", "how")):
        func_match = re.findall(r'`([^`]+)`|(\w+)\(\)', desc + " " + fix)
        funcs = [m[0] or m[1] for m in func_match if (m[0] or m[1])]
        if funcs:
            parts.append(f"Relevant functions/methods: {', '.join(funcs[:5])}")
        if file_match:
            fname = file_match.group(1)
            fpath = BASE_DIR / fname
            if fpath.exists():
                src = fpath.read_text()
                for fn in funcs[:3]:
                    m = re.search(rf'def {re.escape(fn)}\s*\([^)]*\)', src)
                    if m:
                        fn_lineno = src[:m.start()].count("\n") + 1
                        parts.append(f"Function signature: {fname}:{fn_lineno} — `{m.group()}`")
                        break

    # ── 4. Evidence metrics — pull numbers from description ───────────────
    if any(kw in q for kw in ("metric", "data", "evidence", "verif", "measur", "impact", "quantif")):
        metrics = re.findall(r'\d+[\d,.]*\s*(?:ms|s|h|%|k|M|B)?|\$[\d,]+|data/\w+\.json', desc)
        if metrics:
            parts.append(f"Concrete metrics from description: {', '.join(metrics[:5])}")
        data_files = re.findall(r'data/[\w.]+\.json', ev + " " + desc)
        for df in data_files[:2]:
            dfpath = BASE_DIR / df
            if dfpath.exists():
                age_h = _file_age_hours(dfpath)
                parts.append(
                    f"Verified: {df} exists, age={age_h:.1f}h" if age_h
                    else f"Verified: {df} exists"
                )

    # ── 5. Always append full evidence + fix for completeness ─────────────
    if ev:
        parts.append(f"Full evidence: {ev[:300]}")
    if fix:
        parts.append(f"Full proposed fix: {fix[:400]}")

    return "\n".join(parts) if parts else (
        f"CIT Director response — evidence: {ev[:200]} | fix: {fix[:200]} | "
        f"confidence: {proposal.get('confidence','?')}%"
    )


def _process_clarification_requests() -> int:
    """
    Read every PENDING clarification request, generate a specific answer,
    update the source proposal's evidence/fix fields with richer info,
    and mark it for re-vote by the deputy board.

    Returns the number of clarifications answered.
    """
    clarify_path = DATA_DIR / "cit_clarification_requests.json"
    if not clarify_path.exists():
        return 0

    clarifications = _load_json_list_safe(clarify_path)
    pending_clr    = [c for c in clarifications if c.get("status") == "PENDING"]
    if not pending_clr:
        return 0

    # Load pending proposals for update
    proposals = _load_json_list_safe(CIT_PENDING)
    prop_index = {p.get("id"): i for i, p in enumerate(proposals)}

    # Group all PENDING questions by proposal_id so we answer all at once
    from collections import defaultdict as _dd
    pending_by_proposal: dict = _dd(list)
    for clr in clarifications:
        if clr.get("status") == "PENDING":
            pending_by_proposal[clr.get("proposal_id")].append(clr)

    answered = 0
    now_iso  = datetime.now().isoformat()

    for pid, clr_group in pending_by_proposal.items():
        pIdx = prop_index.get(pid)
        if pIdx is None:
            for clr in clr_group:
                clr["status"] = "STALE"
            continue

        prop       = proposals[pIdx]
        clr_source = clr_group[0].get("source", "deputy_board")
        askers     = [c.get("requesting_deputy", c.get("asked_by", "Deputy")) for c in clr_group]

        _ptitle = prop.get("title", "?")[:55]
        logger.info(
            f"  [CIT Director] Deliberating with team on '{_ptitle}' — "
            f"{len(clr_group)} simultaneous question(s) from: {', '.join(askers)}"
        )

        # Answer all questions in one team deliberation pass
        combined_parts = []
        for i, clr in enumerate(clr_group, 1):
            q      = clr.get("question", "")
            deputy = clr.get("requesting_deputy", clr.get("asked_by", "Deputy"))
            answer = _answer_clarification_question(q, prop, source=clr_source)

            clr["status"]      = "ANSWERED"
            clr["answered_at"] = now_iso
            clr["answer"]      = answer

            combined_parts.append(
                f"Q{i} [{deputy}]: {q[:200]}\nA{i}: {answer[:400]}"
            )

            prop.setdefault("clarification_log", []).append({
                "ts":       now_iso,
                "deputy":   deputy,
                "source":   clr_source,
                "question": q,
                "answer":   answer,
            })

            # Upgrade evidence field if the answer surfaced a file:line
            loc_match = re.search(r'(\w+\.py:\d+)', answer)
            if loc_match and loc_match.group(1) not in prop.get("evidence", ""):
                prop["evidence"] = loc_match.group(1) + " — " + prop.get("evidence", "")

        # One re-vote flag per proposal regardless of how many questions were asked
        prop["needs_revote"]  = True
        prop["revote_reason"] = (
            f"CIT Director answered {len(clr_group)} simultaneous question(s) from "
            f"{', '.join(askers)} after team deliberation."
        )
        proposals[pIdx] = prop

        logger.info(
            f"  [CIT Director] All {len(clr_group)} question(s) answered for '{_ptitle}':\n"
            + "\n".join(f"    {p[:180]}" for p in combined_parts)
        )
        answered += len(clr_group)

    # Persist updated clarifications and proposals
    DATA_DIR.mkdir(exist_ok=True)
    with open(clarify_path, "w") as f:
        json.dump(clarifications, f, indent=2, default=str)
    with open(CIT_PENDING, "w") as f:
        json.dump(proposals, f, indent=2, default=str)

    # If any main_board questions were just answered, convene a quick revote
    # immediately so proposals don't sit until the next scheduled full board cycle.
    main_board_answered = sum(
        1 for pid, clr_group in pending_by_proposal.items()
        for c in clr_group
        if c.get("source") == "main_board" and c.get("status") == "ANSWERED"
    )
    if main_board_answered:
        logger.info(
            f"  [CIT Director] {main_board_answered} main_board clarification(s) answered — "
            f"triggering quick board revote now."
        )
        try:
            from board_meeting import run_board_clarification_revote
            run_board_clarification_revote()
        except Exception as _brev_err:
            logger.warning(f"  [CIT Director] Quick board revote failed (non-fatal): {_brev_err}")

    return answered


# =============================================================================
# DIRECTOR
# =============================================================================

class CITDirector:
    """
    Orchestrates all four CIT agents, synthesises findings, applies the
    sensitivity filter, and routes everything to the correct output channel.
    """

    name = "CITDirector"

    def __init__(self):
        self.agents = [
            CITCodeInspector(),
            CITPipelineMonitor(),
            CITStrategyAnalyst(),
            CITArchitectureAdvisor(),
            CITThinkTank(),
            CITDataQualityAnalyst(),
            CITProposalValidator(),
            CITSelfImprovement(),
            CITSecurityMonitor(),
            CITTradingAuditor(),
            CITRiskAuditor(),
            CITInfraAuditor(),
            CITIntegrationAuditor(),
            CITWatchdog(),
        ]

    def _load_existing_ids(self) -> set:
        """Collect all proposal IDs already in pending/approved/backlog to avoid duplicates."""
        ids = set()
        for path in [CIT_PENDING, CIT_APPROVED, CIT_BACKLOG]:
            for item in _load_json_list_safe(path):
                ids.add(item.get("id", ""))
        return ids

    def _dedup(self, findings: list, existing_ids: set) -> list:
        """Assign IDs and remove any that are exact title-matches with existing proposals."""
        existing_titles = set()
        for path in [CIT_PENDING, CIT_APPROVED, CIT_BACKLOG]:
            for item in _load_json_list_safe(path):
                existing_titles.add(item.get("title", "").lower().strip())

        result = []
        for f in findings:
            title_key = f.get("title", "").lower().strip()
            if title_key in existing_titles:
                continue   # already proposed — skip
            if "id" not in f:
                f["id"] = _next_id("CIT", result + list({"id": i} for i in existing_ids))
            result.append(f)
        return result

    def _update_history(self, agent_name: str, n_findings: int):
        """Update per-agent finding count in history file."""
        history = _load_json_safe(CIT_HISTORY)
        agents  = history.setdefault("agents", {})
        entry   = agents.setdefault(agent_name, {"total_findings": 0, "board_proposals": 0, "sensitive": 0})
        entry["total_findings"] += n_findings
        entry["last_run"] = datetime.now().isoformat()
        try:
            # Guard write so a disk error here never aborts the entire CIT run.
            DATA_DIR.mkdir(exist_ok=True)
            with open(CIT_HISTORY, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"  [CIT] _update_history write failed: {e}")

    def run(self, session: str = "auto") -> dict:
        if session == "auto":
            session = "morning" if datetime.now().hour < 14 else "evening"

        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — CONTINUOUS IMPROVEMENT TEAM")
        logger.info(f"  {session.upper()} SESSION · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        logger.info("=" * 60)

        all_findings  = []
        existing_ids  = self._load_existing_ids()

        # ── Inbox: answer pending deputy clarification requests ──────────────
        # Deputies CLARIFY instead of APPROVE when evidence is vague or fix is
        # unclear. The CIT Director answers each question here, enriches the
        # proposal, and marks it for re-vote by the deputy board next session.
        try:
            _n_answered = _process_clarification_requests()
            if _n_answered:
                logger.info(
                    f"\n  [CIT] Answered {_n_answered} deputy clarification request(s). "
                    f"Those proposals are now flagged needs_revote=True and will be "
                    f"re-evaluated by the deputy board at its next session."
                )
            else:
                logger.info("  [CIT] No pending deputy clarification requests.")
        except Exception as _clr_err:
            logger.warning(f"  [CIT] Clarification engine error (non-fatal): {_clr_err}")

        # ── Inbox: pending task requests addressed to CIT Director ────────────
        # HR Agent reports up to CIT Director after every workforce review.
        # CITWatchdog and other agents may also post here.
        try:
            from firm_channel import get_pending_tasks as _get_tasks
            _pending = _get_tasks("CIT Director")
            if _pending:
                logger.info(f"\n  [CIT] Inbox: {len(_pending)} pending task request(s):")
                for _t in _pending:
                    logger.info(f"    • From {_t.get('from','?')}: {_t.get('task','')[:120]}")
            else:
                logger.info("  [CIT] Inbox: no pending task requests.")
        except Exception as _inbox_e:
            logger.debug(f"  [CIT] Inbox check error (non-fatal): {_inbox_e}")

        # ── 0. Load Performance Ledger context ───────────────────────────────
        _ledger_context = {}
        try:
            from performance_ledger import get_all_agent_stats
            _ledger_context = get_all_agent_stats()
            if _ledger_context:
                logger.info(
                    f"  [CIT] Performance ledger loaded: {len(_ledger_context)} agent(s) tracked."
                )
        except Exception as _le:
            logger.debug(f"  [CIT] Performance ledger load error (non-fatal): {_le}")

        # ── 1. Run all sub-agents ─────────────────────────────────────────────
        for agent in self.agents:
            logger.info(f"\n  [{agent.name}] Analysing...")
            try:
                raw = agent.analyze()
                logger.info(f"  [{agent.name}] {len(raw)} finding(s)")
                all_findings.extend(raw)
                self._update_history(agent.name, len(raw))
            except Exception as e:
                logger.warning(f"  [{agent.name}] ERROR: {e}")

        # ── 2. Deduplicate ───────────────────────────────────────────────────
        findings = self._dedup(all_findings, existing_ids)
        logger.info(f"\n  {len(findings)} new findings after deduplication")

        # ── 2b. Enrich every finding with full system context snapshot ────────
        # Deputies need to know the firm state when the finding was generated —
        # canary level, CRO level, portfolio value, open issues, etc.
        _ctx = _build_context_snapshot()
        findings = [_enrich_proposal(f, _ctx) for f in findings]
        logger.info(
            f"  Context snapshot attached: canary={_ctx.get('canary_level','?')}, "
            f"CRO={_ctx.get('cro_level','?')}, portfolio=${_ctx.get('portfolio_value') or 0:,.0f}"
        )

        # ── 3. Route findings ────────────────────────────────────────────────
        board_queue = []
        backlog     = []
        sensitive   = []
        quick_wins  = []
        think_tank  = []

        for f in findings:
            route = _classify_finding(f)
            if route == "sensitive":
                sensitive.append(f)
            elif f.get("category") == "QUICK_WIN":
                quick_wins.append(f)
            elif f.get("category") == "SECURITY":
                # Security findings bypass board — Director implements immediately
                quick_wins.append(f)
            elif route == "board":
                board_queue.append(f)
                if f.get("category") == "VISION":
                    think_tank.append(f)   # also log to think_tank for history
            else:
                backlog.append(f)

        # ── 4. Persist to files ──────────────────────────────────────────────
        DATA_DIR.mkdir(exist_ok=True)

        # Board queue: append to pending proposals (board votes at next meeting)
        existing_pending = _load_json_list_safe(CIT_PENDING)
        existing_pending.extend(board_queue)
        with open(CIT_PENDING, "w") as f:
            json.dump(existing_pending, f, indent=2, default=str)

        # Backlog: append
        existing_backlog = _load_json_list_safe(CIT_BACKLOG)
        existing_backlog.extend(backlog)
        with open(CIT_BACKLOG, "w") as f:
            json.dump(existing_backlog, f, indent=2, default=str)

        # Sensitive: append (never logged to console beyond count)
        existing_sensitive = _load_json_list_safe(CIT_SENSITIVE)
        existing_sensitive.extend(sensitive)
        with open(CIT_SENSITIVE, "w") as f:
            json.dump(existing_sensitive, f, indent=2, default=str)

        # Think tank: overwrite each session (these are living proposals, not a queue)
        existing_tt = _load_json_list_safe(CIT_THINK_TANK)
        tt_existing_titles = {p.get("title", "").lower() for p in existing_tt}
        new_tt = [p for p in think_tank if p.get("title", "").lower() not in tt_existing_titles]
        existing_tt.extend(new_tt)
        with open(CIT_THINK_TANK, "w") as f:
            json.dump(existing_tt, f, indent=2, default=str)

        # ── 4b. Log all new findings to issue tracker ────────────────────────
        _logged_issue_ids: dict[str, str] = {}   # finding title → issue_id
        try:
            import issue_tracker as _issue_tracker

            _cat_map = {
                "CODE_BUG":    "CODE_BUG",
                "BUG_FIX":     "CODE_BUG",
                "QUICK_WIN":   "CODE_BUG",
                "SECURITY":    "SECURITY",
                "PIPELINE":    "PIPELINE",
                "STRATEGY":    "STRATEGY",
                "IMPROVEMENT": "PIPELINE",
                "NEW_FEATURE": "PIPELINE",
                "VISION":      "STRATEGY",
            }
            _sev_map = {
                "CRITICAL": "CRITICAL",
                "HIGH":     "HIGH",
                "MEDIUM":   "MEDIUM",
                "LOW":      "LOW",
            }

            for _f in findings:
                try:
                    _cat  = _cat_map.get(_f.get("category", ""), "PIPELINE")
                    _sev  = _sev_map.get(_f.get("severity", ""), "MEDIUM")
                    _hint = _f.get("proposed_fix") or _f.get("auto_implementable") and "Auto-implementable" or None
                    _iid  = _issue_tracker.log_issue(
                        reporter=_f.get("agent", "CIT"),
                        title=_f.get("title", "Untitled finding"),
                        description=_f.get("description", ""),
                        severity=_sev,
                        category=_cat,
                        auto_fix_hint=str(_hint) if _hint else None,
                    )
                    _logged_issue_ids[_f.get("title", "")] = _iid
                    _f["issue_id"] = _iid   # attach issue_id to finding for reference
                except Exception as _log_err:
                    logger.debug(f"  [CIT] Issue log error (non-fatal): {_log_err}")
        except Exception as _it_import_err:
            logger.debug(f"  [CIT] Issue tracker import error (non-fatal): {_it_import_err}")

        # ── 4c. Resolution Engine ─────────────────────────────────────────────
        #
        # After findings are collected and logged, the CIT Director acts as a
        # resolution engine — not just a reporter. Three phases:
        #   A. Auto-fix   — things the Director can fix without asking anyone
        #   B. Delegation — specific task requests to named agents/teams
        #   C. Escalation — things needing a board vote
        # + Stale issue audit at the end.

        try:
            import issue_tracker as _it

            # ── Phase A: Auto-fix ─────────────────────────────────────────────
            _auto_fixed = 0

            # A1. File permission hardening — .env and oci_key.pem
            import os as _os
            _sensitive_files = [
                str(BASE_DIR / ".env"),
                str(BASE_DIR / "oci_key.pem"),
            ]
            for _sf in _sensitive_files:
                if _os.path.exists(_sf):
                    try:
                        _mode = oct(_os.stat(_sf).st_mode)[-3:]
                        if _mode not in ("600", "400"):
                            _os.chmod(_sf, 0o600)
                            _msg = f"File permissions fixed: {_sf} set to 600"
                            logger.info(f"  [CIT AutoFix] {_msg}")
                            _perm_iid = _it.log_issue(
                                reporter="CIT Director",
                                title=f"Insecure permissions on {_os.path.basename(_sf)}",
                                description=f"File had permissions {_mode} — corrected to 600.",
                                severity="HIGH",
                                category="SECURITY",
                                auto_fix_hint="chmod 600",
                            )
                            _it.resolve_issue(_perm_iid, _msg, "CIT Director (auto-fix)")
                            _auto_fixed += 1
                    except Exception as _perm_err:
                        logger.debug(f"  [CIT AutoFix] Permission check error: {_perm_err}")

            # A2. Stale log/snapshot cleanup — data files older than 7 days
            import glob as _glob
            _non_critical_patterns = [
                str(DATA_DIR / "cit_morning_report.json"),
                str(DATA_DIR / "cit_evening_report.json"),
            ]
            _log_files = _glob.glob(str(LOG_DIR / "*.log.*")) if LOG_DIR.exists() else []
            _stale_threshold = 7 * 24 * 3600   # 7 days in seconds
            _now_ts = __import__("time").time()
            for _lf in _log_files[:20]:   # cap scan to prevent runaway
                try:
                    if _now_ts - _os.path.getmtime(_lf) > _stale_threshold:
                        _os.remove(_lf)
                        logger.info(f"  [CIT AutoFix] Removed stale log: {_os.path.basename(_lf)}")
                        _auto_fixed += 1
                except Exception:
                    pass

            # A3. Mark existing stale open issues as STALE and bump escalation_count
            _stale_open = _it.get_stale_issues(hours=48)
            for _si in _stale_open:
                _siid = _si["issue_id"]
                _new_count = _it.increment_escalation(_siid)
                _it.update_issue(_siid, "STALE", notes=f"Stale >48h — escalation_count now {_new_count}")

            if _auto_fixed > 0 or _stale_open:
                logger.info(
                    f"  [CIT AutoFix] {_auto_fixed} auto-fix(es) applied, "
                    f"{len(_stale_open)} stale issue(s) bumped"
                )

            # ── Phase B: Delegation ───────────────────────────────────────────
            try:
                from firm_channel import post_task_request as _fc_task

                for _f in findings:
                    _fcat = _f.get("category", "")
                    _fsev = _f.get("severity", "")
                    _ftitle = _f.get("title", "")
                    _fdesc  = _f.get("description", "")[:200]
                    _fiid   = _f.get("issue_id")

                    if not _fiid:
                        continue

                    # Code bugs → Chairman (Claude) via CoS meeting request
                    # "Engineering" is not an agent — code fixes require Claude's intervention.
                    # Route HIGH/CRITICAL bugs as CoS meeting requests so they surface at next check-in.
                    if _fcat in ("BUG_FIX", "QUICK_WIN", "CODE_BUG") or _f.get("proposed_fix"):
                        _fix = _f.get("proposed_fix", _fdesc[:100])
                        if _fsev in ("CRITICAL", "HIGH"):
                            try:
                                from firm_channel import request_cos_meeting as _rcm
                                _rcm(
                                    from_agent="CIT Director",
                                    summary=f"CODE BUG requires Chairman fix: {_ftitle}. Proposed fix: {_fix[:200]}",
                                    urgency="HIGH",
                                )
                            except Exception:
                                pass
                        _it.update_issue(_fiid, "ASSIGNED", assigned_to="Chairman (Claude)")

                    # Pipeline issues → Chief of Staff for coordination
                    elif _fcat == "PIPELINE":
                        _fc_task(
                            from_agent="CIT Director",
                            to_agent="Chief of Staff",
                            task=f"Pipeline issue: {_ftitle}. {_fdesc[:150]}",
                            priority="HIGH" if _fsev in ("CRITICAL", "HIGH") else "NORMAL",
                            issue_id=_fiid,
                        )
                        _it.update_issue(_fiid, "ASSIGNED", assigned_to="Chief of Staff")

                    # Risk issues → Manager Agent
                    elif _fcat in ("RISK",):
                        _fc_task(
                            from_agent="CIT Director",
                            to_agent="Manager Agent",
                            task=f"Risk finding: {_ftitle}. {_fdesc[:150]}",
                            priority="HIGH",
                            issue_id=_fiid,
                        )
                        _it.update_issue(_fiid, "ASSIGNED", assigned_to="Manager Agent")

            except Exception as _del_err:
                logger.debug(f"  [CIT Delegation] Error (non-fatal): {_del_err}")

            # ── Phase C: Escalation to board ─────────────────────────────────
            # CRITICAL severity issues always escalate to board, regardless of route
            _escalated = 0
            for _f in findings:
                _fiid  = _f.get("issue_id")
                _fsev  = _f.get("severity", "")
                _fcat  = _f.get("category", "")
                if not _fiid:
                    continue
                _iss   = _it.get_issue(_fiid)
                if not _iss:
                    continue

                _should_escalate = (
                    _fsev == "CRITICAL"
                    or _fcat in ("STRATEGY", "SECURITY")
                    and _fsev in ("CRITICAL", "HIGH")
                )
                if _should_escalate:
                    # Add issue_id reference to the finding before it hits board queue
                    _f["escalated_issue_id"] = _fiid
                    _it.update_issue(_fiid, "ESCALATED", assigned_to="Board")
                    _escalated += 1

            if _escalated:
                logger.info(f"  [CIT Escalation] {_escalated} finding(s) escalated to board")

            # ── Phase D: Stale issue audit and emergency check ────────────────
            _stale_now = _it.get_stale_issues(hours=48)
            _meta_kw   = ("CoS meeting request", "Stale issue ISS-",
                          "BOARD VOTE", "Market alert:")
            _hi_stale  = [
                i for i in _stale_now
                if i.get("severity") in ("CRITICAL", "HIGH")
                and i.get("escalation_count", 0) >= 3
                and not any(kw in i.get("title", "") + i.get("description", "")
                            for kw in _meta_kw)
            ]
            if _hi_stale:
                logger.warning(
                    f"  [CIT Stale Audit] {len(_hi_stale)} HIGH/CRITICAL issue(s) with "
                    f"≥3 escalations — triggering emergency board meeting"
                )
                for _hi in _hi_stale:
                    logger.warning(
                        f"    [{_hi['issue_id']}] {_hi['title']} "
                        f"(escalations: {_hi.get('escalation_count', 0)})"
                    )
                try:
                    from board_meeting import call_emergency_board_meeting
                    _reasons = "; ".join(i["title"] for i in _hi_stale[:3])
                    call_emergency_board_meeting(
                        reason=f"Stale unresolved HIGH/CRITICAL issues: {_reasons}",
                        severity="CRITICAL",
                        caller="CIT Director",
                        anonymous=False,
                    )
                except Exception as _ebm_err:
                    logger.warning(f"  [CIT] Emergency board meeting error: {_ebm_err}")

            # Post stale issues to firm channel as a reminder
            if _stale_now:
                try:
                    from firm_channel import post as _fc_post
                    _stale_titles = "; ".join(
                        f"[{i['issue_id']}] {i['title'][:40]}" for i in _stale_now[:5]
                    )
                    _fc_post(
                        "CIT Director",
                        "PIPELINE_ALERT",
                        f"Stale issue reminder: {len(_stale_now)} issue(s) unresolved >48h. "
                        f"Top: {_stale_titles}",
                        priority="HIGH" if _hi_stale else "NORMAL",
                    )
                except Exception:
                    pass

        except Exception as _resolution_err:
            logger.warning(f"  [CIT Resolution Engine] Error (non-fatal): {_resolution_err}")

        # ── 5. Main session report ───────────────────────────────────────────
        # Build per-sub-agent findings breakdown so the HR agent can verify
        # that CITCodeInspector, CITPipelineMonitor, etc. are producing output.
        # Keyed by the HR-expected subkeys (code_inspector, pipeline_monitor, …)
        _subkey_map = {
            "CITCodeInspector":       "code_inspector",
            "CITPipelineMonitor":     "pipeline_monitor",
            "CITStrategyAnalyst":     "strategy_analyst",
            "CITArchitectureAdvisor": "architecture_advisor",
        }
        _agent_findings_breakdown: dict = {v: [] for v in _subkey_map.values()}
        for _raw_finding in all_findings:
            _agent_name = _raw_finding.get("agent", _raw_finding.get("source", ""))
            _sk = _subkey_map.get(_agent_name)
            if _sk:
                _agent_findings_breakdown[_sk].append(_raw_finding)

        report = {
            "generated_at":    datetime.now().isoformat(),
            "session":          session,
            "total_findings":   len(findings),
            "board_proposals":  len(board_queue),
            "quick_wins":       len(quick_wins),
            "backlog":          len(backlog),
            "sensitive_count":  len(sensitive),   # count only — no content in report
            "proposals":        board_queue,
            "quick_win_items":  quick_wins,
            "agent_ledger_stats": _ledger_context,   # performance ledger context
            "agent_findings":   _agent_findings_breakdown,  # per-sub-agent — read by HR
        }

        rpt_path = CIT_MORNING_RPT if session == "morning" else CIT_EVENING_RPT
        with open(rpt_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        # ── 6. Handle sensitive items ─────────────────────────────────────────
        if sensitive:
            self._handle_sensitive(sensitive)

        # ── 7. Summary log ───────────────────────────────────────────────────
        logger.info(f"\n  CIT {session.upper()} REPORT COMPLETE")
        logger.info(f"  Board proposals queued:  {len(board_queue)}")
        logger.info(f"  Quick wins identified:   {len(quick_wins)}")
        logger.info(f"  Backlog (low confidence):{len(backlog)}")
        logger.info(f"  Think Tank proposals:    {len(think_tank)} (Chairman review — data/cit_think_tank.json)")
        logger.info(f"  Sensitive items:         {len(sensitive)} (routed to Director only)")

        for p in board_queue:
            sev = p.get("severity","")
            cat = p.get("category","")
            logger.info(f"  → [{sev:8s}] {cat:15s} | {p.get('title','')[:60]}")

        if CIT_HR_MEMO.exists():
            logger.info(f"  → HR memo written → {CIT_HR_MEMO.name} (zero-pick streak context for HR team)")

        # ── 8. Broadcast to firm channel ─────────────────────────────────────
        try:
            from firm_channel import post as _fc_post
            # Post a session summary
            _fc_post(
                "CIT Director",
                "PROPOSAL",
                f"{session.upper()} session complete: {len(board_queue)} board proposal(s), "
                f"{len(quick_wins)} quick win(s), {len(think_tank)} ThinkTank idea(s). "
                f"Total new findings: {len(findings)}.",
                priority="HIGH" if board_queue else "NORMAL",
            )
            # Post each board proposal individually so other agents see specifics
            for p in board_queue[:5]:
                _fc_post(
                    "CIT Director",
                    "PROPOSAL" if p.get("category") != "VISION" else "IMPROVEMENT",
                    f"[{p.get('severity','?')}] {p.get('title','?')}: {p.get('description','')[:120]}",
                    priority="HIGH" if p.get("severity") in ("CRITICAL","HIGH") else "NORMAL",
                )
            # Post ThinkTank VISION proposals prominently
            for tt in think_tank[:3]:
                _fc_post(
                    "CIT ThinkTank",
                    "IMPROVEMENT",
                    f"VISION PROPOSAL — {tt.get('title','?')}: {tt.get('description','')[:140]}",
                    priority="HIGH",
                )
        except Exception as _fc_err:
            logger.debug(f"  [FirmChannel] Post failed: {_fc_err}")

        # ── 9. Convene Deputy Board for bug fix proposals ─────────────────────
        # Bug fixes and logic errors don't need the full board — deputies vote
        # and approved fixes go straight to the implementation queue.
        bug_proposals_queued = [
            p for p in board_queue
            if p.get("category", "") in {"BUG_FIX", "LOGIC_ERROR", "INTEGRATION"}
        ]
        if bug_proposals_queued:
            logger.info(
                f"\n  [CIT] {len(bug_proposals_queued)} bug fix proposal(s) — "
                f"convening Deputy Board..."
            )
            try:
                from deputy_board import run_deputy_board
                _db_report = run_deputy_board(dry_run=False)
                logger.info(
                    f"  [CIT] Deputy Board: "
                    f"{_db_report.get('approved_count', 0)} approved, "
                    f"{_db_report.get('rejected_count', 0)} rejected, "
                    f"{_db_report.get('deferred_count', 0)} deferred"
                )
            except Exception as _db_err:
                logger.warning(f"  [CIT] Deputy Board error: {_db_err}")
        else:
            logger.info("  [CIT] No bug fix proposals this session — Deputy Board not needed.")

        return report

    def _handle_sensitive(self, sensitive: list):
        """
        Process findings that must not go to the board.
        Most are self-directed (Director/Claude decides).
        If the finding is urgent and requires Chairman input, email immediately.
        """
        urgent = [s for s in sensitive if s.get("severity") in ("CRITICAL", "HIGH")]

        if not urgent:
            # Director handles non-urgent sensitive items internally — no email needed
            logger.info(f"  [Sensitive] {len(sensitive)} item(s) logged to cit_sensitive.json — no Chairman action required")
            return

        # Urgent sensitive item — email Chairman for guidance
        logger.warning(f"  [Sensitive] {len(urgent)} URGENT sensitive item(s) — emailing Chairman...")
        try:
            from email_sender import send_alert_email
            titles = "\n".join(f"  • [{s.get('severity')}] {s.get('title','')}" for s in urgent)
            body = f"""
            <p><strong>The Continuous Improvement Team has identified {len(urgent)} urgent finding(s)
            that cannot be routed to the board due to confidentiality constraints.</strong></p>
            <p>These findings touch internal governance mechanisms that are not for general circulation.
            Your guidance is requested on how to proceed.</p>
            <p><strong>Item(s) requiring your input:</strong></p>
            <pre style="background:#161b22;color:#e6edf3;padding:12px;border-radius:6px;font-size:13px;">{titles}</pre>
            <p>Full details are in <code>data/cit_sensitive.json</code> on your local machine.
            Please review and reply with instructions.</p>
            <p style="color:#8b949e;font-size:12px;">This message was sent by the CIT Director (Claude) and has not been seen by any other agent.</p>
            """
            severity_label = "URGENT" if any(s.get("severity") == "CRITICAL" for s in urgent) else "HIGH"
            send_alert_email(
                subject=f"FROM CLAUDE | {severity_label} | CIT Sensitive Finding — Chairman Input Required",
                body=body,
            )
        except Exception as e:
            logger.error(f"  [Sensitive] Could not send Chairman email: {e}")


# =============================================================================
# BOARD VOTE INTEGRATION
# =============================================================================

def get_pending_proposals() -> list:
    """Load all pending CIT proposals awaiting board vote."""
    return _load_json_list_safe(CIT_PENDING)


def clear_voted_proposals(voted_ids: list, approved_ids: list):
    """
    Called by board_meeting.py after voting.
    Moves approved proposals to cit_approved_proposals.json.
    Removes all voted proposals from cit_pending_proposals.json.
    """
    pending  = _load_json_list_safe(CIT_PENDING)
    approved = _load_json_list_safe(CIT_APPROVED)

    newly_approved = [p for p in pending if p.get("id") in approved_ids]
    remaining      = [p for p in pending if p.get("id") not in voted_ids]

    # Strip re-vote housekeeping flags before archiving — they're process state, not history
    for p in newly_approved:
        p.pop("needs_revote", None)
        p.pop("revote_reason", None)

    approved.extend(newly_approved)

    with open(CIT_PENDING, "w") as f:
        json.dump(remaining, f, indent=2, default=str)
    with open(CIT_APPROVED, "w") as f:
        json.dump(approved, f, indent=2, default=str)

    return {"approved": len(newly_approved), "remaining": len(remaining)}


# =============================================================================
# ENTRY POINT
# =============================================================================

def run_cit(session: str = "auto") -> dict:
    """Run the full CIT session. Called by main.py or directly via cron."""
    director = CITDirector()
    return director.run(session=session)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Apex Capital — Continuous Improvement Team")
    parser.add_argument("--session", choices=["morning", "evening", "auto"], default="auto",
                        help="Which session to run (default: auto-detect from time)")
    parser.add_argument("--list-pending", action="store_true",
                        help="List pending board proposals without running analysis")
    args = parser.parse_args()

    if args.list_pending:
        proposals = get_pending_proposals()
        print(f"\n  {len(proposals)} pending proposal(s) awaiting board vote:\n")
        for p in proposals:
            print(f"  [{p.get('severity','?'):8s}] [{p.get('category','?'):15s}] "
                  f"conf={p.get('confidence','?')}% | {p.get('title','')}")
        print()
    else:
        run_cit(session=args.session)
