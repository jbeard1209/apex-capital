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
# SCHEDULE
# ────────
#   Runs twice daily (6:00 AM ET + 6:00 PM ET) via cron.
#   Findings queue up in data/cit_pending_proposals.json.
#   Board reviews the queue at its next scheduled meeting.
#
# ROUTING
# ───────
#   Proposals ≥60% confidence → data/cit_pending_proposals.json (board vote)
#   Proposals  <60% confidence → data/cit_backlog.json (Director backlog)
#   Sensitive findings         → data/cit_sensitive.json (Director + Chairman only)
#   Urgent sensitive items     → email to Chairman immediately
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

# ── Confidence threshold ─────────────────────────────────────────────────────
BOARD_CONFIDENCE_THRESHOLD = 60   # proposals below this go to backlog, not board

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
    _SKIP_FILES = {"head_agent.py"}   # archived — don't audit

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
            print_lines = [
                (i, l.strip()) for i, l in enumerate(lines, 1)
                if re.match(r'^\s*print\(', l)
                and not l.strip().startswith("#")
                and "if __name__" not in src_text[max(0, src_text.find(l)-200):src_text.find(l)]
            ]
            # Only flag if outside __main__ block — crude but effective
            main_start = next(
                (i for i, l in enumerate(lines) if "__name__" in l and "__main__" in l),
                len(lines) + 1
            )
            for lineno, text in print_lines:
                if lineno < main_start:
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
                    try:
                        text = log_file.read_text(errors="replace")
                        for line in text.splitlines():
                            if " ERROR " in line:
                                error_count += 1
                                if len(error_samples) < 3:
                                    error_samples.append(line.strip()[:120])
                            elif " WARNING " in line:
                                warning_count += 1
                    except Exception:
                        pass

        if error_count > 5:
            findings.append({
                "agent":        self.name,
                "category":     "BUG_FIX",
                "severity":     "HIGH" if error_count > 20 else "MEDIUM",
                "confidence":   90,
                "title":        f"{error_count} ERROR lines in recent logs",
                "description":  f"The last 24-48h of logs contain {error_count} ERROR-level entries and {warning_count} WARNINGs. High error counts indicate systematic failures that may not be surfaced through normal agent reports.",
                "evidence":     "Recent log samples:\n" + "\n".join(f"  • {s}" for s in error_samples),
                "proposed_fix": "Review logs/run_*.log for recurring ERROR patterns. Most common errors should be caught and handled gracefully rather than logged as ERRORs.",
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
                    "description":  "buy_log and sell_log accumulate indefinitely in state.json. Large logs slow JSON parse time and increase memory usage. After 30 days of paper trading, old entries have no operational value.",
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
    Returns 'board', 'backlog', or 'sensitive'.
    - 'sensitive': finding touches confidential evaluation chains → Chairman-only
    - 'board':     confidence >= threshold → add to board vote queue
    - 'backlog':   confidence < threshold → Director backlog
    """
    text = " ".join([
        finding.get("title", ""),
        finding.get("description", ""),
        finding.get("evidence", ""),
        finding.get("proposed_fix", ""),
    ])
    if _is_sensitive(text):
        return "sensitive"
    if finding.get("confidence", 0) >= BOARD_CONFIDENCE_THRESHOLD:
        return "board"
    return "backlog"


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
        with open(CIT_HISTORY, "w") as f:
            json.dump(history, f, indent=2, default=str)

    def run(self, session: str = "auto") -> dict:
        if session == "auto":
            session = "morning" if datetime.now().hour < 14 else "evening"

        logger.info("=" * 60)
        logger.info("  APEX CAPITAL — CONTINUOUS IMPROVEMENT TEAM")
        logger.info(f"  {session.upper()} SESSION · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        logger.info("=" * 60)

        all_findings  = []
        existing_ids  = self._load_existing_ids()

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

        # ── 3. Route findings ────────────────────────────────────────────────
        board_queue = []
        backlog     = []
        sensitive   = []
        quick_wins  = []

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

        # ── 5. Main session report ───────────────────────────────────────────
        report = {
            "generated_at":  datetime.now().isoformat(),
            "session":        session,
            "total_findings": len(findings),
            "board_proposals":len(board_queue),
            "quick_wins":     len(quick_wins),
            "backlog":        len(backlog),
            "sensitive_count":len(sensitive),   # count only — no content in report
            "proposals":      board_queue,
            "quick_win_items":quick_wins,
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
        logger.info(f"  Sensitive items:         {len(sensitive)} (routed to Director only)")

        for p in board_queue:
            sev = p.get("severity","")
            cat = p.get("category","")
            logger.info(f"  → [{sev:8s}] {cat:15s} | {p.get('title','')[:60]}")

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
