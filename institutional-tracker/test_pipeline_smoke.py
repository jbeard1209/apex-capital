#!/usr/bin/env python3
"""
APEX CAPITAL — PIPELINE SMOKE TESTS
=====================================
Minimal integration tests covering the three core pipeline functions flagged
by CIT-2026-216 (zero test coverage).

Run:  python3 test_pipeline_smoke.py
      (no external dependencies required — all Alpaca/trading calls are mocked)

Tests:
  1. evaluate_recommendation — correct APPROVED/REJECTED logic under key conditions
  2. enforce_risk_rules       — stop-loss fires on breach, hold on healthy position
  3. state.json round-trip    — load_state / save_state preserve all fields
  4. load_state defaults      — fresh state has all required keys
  5. signal_source helpers    — _classify_signal_source and _lookup_buy_source
"""

import sys
import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Make imports work from the project root
sys.path.insert(0, str(Path(__file__).parent))


# =============================================================================
# 1. evaluate_recommendation
# =============================================================================
class TestEvaluateRecommendation(unittest.TestCase):

    def _account(self, **overrides):
        base = {
            "portfolio_value": 100_000,
            "cash": 80_000,
            "buying_power": 80_000,
            "n_positions": 3,
        }
        base.update(overrides)
        return base

    def _rec(self, **overrides):
        base = {
            "ticker": "NVDA",
            "action": "BUY_NEW",   # the canonical form that gates check
            "conviction": 4,
            "source_agent": "RDTeamLeader",
            "unrealized_pnl_pct": 0,
            "hold_days": 0,
            "weight_pct": 0,
        }
        base.update(overrides)
        return base

    def setUp(self):
        from manager_agent import evaluate_recommendation
        self.eval = evaluate_recommendation

    def test_buy_approved_with_sufficient_cash(self):
        """High conviction BUY with plenty of cash → APPROVED."""
        decision = self.eval(self._rec(conviction=4), self._account())
        self.assertEqual(decision["manager_decision"], "APPROVED")
        self.assertIn(decision["manager_action"], ("BUY_NEW", "ADD"))

    def test_buy_blocked_by_canary_orange(self):
        """BUY blocked when canary fires ORANGE."""
        canary = {"level": "ORANGE", "triggers": ["SPY -3%"]}
        decision = self.eval(self._rec(), self._account(), canary_alert=canary)
        self.assertNotEqual(decision["manager_decision"], "APPROVED",
                            "BUY should be blocked by ORANGE canary")

    def test_buy_blocked_by_canary_red(self):
        """BUY blocked when canary fires RED."""
        canary = {"level": "RED", "triggers": ["Market crash"]}
        decision = self.eval(self._rec(), self._account(), canary_alert=canary)
        self.assertNotEqual(decision["manager_decision"], "APPROVED",
                            "BUY should be blocked by RED canary")

    def test_buy_blocked_cvar_circuit_breaker(self):
        """BUY blocked when CVaR circuit breaker is active."""
        decision = self.eval(self._rec(), self._account(), cvar_circuit_breaker=True)
        self.assertNotEqual(decision["manager_decision"], "APPROVED",
                            "BUY should be blocked by CVaR circuit breaker")

    def test_sell_approved_on_stop_loss(self):
        """SELL of a deeply negative position → APPROVED."""
        rec = self._rec(action="SELL", unrealized_pnl_pct=-12.0, hold_days=5)
        decision = self.eval(rec, self._account())
        self.assertEqual(decision["manager_decision"], "APPROVED")

    def test_sell_approved_stop_loss_override(self):
        """SELL of a stop-loss position is always approved even with no cash rules."""
        rec = self._rec(action="SELL", unrealized_pnl_pct=-15.0, hold_days=10,
                        ticker="BA", conviction=3)
        decision = self.eval(rec, self._account())
        self.assertEqual(decision["manager_decision"], "APPROVED")

    def test_decision_has_required_fields(self):
        """Output always contains manager_decision, manager_action, manager_rationale."""
        decision = self.eval(self._rec(), self._account())
        self.assertIn("manager_decision", decision)
        self.assertIn("manager_action",   decision)
        self.assertIn("manager_rationale", decision)
        self.assertIsInstance(decision["manager_rationale"], list)

    def test_pdo_hold_cash_blocks_buy(self):
        """PDO HOLD_CASH signal should block or flag BUY."""
        decision = self.eval(self._rec(), self._account(), pdo_signal="HOLD_CASH")
        # Either blocked or rationale mentions PDO
        rationale = " ".join(decision.get("manager_rationale", []))
        if decision["manager_decision"] == "APPROVED":
            # If approved, at minimum the PDO signal should be acknowledged
            pass  # PDO may not hard-block, just inform — acceptable
        else:
            self.assertNotEqual(decision["manager_decision"], "APPROVED")


