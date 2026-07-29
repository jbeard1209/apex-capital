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
import os, sys, json, tempfile, importlib

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


def run():
    for fn in (attack_g1, attack_g2, attack_g3, attack_g4, attack_g5, attack_g6, attack_rep,
               attack_r2_identity, attack_r2_payload, attack_r2_reputation_farm,
               attack_r2_homoglyph, attack_r3_migration, attack_r4_escalation,
               attack_r5_production):
        try:
            fn()
        except Exception as e:
            findings.append(dict(id="RT-ERR", guardrail=fn.__name__, attack="harness error",
                                 severity="HIGH", expected="-", actual="-",
                                 verdict="ERROR", detail=repr(e)))
    return findings


def _report():
    run()
    inv = _weekly_investigator_lenses()
    bypasses = [f for f in findings if f["verdict"] in ("BYPASSED", "FALSE_BLOCK", "ERROR")]
    print("=" * 72)
    print("  APEX CAPITAL — SIGN-OFF LEDGER RED TEAM REPORT")
    print("=" * 72)
    print(f"  attacks run: {len(findings)}   holds: {len(findings)-len(bypasses)}   "
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
