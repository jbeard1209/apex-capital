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
    return out


def run():
    for fn in (attack_g1, attack_g2, attack_g3, attack_g4, attack_g5, attack_g6, attack_rep):
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
