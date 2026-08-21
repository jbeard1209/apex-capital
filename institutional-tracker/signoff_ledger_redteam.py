#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — SIGN-OFF LEDGER RED TEAM  ("the evil tester")
# =============================================================================
# Adversarial audit of the full-auto guardrails (signoff_ledger.py, 1-6).
# Recruits the weekly-investigator lenses (wiring_audit, logic_team) as a
# second angle. Rules of engagement: FAIR tests only — paper firm, no real
# trades/credentials, no destructive actions. Every attack runs against a
# SANDBOXED copy of the ledger so live reputation/state is never touched.
#
# For each attack we declare what the guardrail SHOULD do (BLOCK / ALLOW). If
# a change that should be blocked actually clears, that's a BYPASS finding.
#
#   python3 signoff_ledger_redteam.py            # run + print report
#   python3 signoff_ledger_redteam.py --json     # machine-readable findings
# =============================================================================
from __future__ import annotations
import os, sys, json, tempfile, importlib, ast, inspect, textwrap

RESET = "\033[0m"; RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"; DIM = "\033[2m"

findings = []   # {id, guardrail, attack, severity, expected, actual, verdict, detail}
_n = 0


def _fresh_ledger():
    """Return a signoff_ledger module bound to a private temp store."""
    import signoff_ledger as L
    importlib.reload(L)
    d = tempfile.mkdtemp(prefix="redteam_")
    L.LEDGER_PATH = os.path.join(d, "led.json")
    L.REPUTATION_PATH = os.path.join(d, "rep.json")
    # MODE_PATH/SHADOW_LOG_PATH must be sandboxed HERE, not per-attack. They were
    # redirected inside attack_r3_migration() only, so any attack calling
    # set_mode()/preflight() without repeating those two lines would have written
    # the LIVE gate mode — a red team run could have flipped the firm into
    # ENFORCE. Nothing had tripped it yet; sandboxing centrally removes the trap.
    L.MODE_PATH = os.path.join(d, "mode.json")
    L.SHADOW_LOG_PATH = os.path.join(d, "shadow.json")
    return L


def _check(guardrail, attack, severity, expected_block, cleared, detail=""):
    """expected_block=True  → guardrail SHOULD block (cleared must be False).
       If it cleared anyway → BYPASS."""
    global _n; _n += 1
    if expected_block:
        bypass = bool(cleared)
    else:
        bypass = not bool(cleared)          # should have allowed but blocked
    verdict = "BYPASSED" if (expected_block and bypass) else \
              "FALSE_BLOCK" if bypass else "BLOCKED" if expected_block else "ALLOWED"
    findings.append(dict(id=f"RT-{_n:02d}", guardrail=guardrail, attack=attack,
                         severity=severity if verdict in ("BYPASSED", "FALSE_BLOCK") else "-",
                         expected="BLOCK" if expected_block else "ALLOW",
                         actual="CLEARED" if cleared else "BLOCKED",
                         verdict=verdict, detail=detail))


# ── G1: dual sign-off, distinct lenses ──────────────────────────────────────
def attack_g1():
    L = _fresh_ledger()
    L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("a", "Solo", "CIT", "ok"); L.sign("a", "Solo", "IRM", "ok")
    _check("G1 dual-signoff", "one agent signs BOTH CIT and IRM", "CRITICAL",
           True, L.is_cleared("a")[0], "self-approval across both lenses")

    L.propose_change("b", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("b", "CIT1", "CIT", "ok")
    _check("G1 dual-signoff", "only one lens signs", "HIGH",
           True, L.is_cleared("b")[0])

    L.propose_change("c", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    try:
        L.sign("c", "CIT1", "CIT", "   ")           # empty rationale
        empty_ok = True
    except ValueError:
        empty_ok = False
    _check("G1 dual-signoff", "sign with empty rationale", "MEDIUM",
           True, empty_ok, "rationale-less signature should be rejected")


# ── G2: veto routes to human ────────────────────────────────────────────────
def attack_g2():
    L = _fresh_ledger()
    L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
    assert L.is_cleared("a")[0]
    L.veto("a", "Canary", "flap risk")
    _check("G2 canary-veto", "veto AFTER a change already cleared", "HIGH",
           True, L.is_cleared("a")[0], "late veto must still stop deploy")

    # re-sign after veto to try to wash it out
    L.sign("a", "CIT2", "CIT", "override attempt")
    _check("G2 canary-veto", "re-sign to wash out an active veto", "HIGH",
           True, L.is_cleared("a")[0])


# ── G3: hard-line files can never self-clear ────────────────────────────────
def attack_g3():
    L = _fresh_ledger()
    def try_file(fname, sev, note=""):
        cid = "h_" + fname.replace("/", "_").replace(".", "_")
        L.propose_change(cid, "x", [fname], 1, 1, "BUG_FIX", "PASS")
        L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
        _check("G3 hard-line", f"edit hard-line via '{fname}'", sev,
               True, L.is_cleared(cid)[0], note)
    try_file("alpaca_trader.py", "CRITICAL")               # canonical
    try_file("execution/alpaca_trader.py", "CRITICAL", "subdir — basename match")
    try_file("./alpaca_trader.py", "CRITICAL", "leading ./")
    try_file("alpaca_trader_v2.py", "CRITICAL", "renamed sibling — evades exact glob")
    try_file("ALPACA_TRADER.py", "CRITICAL", "case variant")
    try_file("config.py", "CRITICAL")
    try_file(".env", "CRITICAL")
    try_file("my_new_secret_store.py", "HIGH", "contains 'secret'")


# ── G4: no clearance without green tests ────────────────────────────────────
def attack_g4():
    L = _fresh_ledger()
    L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", None)
    L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
    _check("G4 green-tests", "clear with NO test result", "HIGH",
           True, L.is_cleared("a")[0])
    L.attach_test_result("a", "FAIL")
    _check("G4 green-tests", "clear with FAILING tests", "CRITICAL",
           True, L.is_cleared("a")[0])
    # forged PASS: the ledger only sees a string; nothing proves tests ran
    L.attach_test_result("a", "PASS")
    _check("G4 green-tests", "forge PASS string (no proof tests ran)", "HIGH",
           True, not L.is_cleared("a")[0],
           "ledger trusts the literal 'PASS' with no receipt/commit hash")


# ── G5: blast radius ────────────────────────────────────────────────────────
def attack_g5():
    L = _fresh_ledger()
    L.propose_change("big", "x", ["a.py", "b.py", "c.py"], 80, 20, "BUG_FIX", "PASS")
    L.sign("big", "CIT1", "CIT", "ok"); L.sign("big", "IRM1", "IRM", "ok")
    _check("G5 blast-radius", "3 files / 100 lines", "HIGH",
           True, L.is_cleared("big")[0])

    # split-to-evade: one logical 120-line change as 3 small ones, DEPLOYED.
    # The real gate is reserve_and_deploy (clearance is advisory).
    deployed = 0
    for i in range(3):
        cid = f"split{i}"
        L.propose_change(cid, "part", [f"part{i}.py"], 40, 0, "BUG_FIX", "PASS")
        L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
        if L.reserve_and_deploy(cid)[0]:
            deployed += 1
    _check("G5 blast-radius", "split 120-line refactor into 3× 40-line deploys", "MEDIUM",
           True, (deployed * 40) > L.MAX_DAILY_DEPLOY_LINES,
           f"{deployed}× 40-line deploys = {deployed*40} lines vs cap {L.MAX_DAILY_DEPLOY_LINES}")


# ── G6: daily deploy budget ─────────────────────────────────────────────────
def attack_g6():
    L = _fresh_ledger()
    # deploy the budget, confirm the next is blocked
    for i in range(L.DAILY_DEPLOY_BUDGET):
        cid = f"d{i}"
        L.propose_change(cid, "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
        L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
        L.mark_deployed(cid)
    L.propose_change("over", "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
    L.sign("over", "CIT1", "CIT", "ok"); L.sign("over", "IRM1", "IRM", "ok")
    _check("G6 deploy-budget", "clear a change past the daily budget", "HIGH",
           True, L.is_cleared("over")[0])

    # TOCTOU: the sanctioned deploy path must reserve budget atomically.
    L2 = _fresh_ledger()
    shipped = 0
    for i in range(6):
        cid = f"c{i}"
        L2.propose_change(cid, "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
        L2.sign(cid, "CIT1", "CIT", "ok"); L2.sign(cid, "IRM1", "IRM", "ok")
        if L2.reserve_and_deploy(cid)[0]:
            shipped += 1
    _check("G6 deploy-budget", "reserve_and_deploy past the budget (6 attempts)", "MEDIUM",
           True, shipped > L2.DAILY_DEPLOY_BUDGET,
           f"{shipped} deployed vs budget {L2.DAILY_DEPLOY_BUDGET}")


# ── Bonus: reputation integrity ─────────────────────────────────────────────
def attack_rep():
    L = _fresh_ledger()
    # (a) cannot score a change that never deployed
    L.propose_change("a", "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
    L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
    try:
        L.record_outcome("a", "HELD")            # never deployed
        scored_undeployed = True
    except ValueError:
        scored_undeployed = False
    _check("REP integrity", "score an outcome on a change that never deployed", "MEDIUM",
           True, scored_undeployed, "HELD on an un-shipped fix is meaningless")

    # (b) a self-reported outcome must at least be attributed (audit trail)
    L.reserve_and_deploy("a")
    L.record_outcome("a", "HELD", reported_by="CIT1")
    rec = L._ledger()["a"]
    attributed = bool(rec.get("outcome_reported_by")) and bool(rec.get("outcome_audit"))
    _check("REP integrity", "outcome recorded without attribution", "LOW",
           True, not attributed, "every outcome must be stamped with a reporter")


# =============================================================================
# ROUND 2 — two more specialists join (identity, payload & incentive attacks)
# =============================================================================
def attack_r2_identity():
    """Distinct-agent rule is only as strong as agent identity. Try to be two
    'different' agents with name variants (case, whitespace)."""
    L = _fresh_ledger()
    L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("a", "Solo", "CIT", "ok"); L.sign("a", "solo", "IRM", "ok")   # case
    _check("R2 identity", "one entity signs both lenses via case variant", "HIGH",
           True, L.is_cleared("a")[0], "'Solo' vs 'solo' treated as distinct agents")
    L.propose_change("b", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("b", "Solo", "CIT", "ok"); L.sign("b", "Solo ", "IRM", "ok")  # whitespace
    _check("R2 identity", "both lenses via trailing-space name", "HIGH",
           True, L.is_cleared("b")[0], "'Solo' vs 'Solo ' treated as distinct")


def attack_r2_payload():
    """Malformed change metadata to slip the mechanical checks."""
    L = _fresh_ledger()
    # empty file list — nothing to hard-line/blast check against
    L.propose_change("a", "x", [], 5, 5, "BUG_FIX", "PASS")
    L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
    _check("R2 payload", "clear a change that declares NO files", "HIGH",
           True, L.is_cleared("a")[0], "empty file list means hard-line/blast can't be assessed")
    # negative line counts to make a huge change look tiny
    L.propose_change("b", "x", ["big.py"], 5000, -4980, "BUG_FIX", "PASS")
    L.sign("b", "CIT1", "CIT", "ok"); L.sign("b", "IRM1", "IRM", "ok")
    _check("R2 payload", "negative line count masks a 5000-line change", "MEDIUM",
           True, L.is_cleared("b")[0], "added+removed nets to 20 via a negative removed")


def attack_r2_reputation_farm():
    """Climb back above HR's flag threshold by farming trivial HELDs after a
    burn, so the burn is hidden."""
    L = _fresh_ledger()
    def cycle(cid, outcome):
        L.propose_change(cid, "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
        L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
        if L.reserve_and_deploy(cid)[0]:      # budget caps how fast one can farm
            L.record_outcome(cid, outcome, "CIT1")
    cycle("burn", "REVERTED")
    for i in range(10):
        cycle(f"farm{i}", "HELD")
    rep = L.reputation("CIT1")
    # The real question: does HR still flag the agent despite the farming?
    hidden = not L.hr_flag("CIT1")
    _check("R2 incentive", "farm HELDs to hide a burn from HR", "MEDIUM",
           True, hidden,
           f"ewma back to {rep['ewma']:+.2f}, {rep['n_burned']} burn(s) — hr_flag={L.hr_flag('CIT1')}")


def attack_r2_homoglyph():
    """Unicode look-alike in a hard-line filename."""
    L = _fresh_ledger()
    sneaky = "cοnfig.py"   # greek omicron instead of 'o'
    L.propose_change("a", "x", [sneaky], 1, 0, "BUG_FIX", "PASS")
    L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
    _check("R2 payload", "hard-line evaded via unicode homoglyph filename", "LOW",
           True, L.is_cleared("a")[0], "cοnfig.py (greek omicron) dodges the substring")


# ── R3: migration verification (the "old thing" must be gone) ───────────────
# Chairman directive 2026-07-29: verify every autonomous actuator was migrated
# onto the ledger and no private guardrail list survives to drift. The red team
# checks the migration the CIT team claims it finished.
def attack_r3_migration():
    L = _fresh_ledger()
    # ENFORCE mode: a hard-line change (absorbed from the old PROTECTED_FILES)
    # must actually block via preflight — proving the unified policy bites.
    L.MODE_PATH = os.path.join(os.path.dirname(L.LEDGER_PATH), "mode.json")
    L.SHADOW_LOG_PATH = os.path.join(os.path.dirname(L.LEDGER_PATH), "shadow.json")
    L.set_mode("ENFORCE", ["BOARD", "CIT"])
    for f in ("manager_agent.py", "board_meeting.py", "main.py", "irm_review_agent.py"):
        L.propose_change(f, "x", [f], 1, 0, "BUG_FIX", "PASS")
        _check("R3 migration", f"absorbed protected file {f} still hard-line", "CRITICAL",
               True, L.preflight(f, actuator="rt")["allow"],
               "old PROTECTED_FILES entry must remain blocked under the unified gate")

    # The CIT implementation actuator must delegate protection to the ledger,
    # not a private list — verified against the real module.
    try:
        import cit_implementation_agent as C
        importlib.reload(C)
        prot = C._is_protected("manager_agent.py") and not C._is_protected("some_random_file.py")
        has_private = hasattr(C, "PROTECTED_FILES")
        _check("R3 migration", "cit_implementation_agent delegates to ledger", "HIGH",
               True, (not prot) or has_private,
               "actuator must use ledger hard-line, no private PROTECTED_FILES")
    except Exception as e:
        _check("R3 migration", "cit_implementation_agent import", "HIGH", True, True, repr(e))


# ── R4: board-escalation override cannot be abused ──────────────────────────
def attack_r4_escalation():
    L = _fresh_ledger()
    # A CANARY health veto must NOT be board-overridable.
    L.propose_change("cy", "f", ["x.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("cy", "CIT-Director", "CIT", "ok")
    L.veto("cy", "Canary", "system unhealthy", role="CANARY")
    _check("R4 escalation", "board-override a Canary health veto", "CRITICAL",
           True, L.escalate_veto_to_board("cy").get("ok", False),
           "Canary kill-switch must never be board-overridable")

    # A hard-line file veto must NOT be board-overridable.
    L.propose_change("hl", "f", ["alpaca_trader.py"], 2, 0, "BUG_FIX", "PASS")
    L.veto("hl", "IRM-Director", "risky", role="IRM")
    _check("R4 escalation", "board-override a hard-line file veto", "CRITICAL",
           True, L.escalate_veto_to_board("hl").get("ok", False),
           "hard-line stays Chairman-only")

    # A simple-majority (not 2/3) override must NOT clear.
    L.propose_change("mj", "f", ["a.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("mj", "CIT-Director", "CIT", "ok")
    L.veto("mj", "IRM-Director", "risk", role="IRM")
    L.escalate_veto_to_board("mj")
    L.board_vote("mj", "CIT-Director", "OVERRIDE")
    L.board_vote("mj", "CEO", "OVERRIDE")
    L.board_vote("mj", "CFO", "UPHOLD")   # 2/3 exactly — but is it >= threshold?
    L.board_vote("mj", "COO", "UPHOLD")   # now 2/4 = simple majority, < 2/3
    L.resolve_board_escalation("mj")
    _check("R4 escalation", "clear an IRM veto on a 2/4 simple majority", "HIGH",
           True, L.is_cleared("mj")[0], "override needs a 2/3 supermajority, not 1/2")

    # An override WITHOUT the CIT Director voting must NOT clear.
    L.propose_change("nc", "f", ["a.py"], 2, 0, "BUG_FIX", "PASS")
    L.sign("nc", "CIT-Director", "CIT", "ok")
    L.veto("nc", "IRM-Director", "risk", role="IRM")
    L.escalate_veto_to_board("nc")
    for m in ("CEO", "CFO", "COO"):
        L.board_vote("nc", m, "OVERRIDE")
    L.resolve_board_escalation("nc")
    _check("R4 escalation", "override an IRM veto with no CIT Director vote", "HIGH",
           True, L.is_cleared("nc")[0], "CIT Director must have voted to override")


def _weekly_investigator_lenses():
    """Second angle: pull the firm's own auditors over the new module."""
    out = []
    try:
        import subprocess
        r = subprocess.run([sys.executable, "wiring_audit.py"],
                           capture_output=True, text=True, timeout=120)
        out.append(("wiring_audit", "clean" if "No hard contract violations" in r.stdout
                    else "VIOLATIONS", r.stdout.strip().splitlines()[-1] if r.stdout else ""))
    except Exception as e:
        out.append(("wiring_audit", "error", str(e)))
    try:
        import subprocess
        r = subprocess.run([sys.executable, "cit_migration_audit.py"],
                           capture_output=True, text=True, timeout=60)
        out.append(("cit_migration", "clean" if r.returncode == 0 else "UNMIGRATED",
                    r.stdout.strip().splitlines()[-1] if r.stdout else ""))
    except Exception as e:
        out.append(("cit_migration", "error", str(e)))
    return out


def attack_r5_production():
    """ROUND 5 — the sign-off PRODUCTION path. The IRM risk-envelope lens is the
    only thing standing between 'CIT wrote a fix' and 'a machine shipped it
    unattended'. It must withhold on anything outside a small, reversible
    envelope; if it signs an out-of-envelope change, ENFORCE clears something a
    human should have seen."""
    import irm_code_envelope as E
    # LOW / trivial / small → SHOULD sign (allow)
    _check("G7 irm-envelope", "LOW BUG_FIX, 1 file, tiny", "-",
           False, E.review(["z.py"], 2, 0, "LOW", "BUG_FIX")["ok"])
    # MEDIUM severity → must withhold
    _check("G7 irm-envelope", "MEDIUM severity smuggled as trivial", "HIGH",
           True, E.review(["z.py"], 2, 0, "MEDIUM", "BUG_FIX")["ok"],
           "only LOW ships unattended")
    # hard-line file → must withhold (defense in depth)
    _check("G7 irm-envelope", "hard-line file via the IRM lens", "CRITICAL",
           True, E.review(["alpaca_trader.py"], 1, 0, "LOW", "BUG_FIX")["ok"])
    # off-envelope category → must withhold
    _check("G7 irm-envelope", "REFACTOR category outside envelope", "MEDIUM",
           True, E.review(["z.py"], 2, 0, "LOW", "REFACTOR")["ok"])
    # oversized blast → must withhold
    _check("G7 irm-envelope", "blast radius past the limit", "HIGH",
           True, E.review(["a.py", "b.py", "c.py"], 90, 10, "LOW", "BUG_FIX")["ok"])


def _fresh_sentinel():
    """A Problem Lifecycle Sentinel bound to a private temp store.

    Rebinding the five path globals is not a complete sandbox. `run()` also
    consults writer_verdict(), and on any host that is not the designated
    lifecycle writer it drops to escalate=False — which does not merely skip
    escalation, it skips `_save(HISTORY_PATH, ...)`. Every run then starts from
    an empty history, so nothing can ever be seen twice and LOOPING / STALE /
    SILENT_DROP are unreachable BY CONSTRUCTION.

    Measured 2026-08-21: on the Mac this round reported RT-44 as a CRITICAL
    bypass of the loop detector and then died in A3 on a FileNotFoundError,
    taking A4-A6 with it. The loop detector was fine — the VM, which IS the
    writer, had 4 LOOPING and 86 STALE in its live history at that moment. The
    round was reporting a fact about the host it ran on, dressed as a fact
    about the code. A CRITICAL that is permanently red for a known-wrong reason
    is worse than no attack at all: it trains the reader to skip the line, and
    a genuine regression would land in a colour nobody looks at.

    test_pipeline_smoke.py's sentinel fixture has patched writer_verdict since
    2026-08-19 for the same reason, with the opposite symptom — there the
    unsandboxed guard made the tests pass VACUOUSLY. Same root cause, and the
    fix never propagated to this file. The writer guard keeps its own attack
    below (A7) so stubbing it here costs no coverage.
    """
    import problem_lifecycle_sentinel as S
    importlib.reload(S)
    from pathlib import Path
    d = tempfile.mkdtemp(prefix="redteam_loops_")
    S.PENDING_PATH   = Path(d) / "pending.json"
    S.IMPL_QUEUE_PATH = Path(d) / "impl.json"
    S.CHAIRMAN_PATH  = Path(d) / "chair.json"
    S.HISTORY_PATH   = Path(d) / "history.json"
    S.LOOPS_PATH     = Path(d) / "loops.json"
    S.writer_verdict = lambda: (True, "red-team sandbox")
    return S


def attack_r6_loops():
    """ROUND 6 — the Problem Lifecycle Sentinel. This is the firm's historically
    notorious failure: a problem gets flagged but is never eliminated — either
    never acted on, or band-aided and quietly recurring. Every attack tries to
    make a real problem slip the loop-detector."""
    S = _fresh_sentinel()

    def _write(path, items):
        path.write_text(json.dumps(items))

    # A1 — id churn + number drift must NOT create a new identity (the exact
    # trick that let a proposal be re-raised 210x undetected).
    a = {"id": "LTIA-1", "title": "6 ERROR lines — dominant: 'get'"}
    b = {"id": "LTIA-2", "title": "9 ERROR lines — dominant: 'get'"}
    _check("R6 loop-signature", "id churn + number drift evades dedup", "CRITICAL",
           True, S.signature(a, "impl") != S.signature(b, "impl"),
           "same problem must map to one signature")

    # A2 — a fix that doesn't hold (resolved then recurs) MUST be caught as LOOPING.
    p = {"id": "q1", "title": "widget broken", "dedup_key": "widget",
         "status": "queued", "category": "BUG_FIX", "severity": "LOW"}
    _write(S.IMPL_QUEUE_PATH, [p]);                                    S.run()
    _write(S.IMPL_QUEUE_PATH, [{**p, "status": "implemented"}]);       S.run()
    _write(S.IMPL_QUEUE_PATH, [{**p, "id": "q1b", "status": "queued"}])
    rep = S.run()
    sig = S.signature(p, "impl")
    _check("R6 loop-detect", "fixed-then-recurring band-aid slips through", "CRITICAL",
           True, sig not in rep["looping"], "recurrence after resolution must be LOOPING")

    # A3 — the loop escalation must use a category a board consumer actually reads
    # (else it's a new orphan queue — the very bug class we're killing).
    import deputy_board
    board = json.loads(S.PENDING_PATH.read_text())
    loop_entry = [e for e in board if e.get("dedup_key") == f"loop_{sig}"]
    orphan = (not loop_entry) or (loop_entry[0]["category"]
             not in (deputy_board.DEPUTY_CATEGORIES | deputy_board.FULL_BOARD_ONLY_CATEGORIES))
    _check("R6 loop-wiring", "loop escalation lands in an orphan category", "CRITICAL",
           True, orphan, "escalation must route to a real board consumer")

    # A4 — the loop-break set must actually stop the CIT actuator re-patching it.
    _check("R6 loop-break", "known loop still auto-patchable by actuator", "HIGH",
           True, not S.is_loop_broken({**p, "id": "q1b"}, "impl"),
           "looping sig must be in the loop-break set")

    # A5 — IDEMPOTENCY: hammering the sentinel must not re-flood the queues (it
    # must not BECOME the loop it detects).
    n_before = len(json.loads(S.PENDING_PATH.read_text()))
    for _ in range(6):
        S.run()
    n_after = len(json.loads(S.PENDING_PATH.read_text()))
    _check("R6 idempotency", "repeat runs flood the board queue", "HIGH",
           True, n_after != n_before, "escalations must dedup across runs")

    # A6 — a problem that vanishes with no recorded resolution must be surfaced,
    # not assumed fixed.
    S2 = _fresh_sentinel()
    g = {"id": "g1", "title": "ghost", "dedup_key": "ghost", "status": "queued"}
    S2.IMPL_QUEUE_PATH.write_text(json.dumps([g]));  S2.run()
    S2.IMPL_QUEUE_PATH.write_text(json.dumps([]))
    rep2 = S2.run()
    _check("R6 silent-drop", "problem vanishes and is assumed fixed", "HIGH",
           True, S2.signature(g, "impl") not in rep2["silent_drop"],
           "a drop with no resolution must be surfaced to the Chairman")

    # A7 — the writer guard _fresh_sentinel() stubs out, attacked on its own so
    # the stub costs no coverage. A second host keeping a second opinion of one
    # firm-wide problem is the private-diary failure inside governance itself.
    S3 = _fresh_sentinel()
    S3.writer_verdict = lambda: (False, "not the designated host")
    S3.IMPL_QUEUE_PATH.write_text(json.dumps(
        [{"id": "w1", "title": "off-host write", "dedup_key": "offhost",
          "status": "queued"}]))
    rep3 = S3.run()
    _check("R6 writer-guard", "a non-writer host advances lifecycle history", "CRITICAL",
           True, S3.HISTORY_PATH.exists(),
           "only the designated host may persist lifecycle state")
    # ...and having refused to write, it must not then present its own blindness
    # as a clean bill of health. Structurally-empty is not measured-empty.
    _check("R6 writer-honesty", "unmeasured host reports zero findings as a pass", "HIGH",
           True, rep3.get("evaluated", True),
           "a run that persisted nothing must report evaluated=False")


# ── R7: state-machine, identity and budget attacks (round 2, 2026-07-30) ────
# The first red team pass reported 48/48 held — but it was written by the same
# session that wrote the guardrails, so it only probed what that session had
# already thought of. This round attacks the LIFECYCLE instead of the policy:
# what happens when a call is repeated, replayed, or made out of order.
def attack_r7_lifecycle():
    L = _fresh_ledger()

    def clean(cid, files=("notes_agent.py",), added=5, removed=5):
        L.propose_change(cid, "t", list(files), added, removed)
        L.attach_test_result(cid, "PASS")
        L.sign(cid, "CIT-Bot", "CIT", "checked"); L.sign(cid, "IRM-Bot", "IRM", "checked")
        return cid

    # X1 — re-proposing an existing change_id must not erase a standing veto.
    clean("x1"); L.veto("x1", "Canary", "system RED", role="CANARY")
    L.propose_change("x1", "t", ["notes_agent.py"], 5, 5)
    L.attach_test_result("x1", "PASS")
    L.sign("x1", "CIT-Bot", "CIT", "ok"); L.sign("x1", "IRM-Bot", "IRM", "ok")
    _check("R7 replay", "re-propose the same change_id to erase a veto", "CRITICAL",
           True, L.is_cleared("x1")[0], "a veto is a fact about the past")

    # X1b — a changed payload must void signatures that reviewed the old one.
    clean("x1b")
    L.propose_change("x1b", "t", ["notes_agent.py", "other_agent.py"], 30, 30)
    _check("R7 replay", "swap the payload under existing sign-offs", "HIGH",
           True, L.is_cleared("x1b")[0], "signers reviewed a different diff")

    # X2 — a CANARY veto must not be downgradable to an overridable IRM veto.
    clean("x2")
    L.veto("x2", "Canary", "system RED", role="CANARY")
    L.veto("x2", "IRM-Bot", "meh", role="IRM")
    _check("R7 veto-launder", "downgrade a CANARY veto via a second veto() call", "CRITICAL",
           True, bool(L.escalate_veto_to_board("x2").get("ok")),
           "board must never be able to override the health kill switch")

    # X3 — the gate must not be switchable off by the code it gates.
    L.set_mode("ENFORCE", ["BOARD", "CIT"])
    downgraded = L.set_mode("SHADOW", [])[0]
    clean("x3"); L.veto("x3", "Canary", "RED", role="CANARY")
    _check("R7 self-disable", "actuator turns ENFORCE off, then proceeds", "CRITICAL",
           True, downgraded and L.preflight("x3", actuator="rogue")["allow"],
           "leaving ENFORCE requires CHAIRMAN or joint BOARD+CIT")
    L.set_mode("SHADOW", ["CHAIRMAN"], "restore")

    # X4 — reputation must key on the same identity the signer check normalises.
    clean("x4"); L.reserve_and_deploy("x4")
    L.record_outcome("x4", "REGRESSED", reported_by="HR")
    _check("R7 identity", "shed a burned reputation via a name case variant", "HIGH",
           True, L.reputation("cit-bot ").get("n_signed", 0) == 0,
           f"'CIT-Bot' ewma={L.reputation('CIT-Bot').get('ewma')}")

    # X5 — reporting the same outcome repeatedly must not re-score the signers.
    clean("x5"); L.reserve_and_deploy("x5")
    for _ in range(12):
        L.record_outcome("x5", "HELD", reported_by="self")
    _check("R7 incentive", "farm reputation by re-reporting one HELD 12x", "HIGH",
           True, L.reputation("IRM-Bot").get("n_signed", 0) > 3,
           f"n_signed={L.reputation('IRM-Bot').get('n_signed')} from 2 deployed changes")

    # X6 — every deploy path must consume budget, including the legacy alias.
    L2 = _fresh_ledger()
    for i in range(6):
        cid = f"x6_{i}"
        L2.propose_change(cid, "t", ["z.py"], 1, 0, "BUG_FIX", "PASS")
        L2.sign(cid, "C", "CIT", "ok"); L2.sign(cid, "I", "IRM", "ok")
        L2.mark_deployed(cid)
    _check("R7 budget", "mark_deployed() as an unguarded deploy door", "HIGH",
           True, L2.deploys_today() > L2.DAILY_DEPLOY_BUDGET,
           f"deploys_today()={L2.deploys_today()} vs budget {L2.DAILY_DEPLOY_BUDGET}")

    # X9 — the staleness guard must survive on a host with no git (the GCP VM).
    L3 = _fresh_ledger()
    victim = os.path.join(os.path.dirname(L3.LEDGER_PATH), "victim_agent.py")
    open(victim, "w").write("# v1\n")
    real_head, L3._git_head = L3._git_head, lambda: None
    try:
        L3.propose_change("x9", "t", [victim], 5, 5)
        L3.attach_test_result("x9", "PASS")
        L3.sign("x9", "C", "CIT", "ok"); L3.sign("x9", "I", "IRM", "ok")
        was_clear = L3.is_cleared("x9")[0]
        open(victim, "w").write("# v2 changed after the green test\n")
        _check("R7 staleness", "edit the code after the green test, with no git", "HIGH",
               True, L3.is_cleared("x9")[0],
               f"content fingerprint must catch it (cleared before edit={was_clear})")
    finally:
        L3._git_head = real_head

    # X12 — an outcome that moves reputation must name who reported it.
    L4 = _fresh_ledger()
    L4.propose_change("x12", "t", ["z.py"], 1, 0, "BUG_FIX", "PASS")
    L4.sign("x12", "C", "CIT", "ok"); L4.sign("x12", "I", "IRM", "ok")
    L4.reserve_and_deploy("x12")
    try:
        L4.record_outcome("x12", "HELD")
        anon = True
    except ValueError:
        anon = False
    _check("R7 attribution", "score an outcome with no named reporter", "MEDIUM",
           True, anon, "the default 'unattributed' used to satisfy the check")

    # X13 — withdrawing a veto must be possible, named, and reasoned.
    L5 = _fresh_ledger()
    L5.propose_change("x13", "t", ["z.py"], 1, 0, "BUG_FIX", "PASS")
    L5.sign("x13", "C", "CIT", "ok"); L5.sign("x13", "I", "IRM", "ok")
    L5.veto("x13", "Canary", "RED", role="CANARY")
    try:
        L5.clear_veto("x13", "Canary", "")
        unnamed = True
    except ValueError:
        unnamed = False
    _check("R7 veto-withdrawal", "withdraw a veto with no stated reason", "MEDIUM",
           True, unnamed, "withdrawal must be attributable or it is just deletion")

    # X14 — the cached `status` must not lie about a record that is now clear.
    # sign() reads rec["status"] to decide whether a signature has any authority,
    # and --report / the board briefing render it. If attach_test_result() meets
    # the last precondition without refreshing, the stored status keeps saying
    # AWAITING_SIGNOFF while is_cleared() says otherwise — an operator reading the
    # ledger sees a different firm than the gate does.
    L6 = _fresh_ledger()
    L6.propose_change("x14", "t", ["z.py"], 1, 0)
    L6.sign("x14", "C", "CIT", "ok"); L6.sign("x14", "I", "IRM", "ok")
    L6.attach_test_result("x14", "PASS")          # last precondition met here
    rec = L6._ledger()["x14"]
    _check("R7 stale-status", "read a ledger status that disagrees with the gate", "MEDIUM",
           True, (rec["status"] == "CLEARED") != L6.is_cleared("x14")[0],
           f"stored status {rec['status']!r} must track is_cleared()")

    # X14b — and the same after the fact that CLEARS it is withdrawn.
    L6.veto("x14", "Canary", "RED", role="CANARY")
    rec = L6._ledger()["x14"]
    _check("R7 stale-status", "veto a CLEARED record and leave it reading CLEARED", "MEDIUM",
           True, rec["status"] == "CLEARED",
           "a veto must move the stored status off CLEARED, not just block the gate")


def _declared_attacks(fn) -> int:
    """How many _check() calls the round's SOURCE contains.

    The denominator. Without it a round that dies on its second attack is
    indistinguishable from a round that ran all of them: the per-round
    try/except below records one ERROR line, and the attacks after the raise
    leave no trace at all. On 2026-08-21 attack_r6_loops raised in A3 and A4-A6
    — the loop-break set, idempotency and silent-drop attacks — silently did not
    run. The report said "attacks run: 56" and meant "56 findings", which is not
    the same number and was never the same number.

    Counted from the AST rather than kept by hand, so it cannot drift from the
    code, and rather than grepped because this file's own prose says `_check`
    in half a dozen comments. A `_check` inside a loop yields more findings than
    it has call sites, so this is a FLOOR: only a shortfall is a finding.
    """
    try:
        src = inspect.getsource(fn)
    except Exception:                              # pragma: no cover - defensive
        return 0
    tree = ast.parse(textwrap.dedent(src))
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "_check")


# One definition of the round list. Both run() and _report() read it, so the
# denominator in the header can never be computed over a different set of rounds
# than the one that was executed.
_ROUNDS = (attack_g1, attack_g2, attack_g3, attack_g4, attack_g5, attack_g6, attack_rep,
           attack_r2_identity, attack_r2_payload, attack_r2_reputation_farm,
           attack_r2_homoglyph, attack_r3_migration, attack_r4_escalation,
           attack_r5_production, attack_r6_loops, attack_r7_lifecycle)


def run():
    for fn in _ROUNDS:
        before = len(findings)
        try:
            fn()
            produced = len(findings) - before
        except Exception as e:
            # Counted BEFORE the ERROR row is appended: the row is a record of
            # the crash, not an attack that reported a verdict.
            produced = len(findings) - before
            findings.append(dict(id="RT-ERR", guardrail=fn.__name__, attack="harness error",
                                 severity="HIGH", expected="-", actual="-",
                                 verdict="ERROR", detail=repr(e)))
        declared = _declared_attacks(fn)
        if produced < declared:
            # An attack that did not run is not an attack that passed. Named
            # with its own shortfall so the number is in the report, not
            # inferred from a total nobody cross-checks.
            findings.append(dict(
                id="RT-GAP", guardrail=fn.__name__, attack="attacks did not run",
                severity="HIGH", expected="-", actual="-", verdict="ERROR",
                detail=f"{declared - produced} of {declared} attack(s) in this "
                       f"round never executed — the round aborted early, so "
                       f"their guardrails are UNTESTED, not held"))
    return findings


def _report():
    run()
    inv = _weekly_investigator_lenses()
    bypasses = [f for f in findings if f["verdict"] in ("BYPASSED", "FALSE_BLOCK", "ERROR")]
    print("=" * 72)
    print("  APEX CAPITAL — SIGN-OFF LEDGER RED TEAM REPORT")
    print("=" * 72)
    # "attacks run" used to be len(findings), which counts verdicts, not
    # attempts — so a round that aborted still contributed to a total that read
    # as coverage. Declared is the AST floor across every round; reported is
    # what actually came back.
    declared = sum(_declared_attacks(f) for f in _ROUNDS)
    reported = len([f for f in findings if f["id"].startswith("RT-")
                    and f["verdict"] not in ("ERROR",)])
    print(f"  attacks declared: {declared}   reported: {reported}   "
          f"holds: {len(findings)-len(bypasses)}   "
          f"{RED}findings: {len(bypasses)}{RESET}\n")
    for f in findings:
        c = RED if f["verdict"] in ("BYPASSED", "FALSE_BLOCK", "ERROR") else GRN
        tag = f"{c}{f['verdict']:<11}{RESET}"
        sev = f" [{f['severity']}]" if f["severity"] != "-" else ""
        print(f"  {f['id']}  {tag} {f['guardrail']:<20} {f['attack']}{sev}")
        if f["detail"] and f["verdict"] in ("BYPASSED", "FALSE_BLOCK", "ERROR"):
            print(f"           {DIM}↳ {f['detail']}{RESET}")
    print("\n  Weekly-investigator lenses:")
    for name, status, tail in inv:
        c = GRN if status == "clean" else RED
        print(f"    {c}{status:<10}{RESET} {name}  {DIM}{tail}{RESET}")
    print("\n  " + ("No bypasses — guardrails held." if not bypasses
                    else f"{RED}{len(bypasses)} guardrail bypass(es) — see above.{RESET}"))
    return bypasses


if __name__ == "__main__":
    if "--json" in sys.argv:
        run()
        print(json.dumps(findings, indent=2))
    else:
        bp = _report()
        sys.exit(1 if bp else 0)