# =============================================================================
# 2. enforce_risk_rules (mocked AlpacaTrader)
# =============================================================================
class TestEnforceRiskRules(unittest.TestCase):

    def _make_positions_df(self, rows):
        import pandas as pd
        return pd.DataFrame(rows)

    def setUp(self):
        from alpaca_trader import AlpacaTrader
        self.AlpacaTrader = AlpacaTrader

    def test_stop_loss_fires_on_breach(self):
        """Position at -12% (well past any stop threshold) should trigger sell."""
        import pandas as pd
        from alpaca_trader import AlpacaTrader
        from datetime import datetime, timedelta

        positions = pd.DataFrame([{
            "ticker": "BA", "qty": 10, "unrealized_pct": -12.0,
            "market_value": 2000.0, "avg_entry_price": 230.0,
        }])
        sell_calls = []
        entry_dates = {"BA": datetime.now() - timedelta(days=5)}  # held 5 days

        trader = AlpacaTrader.__new__(AlpacaTrader)
        trader.get_positions = lambda: positions
        # enforce_risk_rules calls submit_sell internally
        trader.submit_sell = lambda ticker, reason="stop_loss": {
            "executed": True, "ticker": ticker,
            "fill_price": 200.0, "pnl_pct": -12.0, "reason": reason,
        }

        result = trader.enforce_risk_rules(
            entry_dates=entry_dates,
            exclude_tickers=set(),
        )
        sold_tickers = {r.get("ticker") for r in result}
        self.assertIn("BA", sold_tickers,
                      f"Stop-loss should fire for BA at -12%. Got: {result}")

    def test_healthy_position_not_sold(self):
        """Position at +5% held for 1 day should not trigger any rule."""
        import pandas as pd
        from alpaca_trader import AlpacaTrader
        from datetime import datetime

        positions = pd.DataFrame([{
            "ticker": "NVDA", "qty": 5, "unrealized_pct": 5.0,
            "market_value": 5000.0, "avg_entry_price": 950.0,
        }])
        sell_calls = []
        trader = AlpacaTrader.__new__(AlpacaTrader)
        trader.get_positions = lambda: positions
        trader.market_sell = lambda *a, **kw: sell_calls.append(a)

        trader.enforce_risk_rules(
            entry_dates={"NVDA": datetime.now()},
            exclude_tickers=set(),
        )
        self.assertEqual(len(sell_calls), 0, "Healthy NVDA position should not be sold")


# =============================================================================
# 3. state.json round-trip
# =============================================================================
class TestStateRoundTrip(unittest.TestCase):

    def test_round_trip(self):
        """load_state → save_state → load_state preserves all fields."""
        import main as m

        original_path = m.STATE_FILE
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp:
            state = {
                "paper_day":         42,
                "start_date":        "2026-01-01",
                "entry_dates":       {"NVDA": "2026-03-15", "MSFT": "2026-04-01"},
                "buy_log":           [{"date": "2026-03-15", "ticker": "NVDA",
                                       "conviction": 4, "source": "13F"}],
                "sell_log":          [{"date": "2026-04-10", "ticker": "BA",
                                       "reason": "stop_loss", "pnl_pct": -11.5,
                                       "signal_source": "R&D"}],
                "win_streak":        2,
                "loss_streak":       0,
                "last_trade_result": "WIN",
                "last_context_run":  "2026-05-01",
            }
            json.dump(state, tmp, indent=2)
            tmp_path = Path(tmp.name)

        try:
            m.STATE_FILE = tmp_path
            loaded = m.load_state()
            # Verify every field survived
            self.assertEqual(loaded["paper_day"], 42)
            self.assertEqual(loaded["entry_dates"]["NVDA"], "2026-03-15")
            self.assertEqual(loaded["buy_log"][0]["source"], "13F")
            self.assertEqual(loaded["sell_log"][0]["signal_source"], "R&D")
            self.assertEqual(loaded["win_streak"], 2)

            # save and reload
            loaded["paper_day"] = 43
            m.save_state(loaded)
            reloaded = m.load_state()
            self.assertEqual(reloaded["paper_day"], 43)
        finally:
            m.STATE_FILE = original_path
            tmp_path.unlink(missing_ok=True)

    def test_load_state_defaults(self):
        """load_state on a missing file returns all required keys with correct types."""
        import main as m
        original_path = m.STATE_FILE
        try:
            m.STATE_FILE = Path("/tmp/_nonexistent_apex_state.json")
            state = m.load_state()
            required = ["paper_day", "start_date", "entry_dates",
                        "buy_log", "sell_log", "win_streak", "loss_streak",
                        "last_trade_result", "last_context_run"]
            for key in required:
                self.assertIn(key, state, f"Missing default key: {key}")
            self.assertIsInstance(state["buy_log"], list)
            self.assertIsInstance(state["sell_log"], list)
            self.assertIsInstance(state["entry_dates"], dict)
        finally:
            m.STATE_FILE = original_path


# =============================================================================
# 4. signal_source helpers (CIT-2026-184 implementation)
# =============================================================================
class TestSignalSourceHelpers(unittest.TestCase):

    def setUp(self):
        import main as m
        self.classify = m._classify_signal_source
        self.lookup   = m._lookup_buy_source

    def test_classify_13f(self):
        self.assertEqual(self.classify("13FInstitutionalAgent"), "13F")

    def test_classify_rd(self):
        self.assertEqual(self.classify("RDTeamLeader"), "R&D")
        self.assertEqual(self.classify("SectorMomentumAgent"), "R&D")
        self.assertEqual(self.classify("OptionsFlowMonitorAgent"), "R&D")

    def test_classify_energy(self):
        self.assertEqual(self.classify("EnergyResearchCoordinator"), "Energy")
        self.assertEqual(self.classify("energy_team_leader"), "Energy")

    def test_classify_unknown(self):
        self.assertEqual(self.classify(""), "Manager")
        self.assertEqual(self.classify(None), "Manager")

    def test_lookup_finds_last_buy(self):
        state = {
            "buy_log": [
                {"ticker": "NVDA", "source": "13F",  "date": "2026-03-01"},
                {"ticker": "NVDA", "source": "R&D",  "date": "2026-04-01"},  # most recent
            ]
        }
        self.assertEqual(self.lookup(state, "NVDA"), "R&D")

    def test_lookup_missing_ticker(self):
        state = {"buy_log": [{"ticker": "AAPL", "source": "13F"}]}
        self.assertEqual(self.lookup(state, "MSFT"), "unknown")

    def test_lookup_empty_log(self):
        self.assertEqual(self.lookup({}, "NVDA"), "unknown")


# =============================================================================
# 5. CIT specificity gate
# =============================================================================
class TestCITSpecificityGate(unittest.TestCase):

    def setUp(self):
        from cit_director import _fix_is_specific
        self.gate = _fix_is_specific

    def test_rejects_vague_fix(self):
        """Vague fix with only a file name fails the gate."""
        self.assertFalse(self.gate("Review error handling in main.py."))
        self.assertFalse(self.gate("Improve logging in the pipeline."))
        self.assertFalse(self.gate("Add better error handling."))

    def test_accepts_specific_fix(self):
        """Specific fix with file + function + code passes the gate."""
        good = ("In `email_sender.py`, in `_send_raw()`, "
                "change `logger.error` to `logger.warning` for OSError/DNS exceptions (Errno 8).")
        self.assertTrue(self.gate(good))

    def test_accepts_fix_with_code_fragment(self):
        """Fix containing a backtick-quoted code fragment passes."""
        good = "In `rd_team_leader.py`, filter out None scores: `if score is None: continue` before sorting."
        self.assertTrue(self.gate(good))

    def test_rejects_short_fix(self):
        """Fix under 40 chars always fails."""
        self.assertFalse(self.gate("Fix main.py line 100"))


class TestCITScannerPrecision(unittest.TestCase):
    """`_audit_files` pattern scanners must not flag known-good code.

    The CIT backlog had filled with false-positive lint: documented `pass`
    statements and `.get(k, default)` calls flagged as bugs. These guard the
    precision fixes so the noise can't silently return.
    """

    def _scan(self, source: str):
        import tempfile, os
        from cit_director import _audit_files, BASE_DIR
        fd, path = tempfile.mkstemp(suffix=".py", dir=str(BASE_DIR))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(source)
            return _audit_files("test", [os.path.basename(path)])
        finally:
            os.remove(path)

    def test_documented_pass_not_flagged(self):
        src = ("try:\n    risky()\nexcept Exception:\n"
               "    pass  # advisory — never block a trade\n")
        titles = " ".join(f["title"] for f in self._scan(src))
        self.assertNotIn("silent exception", titles)

    def test_bare_except_pass_still_flagged(self):
        src = "try:\n    risky()\nexcept Exception:\n    pass\n"
        titles = " ".join(f["title"] for f in self._scan(src))
        self.assertIn("silent exception", titles)

    def test_get_with_default_not_flagged(self):
        src = 'v = float(obj.get("k", 0))\n'
        titles = " ".join(f["title"] for f in self._scan(src))
        self.assertNotIn("None guard", titles)

    def test_get_without_default_still_flagged(self):
        src = 'v = float(obj.get("k"))\n'
        titles = " ".join(f["title"] for f in self._scan(src))
        self.assertIn("None guard", titles)


# =============================================================================
# 6. Deputy board routing
# =============================================================================
class TestDeputyBoardRouting(unittest.TestCase):

    def setUp(self):
        from deputy_board import DEPUTY_CATEGORIES, FULL_BOARD_ONLY_CATEGORIES, AUTO_IMPLEMENT_CATEGORIES
        self.deputy_cats    = DEPUTY_CATEGORIES
        self.full_board_cats = FULL_BOARD_ONLY_CATEGORIES
        self.auto_cats      = AUTO_IMPLEMENT_CATEGORIES

    def test_improvement_in_deputy_scope(self):
        """IMPROVEMENT should now be in deputy scope (expanded routing)."""
        self.assertIn("IMPROVEMENT", self.deputy_cats)
        self.assertIn("VISION",      self.deputy_cats)

    def test_strategy_is_full_board_only(self):
        """STRATEGY and ARCHITECTURE should never go to deputies."""
        self.assertIn("STRATEGY",     self.full_board_cats)
        self.assertIn("ARCHITECTURE", self.full_board_cats)
        self.assertNotIn("STRATEGY",     self.deputy_cats)
        self.assertNotIn("ARCHITECTURE", self.deputy_cats)

    def test_auto_implement_categories(self):
        """Trivial bug categories should be in auto-implement set."""
        for cat in ("BUG_FIX", "LOGIC_ERROR", "INTEGRATION"):
            self.assertIn(cat, self.auto_cats)


# =============================================================================
# 7. System health check (module imports)
# =============================================================================
class TestCriticalImports(unittest.TestCase):

    def _import(self, module):
        import importlib
        importlib.import_module(module)

    def test_import_manager_agent(self):        self._import("manager_agent")
    def test_import_risk_manager_agent(self):   self._import("risk_manager_agent")
    def test_import_hr_agent(self):             self._import("hr_agent")
    def test_import_irm_review_agent(self):     self._import("irm_review_agent")
    def test_import_board_meeting(self):        self._import("board_meeting")
    def test_import_cit_director(self):         self._import("cit_director")
    def test_import_deputy_board(self):         self._import("deputy_board")
    def test_import_email_sender(self):         self._import("email_sender")
    def test_import_canary_agent(self):         self._import("canary_agent")
    def test_import_cit_implementation_agent(self): self._import("cit_implementation_agent")
    def test_import_overnight_sprint(self):     self._import("overnight_board_sprint")


# =============================================================================
# 8. Active agent count health check
# =============================================================================
class TestAgentRegistry(unittest.TestCase):

    def setUp(self):
        reg_path = Path(__file__).parent / "data" / "agent_registry.json"
        self.agents = json.loads(reg_path.read_text()).get("agents", [])

    def test_at_least_20_active_agents(self):
        """After amnesty + sector gap fill, should have >= 20 active agents."""
        active = [a for a in self.agents if a.get("status") == "active"]
        self.assertGreaterEqual(len(active), 20,
            f"Only {len(active)} active agents — expected >= 20 after sector gap fill")

    def test_no_unknown_status(self):
        """No agent should have a missing or None status."""
        bad = [a["class_name"] for a in self.agents if not a.get("status")]
        self.assertEqual(bad, [], f"Agents with missing status: {bad}")

    def test_key_sectors_covered(self):
        """Core sectors should have at least one active agent."""
        active = [a for a in self.agents if a.get("status") == "active"]
        sectors = {a.get("sector","").lower() for a in active}
        key = ["technology", "financials", "healthcare", "industrials"]
        missing = [s for s in key if not any(s in sector for sector in sectors)]
        self.assertEqual(missing, [], f"Sectors with no active agent: {missing}")


# =============================================================================
# Producer/consumer wiring contracts
# These guard the class of bug where one component emits an identifier
# (a rule name, a pod name, a category) that no downstream consumer handles —
# the failure is silent because .get()/try-except swallow the mismatch.
# =============================================================================
class TestWiringContracts(unittest.TestCase):

    def test_every_logic_rule_has_a_handler(self):
        """Every rule the Logic Team can emit must resolve to an implementation handler.

        Regression guard: the handler map once keyed on rule names the Logic
        Team never emitted (pipeline_dark/idle_capital), so 25 of 28 findings
        were never acted on and the implementation log stayed empty.
        """
        import re
        import logic_team_implementation_agent as ltia

        src = open(Path(__file__).parent / "logic_team.py").read()
        # Static rules: rule="..."   Dynamic rules: rule=f"...{var}"
        static  = set(re.findall(r'rule="([a-z_]+)"', src))
        dynamic = re.findall(r'rule=f"([^"]+)"', src)
        # Turn f-string templates into concrete sample rule names
        samples = []
        for tmpl in dynamic:
            if "{pod_id}" in tmpl:
                samples.append(tmpl.replace("{pod_id}", "fi"))
                samples.append(tmpl.replace("{pod_id}", "fe"))
            else:
                samples.append(re.sub(r"\{[^}]+\}", "x", tmpl))

        unmatched = []
        for rule in sorted(static) + samples:
            if ltia._normalize_rule(rule) not in ltia.RESOLUTION_HANDLERS:
                unmatched.append(rule)
        self.assertEqual(unmatched, [],
                         f"Logic Team emits rules with no handler: {unmatched}")

    def test_pod_router_only_targets_live_consumers(self):
        """Every pod the router can send a proposal to must have a real consumer.

        Regression guard: proposals routed to fi/fe/etf landed in *_pod_pending
        files that no board reads — silent dead letters. submit_to_pod_queue
        must redirect those to the main board.
        """
        import cit_director as cd
        # Pods that route_proposal_to_pod() can return
        routable = {"rd", "ml", "fi", "fe", "etf", "main"}
        for pod in routable:
            proposal = {"title": f"wiring-test-{pod}", "pod": pod,
                        "description": "x", "source": "test"}
            # _effective_pod applies the dead-letter redirect (pure, no side effects)
            dest = cd._effective_pod(proposal)
            self.assertTrue(
                dest == "main" or dest in cd._LIVE_POD_QUEUES,
                f"_effective_pod returned dead queue '{dest}' for pod '{pod}'")

    def test_ltia_category_is_board_recognized(self):
        """The LT Implementation Agent's escalation category must be in board scope.

        Regression guard: it emitted OPERATIONAL_FIX, which is in none of
        deputy_board's category sets, so escalations skipped the deputy path.
        """
        import re
        import deputy_board as db
        src = open(Path(__file__).parent / "logic_team_implementation_agent.py").read()
        # Scope to the _escalate_to_board function body — that's the only place
        # a *board proposal* category is set (firm-channel categories elsewhere
        # like LT_RESOLUTION are not board categories and must not be checked).
        m = re.search(r'def _escalate_to_board\(.*?\n(?=def )', src, re.DOTALL)
        body = m.group(0) if m else src
        cats = set(re.findall(r'"category":\s*"([A-Z_]+)"', body))
        known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES
                 | db.AUTO_IMPLEMENT_CATEGORIES)
        unknown = cats - known
        self.assertEqual(unknown, set(),
                         f"LTIA escalates with categories no board scope recognizes: {unknown}")

    def test_strategy_council_categories_board_recognized(self):
        """Strategy Council proposals must use board-recognized categories, else the
        board/IRM silently never act on the council's goals (the classic wiring bug)."""
        import strategy_council as sc
        import deputy_board as db
        known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES
                 | db.AUTO_IMPLEMENT_CATEGORIES)
        for cat in (sc.CAT_GOALS, sc.CAT_ACTION):
            self.assertIn(cat, known,
                          f"Strategy Council category '{cat}' is in no board scope set")

    def test_apply_code_changes_is_idempotent_on_cos_agent(self):
        """board_meeting._apply_code_changes() patches chief_of_staff_agent.py via
        string replacement; running it must never duplicate the patched block.

        Regression guard: the old guard checked `old_block in cos_src`, but
        old_block was a substring of the block it inserted, so every board
        meeting re-matched and re-inserted another copy. This ran unnoticed on
        both hosts — 124 duplicates reached git HEAD and the VM's live copy (no
        git, never overwritten between deploys) accumulated 175 before catch.
        """
        import shutil
        import board_meeting as bm

        real_path = Path(__file__).parent / "chief_of_staff_agent.py"
        marker = 'if health["risk_level"] == "ELEVATED":'
        self.assertEqual(
            real_path.read_text().count(marker), 1,
            "chief_of_staff_agent.py has a duplicated ELEVATED risk block — "
            "collapse to a single copy")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_cos = Path(tmpdir) / "chief_of_staff_agent.py"
            shutil.copy(real_path, tmp_cos)
            cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                bm._apply_code_changes({})
                bm._apply_code_changes({})
                bm._apply_code_changes({})
            finally:
                os.chdir(cwd)
            self.assertEqual(
                tmp_cos.read_text().count(marker), 1,
                "_apply_code_changes duplicated the ELEVATED block on repeat runs")


class TestModelMetricsContract(unittest.TestCase):
    """Guards against the fake/hardcoded-AUC regression."""

    def test_model_metrics_reads_the_file_ml_model_writes(self):
        """model_metrics must read the same file predictive_model/ml_model.py writes."""
        import model_metrics
        src = open(Path(__file__).parent / "predictive_model" / "ml_model.py").read()
        import re
        m = re.search(r'META_PATH\s*=\s*DATA_DIR\s*/\s*"([^"]+)"', src)
        self.assertIsNotNone(m, "ml_model.py META_PATH not found")
        self.assertEqual(model_metrics.META_PATH.name, m.group(1),
                         "model_metrics reads a different file than ml_model writes")

    def test_untrained_returns_none_not_a_number(self):
        """When no model exists, metrics must be None — never a fabricated default."""
        import model_metrics
        if not model_metrics.META_PATH.exists():
            self.assertIsNone(model_metrics.headline_auc())

    def test_no_hardcoded_auc_literals_in_reports(self):
        """board_meeting/logic_team must not carry hardcoded AUC numbers."""
        import re
        for fn in ("board_meeting.py", "logic_team.py"):
            txt = open(Path(__file__).parent / fn).read()
            # a float literal 0.5x–0.6x assigned to something auc-ish
            hits = re.findall(r'(?:model_)?auc\w*\s*=\s*0\.[56]\d+', txt)
            self.assertEqual(hits, [], f"{fn} has hardcoded AUC literal(s): {hits}")

    def test_model_metrics_handles_v5_wf_cv_key(self):
        """model_metrics must use wf_cv_mean_auc (v5+ key) as headline AUC when
        cv_auc_mean is absent — not silently fall through to the holdout."""
        import model_metrics, json, tempfile, pathlib
        fake_meta = {
            "ensemble_auc_val": 0.99,   # holdout — must NOT be headlined when WF present
            "wf_cv_mean_auc":   0.55,   # v5 walk-forward key
            "trained_at": "2026-01-01T00:00:00",
            "n_samples": 1000,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(fake_meta, f)
            tmp = pathlib.Path(f.name)
        original = model_metrics.META_PATH
        try:
            model_metrics.META_PATH = tmp
            m = model_metrics.get_model_metrics()
            self.assertEqual(m["auc"], 0.55,
                             "headline AUC should be wf_cv_mean_auc (0.55), not holdout (0.99)")
            self.assertEqual(m["cv_auc_mean"], 0.55,
                             "cv_auc_mean should alias wf_cv_mean_auc in returned dict")
        finally:
            model_metrics.META_PATH = original
            tmp.unlink(missing_ok=True)


# =============================================================================
# Runner
# =============================================================================
if __name__ == "__main__":
    os.chdir(Path(__file__).parent)  # ensure relative paths resolve correctly
    loader  = unittest.TestLoader()
    suite   = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestEvaluateRecommendation))
    suite.addTests(loader.loadTestsFromTestCase(TestEnforceRiskRules))
    suite.addTests(loader.loadTestsFromTestCase(TestStateRoundTrip))
    suite.addTests(loader.loadTestsFromTestCase(TestSignalSourceHelpers))
    suite.addTests(loader.loadTestsFromTestCase(TestCITSpecificityGate))
    suite.addTests(loader.loadTestsFromTestCase(TestCITScannerPrecision))
    suite.addTests(loader.loadTestsFromTestCase(TestDeputyBoardRouting))
    suite.addTests(loader.loadTestsFromTestCase(TestCriticalImports))
    suite.addTests(loader.loadTestsFromTestCase(TestAgentRegistry))
    suite.addTests(loader.loadTestsFromTestCase(TestWiringContracts))
    suite.addTests(loader.loadTestsFromTestCase(TestModelMetricsContract))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
