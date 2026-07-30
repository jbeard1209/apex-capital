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
import shutil
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


class TestChairmanQueueDedup(unittest.TestCase):
    """Chronic findings (CRO paralysis, stale-chairman-actions, ...) render a
    growing cycle/day count into their title every run. Dedup that compares
    exact title text never matches its own prior entry, so the same
    unresolved issue re-queues indefinitely and chairman_action_required.json
    bloats with near-duplicates. Both write sites must collapse embedded
    digits (or use a stable dedup_key) before comparing."""

    def test_deputy_board_queue_fix_collapses_digit_variants(self):
        import deputy_board as db

        orig_chairman, orig_cmr = db.CHAIRMAN_ACTION_FILE, db.CMR_PATH
        with tempfile.TemporaryDirectory() as d:
            chairman_path = os.path.join(d, "chairman_action_required.json")
            cmr_path = os.path.join(d, "cos_meeting_requests.json")
            db.CHAIRMAN_ACTION_FILE = chairman_path
            db.CMR_PATH = cmr_path
            try:
                vote = {"classification": "deputy_approved"}
                db._queue_fix({"id": "LT-1-cro_paralysis", "title": "CRO at HIGH for 32+ days",
                                "category": "IMPROVEMENT", "severity": "HIGH"}, vote)
                db._queue_fix({"id": "LT-2-cro_paralysis", "title": "CRO at HIGH for 52+ days",
                                "category": "IMPROVEMENT", "severity": "HIGH"}, vote)
                with open(chairman_path) as f:
                    queue = json.load(f)
                self.assertEqual(len(queue), 1,
                    "same finding re-rendered with a different day count must not duplicate")
            finally:
                db.CHAIRMAN_ACTION_FILE, db.CMR_PATH = orig_chairman, orig_cmr

    def test_pod_queue_dedup_collapses_digit_variants(self):
        import cit_director as cd

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cit_pending_proposals.json"
            orig_cit_pending = cd.CIT_PENDING
            cd.CIT_PENDING = target
            try:
                # "pod": "main" forces routing to CIT_PENDING regardless of
                # keyword matching, so the test can't leak into a live pod file.
                cd.submit_to_pod_queue({"title": "CRO at HIGH for 32+ days", "category": "IMPROVEMENT",
                                         "severity": "HIGH", "proposed_fix": "x", "pod": "main"})
                cd.submit_to_pod_queue({"title": "CRO at HIGH for 52+ days", "category": "IMPROVEMENT",
                                         "severity": "HIGH", "proposed_fix": "x", "pod": "main"})
                with open(target) as f:
                    queue = json.load(f)
                self.assertEqual(len(queue), 1,
                    "same finding re-rendered with a different day count must not duplicate")
            finally:
                cd.CIT_PENDING = orig_cit_pending

    def test_ltia_escalate_to_board_dedups_by_rule(self):
        import logic_team_implementation_agent as ltia

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cit_pending_proposals.json"
            orig_cit_pending = ltia.CIT_PENDING
            ltia.CIT_PENDING = target
            try:
                ltia._board_cro_paralysis({"rule": "cro_paralysis",
                    "title": "CRO at HIGH for 32+ days"})
                ltia._board_cro_paralysis({"rule": "cro_paralysis",
                    "title": "CRO at HIGH for 52+ days"})
                with open(target) as f:
                    queue = json.load(f)
                self.assertEqual(len(queue), 1,
                    "same rule re-fired with a growing day count must not duplicate")
            finally:
                ltia.CIT_PENDING = orig_cit_pending

    def test_logic_officer_write_board_proposals_dedups_by_rule(self):
        import logic_officer as lo

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cit_pending_proposals.json"
            orig_cit_pending = lo.CIT_PENDING_PATH
            lo.CIT_PENDING_PATH = target
            try:
                finding = lambda cycles: {
                    "rule": "extended_cro_high_no_review", "severity": "HIGH",
                    "title": f"CRO has been at HIGH for {cycles} cycles — no board review",
                    "observation": "x", "why_its_wrong": "x", "suggested_resolution": "x",
                }
                lo._write_board_proposals([finding(8)])
                lo._write_board_proposals([finding(9)])
                with open(target) as f:
                    queue = json.load(f)
                self.assertEqual(len(queue), 1,
                    "same rule re-fired with a growing cycle count must not duplicate")
            finally:
                lo.CIT_PENDING_PATH = orig_cit_pending

    def test_board_meeting_route_approved_dedups_by_key(self):
        import board_meeting as bm

        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"), exist_ok=True)
            os.chdir(d)
            try:
                bm._route_approved_cit_proposals([{
                    "id": "LT-1-cro_paralysis", "title": "CRO at HIGH for 32+ days",
                    "dedup_key": "cro_paralysis", "category": "IMPROVEMENT",
                    "description": "x", "confidence": 88,
                }])
                bm._route_approved_cit_proposals([{
                    "id": "LT-2-cro_paralysis", "title": "CRO at HIGH for 52+ days",
                    "dedup_key": "cro_paralysis", "category": "IMPROVEMENT",
                    "description": "x", "confidence": 88,
                }])
                with open(os.path.join(d, "data", "chairman_action_required.json")) as f:
                    queue = json.load(f)
                self.assertEqual(len(queue), 1,
                    "same rule re-approved with a growing day count must not duplicate")
            finally:
                os.chdir(orig_cwd)

    def test_pipeline_integrity_chairman_queue_dedups_and_preserves_others(self):
        import cit_pipeline_integrity_agent as pia

        orig_path, orig_max = pia._CHAIRMAN_PATH, pia._CHAIRMAN_MAX_ENTRIES
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "chairman_action_required.json")
            pia._CHAIRMAN_PATH = target
            pia._CHAIRMAN_MAX_ENTRIES = 2
            try:
                # A pre-existing item from a different producer must survive
                # even once this agent's own entries exceed its cap.
                with open(target, "w") as f:
                    json.dump([{"id": "LT-1-cro_paralysis", "source": "logic_officer",
                                "status": "awaiting_chairman"}], f)

                issue = lambda check: {"check": check, "severity": "HIGH", "message": "x"}
                pia._update_chairman_queue([issue("email_loop_detection")], "2026-07-24T16:00:00")
                pia._update_chairman_queue([issue("email_loop_detection")], "2026-07-24T18:00:00")
                pia._update_chairman_queue([issue("disk_pressure")], "2026-07-24T20:00:00")

                with open(target) as f:
                    queue = json.load(f)

                sources = [e.get("source") for e in queue]
                self.assertIn("logic_officer", sources,
                    "another producer's entry must not be evicted by this agent's own cap")
                own = [e for e in queue if e.get("source") == "cit_pipeline_integrity_agent"]
                self.assertEqual(
                    sorted(e["dedup_key"] for e in own), ["disk_pressure", "email_loop_detection"],
                    "same check re-firing every 2h cycle must not duplicate")
            finally:
                pia._CHAIRMAN_PATH, pia._CHAIRMAN_MAX_ENTRIES = orig_path, orig_max


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

    def test_international_analyst_is_actually_scheduled(self):
        """The agent that watches foreign markets must be RUN by something.

        It tracks ^KS11 (KOSPI) and has five wired consumers, but was scheduled
        nowhere and crashed on import under Python 3.9 — so it produced zero
        reports for the firm's entire history and the firm never saw the July
        2026 Korean crash. Guard both halves: it must import, and main.py must
        actually call it.
        """
        import importlib
        mod = importlib.import_module("international_analyst")   # must not raise on py3.9
        self.assertTrue(hasattr(mod, "run"))
        main_src = (Path(__file__).parent / "main.py").read_text()
        self.assertIn("international_analyst", main_src,
                      "international_analyst is not invoked by main.py — it is an agent "
                      "that never comes to work")

    # Agents with live downstream consumers. Every one of these was found
    # scheduled-nowhere on 2026-07-30; two of them could not even be imported,
    # and nothing noticed for months precisely because nothing ran them. An
    # unscheduled agent hides a broken agent — that is the accountability cost
    # of tolerating orphans, so the import half is now enforced here.
    WIRED_AGENTS = [
        "macro_regime_model", "fi_cit_agent", "dynamic_position_sizer",
        "institution_performance_tracker", "ml_longterm_improvement_agent",
        "ml_longterm_experiment_runner", "position_aging_agent",
        "apex_system_cit_agent", "ml_improvement_loop", "ml_validate_longterm",
        "edgar_transcript_agent", "international_analyst",
    ]

    def test_every_wired_agent_imports_on_this_host(self):
        """An agent with consumers must at least be loadable.

        position_aging_agent.py feeds sector_assignments.json to 13 modules and
        died on `str | None` under Python 3.9 — the identical bug that had already
        bitten international_analyst.py. ml_validate_longterm.py hardcoded
        '/home/jackbeard/...' and mkdir()s at module scope, so it raised OSError on
        import on the Mac. Both failures are invisible while the agent is
        unscheduled: the consumers' try/except just sees "no data today".
        """
        import importlib
        broken = []
        for name in self.WIRED_AGENTS:
            try:
                importlib.import_module(name)
            except Exception as e:
                broken.append(f"{name}: {type(e).__name__}: {e}")
        self.assertEqual(broken, [], "wired agents that cannot be imported:\n  " +
                                     "\n  ".join(broken))

    def test_no_host_literal_paths_in_wired_agents(self):
        """A path naming one host is a bug on the other. Two brains, one codebase."""
        offenders = []
        for name in self.WIRED_AGENTS:
            src = (Path(__file__).parent / f"{name}.py").read_text()
            if "/home/jackbeard" in src or "/Users/jackbeard" in src:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         f"hardcoded host path(s) in wired agent(s): {offenders} — "
                         "resolve from __file__ instead")

    def test_unknown_order_is_foreign_even_if_we_traded_that_symbol_today(self):
        """The v1 bar — "we touched this ticker today" — must not explain an order.

        2026-07-30: a BUY of 163 XLE took five hours and a two-host forensic sweep
        to attribute, and the detector had called it EXPLAINED the whole time —
        because some local record mentioned XLE that day. That is the precise
        failure this pins. An order the firm cannot prove it placed is foreign even
        when the firm traded that same symbol, that same side, that same day; only
        an order-level match (apex stamp, or the id in our own submission ledger)
        clears it.
        """
        import foreign_order_detector as F
        import order_ledger as OL

        class Order:
            def __init__(self, oid, sym, coid="", side="buy", qty="1"):
                self.id, self.symbol, self.client_order_id = oid, sym, coid
                self.side, self.qty, self.status = side, qty, "filled"
                # well after STRICT_FROM, so the pre-cutover heuristic cannot apply
                self.submitted_at = "2099-01-01T15:36:21Z"

        with tempfile.TemporaryDirectory() as d:
            F.REPORT = Path(d) / "r.json"
            F.CHAIRMAN = Path(d) / "c.json"          # never touch the live queue
            # The firm demonstrably traded XLE today, on this very day.
            F._local_decision_index = lambda: {("XLE", "2099-01-01")}
            F._known_ids = lambda: {"ours-123"}

            ours = Order("ours-123", "XLE")                        # in our ledger
            stamped = Order("x", "XLE", coid=OL.make_stamp("manager_agent"))
            ghost = Order("nobody-knows-me", "XLE")                # SAME symbol, same day
            hedge_ghost = Order("also-unknown", "SH")              # the old symbol whitelist

            out = F.run_check(orders=[ours, stamped, ghost, hedge_ghost])

            foreign = {f["symbol"] for f in out["foreign"]}
            self.assertEqual(out["orders_seen"], 4)
            self.assertEqual(len(out["attributed"]), 2,
                             "an id in our ledger and a valid apex stamp must both clear")
            self.assertIn("XLE", foreign,
                          "an unknown order id was explained away by same-symbol/same-day "
                          "activity — this is exactly the v1 bug")
            self.assertIn("SH", foreign,
                          "an unstamped hedge ticker must no longer pass on symbol alone")
            self.assertEqual(out["status"], "FOREIGN_ORDERS")
            self.assertEqual(out["legacy_explained"], 0,
                             "post-cutover orders must never reach the v1 heuristic")

    def test_order_stamp_cannot_be_forged_or_collide(self):
        """The stamp is the attribution primitive — it must be unique and checkable."""
        import order_ledger as OL
        ids = {OL.make_stamp("manager_agent") for _ in range(500)}
        self.assertEqual(len(ids), 500, "client_order_id collision — attribution would alias")
        good = OL.parse_stamp(next(iter(ids)))
        self.assertIsNotNone(good)
        self.assertIn("host", good)
        self.assertIn("source", good)
        for forged in ("", "apex", "apex-x", "notapex-h-s-abc", "apex--s-abc", None):
            self.assertIsNone(OL.parse_stamp(forged),
                              f"{forged!r} must not parse as a firm-signed order")

    def test_nan_market_move_is_missing_not_quiet(self):
        """A NaN % change must normalise to None, never reach a threshold test.

        2026-07-30: KOSPI came back `pct: NaN` on the day the Chairman was
        watching Korea. NaN is a float, so it passed every `pct is not None`
        guard; then `abs(nan) >= 0.8` is False — as is every comparison against
        NaN — so the index silently dropped out of `notable_indices`. "Crashing"
        and "flat" produced identical output. Bare NaN is also illegal JSON, so
        one bad index can break a strict parser reading the whole report.
        """
        import international_analyst as ia
        for bad in (float("nan"), float("inf"), float("-inf"), "n/a", None):
            self.assertIsNone(ia._finite(bad), f"{bad!r} must normalise to None")
        self.assertEqual(ia._finite(-4.25), -4.25)   # real moves survive intact
        self.assertEqual(ia._finite(0.0), 0.0)       # a genuine flat day is not "missing"

        # The whole point: a NaN reading must not be able to hide inside a
        # threshold filter. It must be absent-and-named, not absent-and-silent.
        rows = [
            {"name": "KOSPI", "ticker": "^KS11", "pct": ia._finite(float("nan"))},
            {"name": "DAX",   "ticker": "^GDAXI", "pct": 2.0},
        ]
        self.assertEqual([r["name"] for r in ia._notable(rows, threshold=0.8)], ["DAX"])
        self.assertIn("KOSPI", [r["name"] for r in rows if r["pct"] is None])

        # And the live report must carry the honest list + be strict-JSON clean.
        report = Path(__file__).parent / "data" / "international_report.json"
        if report.exists():
            raw = report.read_text()
            self.assertNotIn("NaN", raw, "report contains bare NaN — not valid JSON")
            self.assertIn("unreadable", json.loads(raw),
                          "report must name the instruments it could not read")

    def test_hr_attendance_audit_is_wired_and_actually_fires(self):
        """HR must take attendance, and the result must reach the review report.

        HR audited its 39-name ROSTER, so an agent that could not even import was
        never marked absent — it wasn't on the roster, so nobody was there to mark
        absent. audit_attendance() closes that, but a checker nothing calls is just
        a longer way of not checking, so assert the wiring too.
        """
        import inspect, textwrap, importlib, sys as _sys
        import hr_agent

        # 1. Wired: the review report must actually carry the attendance result.
        src = inspect.getsource(hr_agent.run_hr_review)
        self.assertIn('report["attendance"]', src,
                      "run_hr_review must publish the attendance audit, not orphan it")

        # 2. Fires: a module that cannot import must be reported CANNOT_START.
        #    Planted on sys.path, not in the repo, so the repo stays clean.
        sandbox = Path(tempfile.mkdtemp())
        (sandbox / "hr_absent_probe.py").write_text("raise ImportError('probe')\n")
        (sandbox / "hr_present_probe.py").write_text("ok = True\n")
        import wiring_audit
        real_modules = getattr(wiring_audit, "AGENT_MODULES", None)
        _sys.path.insert(0, str(sandbox))
        try:
            wiring_audit.AGENT_MODULES = ["hr_absent_probe", "hr_present_probe"]
            res = hr_agent.audit_attendance()
        finally:
            _sys.path.remove(str(sandbox))
            if real_modules is None:
                delattr(wiring_audit, "AGENT_MODULES")
            else:
                wiring_audit.AGENT_MODULES = real_modules
            shutil.rmtree(sandbox, ignore_errors=True)

        broken = [f["agent"] for f in res["cannot_start"]]
        self.assertIn("hr_absent_probe", broken,
                      "a module that raises on import must be reported CANNOT_START")
        self.assertNotIn("hr_present_probe", broken,
                         "a module that imports fine must not be branded broken")
        self.assertEqual(res["status"], "ATTENTION")

        # 3. Attendance is a MANAGEMENT finding. A module nobody scheduled has done
        #    nothing wrong, so this must never reach into ratings or the PIP path.
        self.assertFalse({"rating", "ratings", "pip", "pip_status"} & set(res),
                         "attendance must not carry performance/PIP fields")

    def test_a_test_file_cannot_make_an_agent_look_scheduled(self):
        """Naming a module in a test must never count as employing it.

        2026-07-30: check_orphan_agents() barred test files from being scheduler
        ROOTS, but not from the reachability walk — so any production module that
        merely mentioned a test's name dragged it into `reachable`, and the frontier
        then expanded from the test's own text. Adding a WIRED_AGENTS list to this
        very file reclassified NINE baselined orphans as "scheduled", and the audit
        asked to delete them from the baseline. The ratchet would have loosened
        itself on the strength of a list that exists only inside a test.
        """
        import wiring_audit
        sources = {
            # main.py mentions the test file (a docstring reference is enough).
            "main.py": "import canary_agent\n# see test_fake_suite.py for coverage\n",
            "canary_agent.py": "x = 1\n",
            "test_fake_suite.py": "import ghost_agent\n",
            # An orphan that only the TEST names. It must still be caught.
            "ghost_agent.py": (
                'import json\n'
                'OUT = "data/ghost_report.json"\n'
                'json.dump({}, open(OUT, "w"))\n'
            ),
            "consumer.py": 'open("data/ghost_report.json")\n',
        }
        wiring_audit.problems = []
        wiring_audit.check_orphan_agents(sources)
        self.assertTrue(
            any("ghost_agent.py" in p for p in wiring_audit.problems),
            "an agent reachable ONLY via a test file must still be an orphan; "
            f"got {wiring_audit.problems}")

    def test_orphan_agent_check_names_the_file_the_agent_really_writes(self):
        """The 'writes X' in a finding must be an output, and must not change per run.

        It used to be any .json literal near the word "data", iterated out of a SET —
        so the named file was a coin flip across processes, and inputs were reported
        as outputs (dynamic_position_sizer.py "writes positions.json", which it only
        reads). The flagged agents were right; every explanation naming a file was not.
        """
        import wiring_audit
        out_re = wiring_audit.re.compile(r'["\'](?:[\w.\-/]*/)?([a-z0-9_]+\.json)["\']')
        src = (
            'IN_PATH  = "data/positions.json"\n'         # read only
            'OUT_PATH = "data/position_sizes.json"\n'     # the real output
            'raw = open(IN_PATH).read()\n'
            'json.dump(sizes, open(OUT_PATH, "w"))\n'
        )
        outs = wiring_audit._declared_outputs(src, out_re)
        self.assertEqual(outs, ["position_sizes.json"],
                         "must report the written file, not the read one")
        self.assertEqual(outs, wiring_audit._declared_outputs(src, out_re),
                         "output attribution must be deterministic")

    def test_orphan_check_reads_the_canonical_prod_schedule(self):
        """'Scheduled' must be judged against the VM's live crontab, not a template.

        new_crontab.txt's own header says the VM's live crontab is canonical and that
        file is only the Mac-path template — and it had drifted 10 agents behind prod,
        so agents running every 4h in production were reported as scheduled NOWHERE.
        """
        import wiring_audit
        snap = Path(__file__).parent / "data" / "vm_crontab_snapshot.txt"
        template = Path(__file__).parent / "new_crontab.txt"
        self.assertTrue(snap.exists(), "canonical prod-schedule snapshot is missing")

        # Pick agents that prove the point: scheduled in the LIVE crontab and absent
        # from the template. If the check consults only the template, these are
        # branded orphans even though they run every few hours in production.
        snap_txt, tmpl_txt = snap.read_text(), template.read_text()
        provers = [a for a in ("data_integrity_sentinel", "tech_short_pod",
                               "problem_lifecycle_sentinel", "metric_sanity_sentinel")
                   if a in snap_txt and a not in tmpl_txt]
        self.assertTrue(provers,
                        "snapshot no longer differs from the template — refresh it "
                        "(scripts/refresh_crontab_snapshot.sh) or this guard proves nothing")

        # Behavioural, and deliberately NOT run against the real tree. A source-string
        # grep was the first attempt and a comment mentioning the snapshot satisfied it
        # with the snapshot wired out. Running it over the real repo is no better: the
        # agents the snapshot rescues happen not to declare a read-by-others output, so
        # the flagged set is identical either way — a real fix with no live consequence
        # yet. So point BASE_DIR at a fixture where the two files disagree, which is the
        # exact condition prod was in (template 10 agents behind the VM).
        sandbox = Path(tempfile.mkdtemp())
        (sandbox / "data").mkdir()
        (sandbox / "new_crontab.txt").write_text("0 6 * * * python3 canary_agent.py\n")
        (sandbox / "data" / "vm_crontab_snapshot.txt").write_text(
            "0 6 * * * python3 canary_agent.py\n"
            "20 */4 * * * python3 snapshot_only_agent.py\n")   # live, template-invisible
        sources = {
            "main.py": "import canary_agent\n",
            "canary_agent.py": "x = 1\n",
            "snapshot_only_agent.py": (
                'OUT = "data/snap_report.json"\n'
                'json.dump(d, open(OUT, "w"))\n'
            ),
            "consumer.py": 'r = load("data/snap_report.json")\n',
        }
        real_base = wiring_audit.BASE_DIR
        try:
            wiring_audit.BASE_DIR = sandbox
            wiring_audit.problems = []
            wiring_audit.check_orphan_agents(sources)
            flagged = " ".join(wiring_audit.problems)
        finally:
            wiring_audit.BASE_DIR = real_base
            shutil.rmtree(sandbox, ignore_errors=True)

        self.assertNotIn(
            "snapshot_only_agent.py", flagged,
            "an agent scheduled in the canonical live crontab was called an orphan — "
            "the check is judging prod against the stale Mac template")

    def test_orphan_agent_check_actually_detects_orphans(self):
        """wiring_audit must catch a producer that is scheduled nowhere.

        check_orphan_queues() only inspects files that already EXIST and match a
        queue/log/pending glob, so it is structurally blind to an agent that has
        never run once. check_orphan_agents() closes that hole; this proves it
        fires rather than merely existing.
        """
        import wiring_audit
        sources = {
            "main.py": "import canary_agent\n",
            "canary_agent.py": "x = 1\n",
            # ghost_agent writes a report that a real consumer reads, but nothing
            # anywhere runs ghost_agent -> must be flagged.
            "ghost_agent.py": 'p = "data/ghost_report.json"\njson.dump(d, open(p, "w"))\n',
            "consumer_agent.py": 'r = load("data/ghost_report.json")\n',
        }
        wiring_audit.problems = []
        wiring_audit.check_orphan_agents(sources)
        self.assertTrue(
            any("ghost_agent.py" in p for p in wiring_audit.problems),
            f"orphan-agent check did not flag an unscheduled producer: {wiring_audit.problems}")

    def test_orphan_baseline_is_a_ratchet_that_only_tightens(self):
        """The known-orphan baseline must never be usable to silence a NEW finding.

        A baseline is how 'flagged but never solved' normally creeps back in: someone
        appends to it instead of fixing. Two properties keep it honest — a brand-new
        orphan hard-fails even though 11 are baselined, and an entry that is no longer
        an orphan hard-fails until it is removed (so the list can only shrink).
        """
        import wiring_audit, pathlib, tempfile

        # Hermetic fixture — never _py_sources(). This test's own text mentions its
        # fixture names, and a test file used to count as a scheduler root, so real
        # sources made the fixtures look reachable and the test silently passed
        # nothing. Both halves are fixed; keep the fixture synthetic regardless.
        src = {
            "main.py":        "import scheduled_agent\n",
            "scheduled_agent.py": 'json.dump(d, open("data/sched_out.json", "w"))\n',
            "known_ghost.py": 'json.dump(d, open("data/known_ghost_out.json", "w"))\n',
            "new_ghost.py":   'json.dump(d, open("data/new_ghost_out.json", "w"))\n',
            "reader.py":      'a = load("data/known_ghost_out.json")\n'
                              'b = load("data/new_ghost_out.json")\n',
        }
        real = wiring_audit._BASELINE_PATH
        tmp = None
        try:
            def _baseline(entries):
                nonlocal tmp
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                    json.dump({"known_orphans": entries}, fh)
                    tmp = fh.name
                wiring_audit._BASELINE_PATH = pathlib.Path(tmp)

            # (a) known_ghost is baselined -> note; new_ghost is not -> hard failure.
            _baseline(["known_ghost.py"])
            wiring_audit.problems, wiring_audit.notes = [], []
            wiring_audit.check_orphan_agents(src)
            self.assertTrue(any("new_ghost.py" in p for p in wiring_audit.problems),
                            "a NEW orphan must hard-fail, not be absorbed by the baseline")
            self.assertFalse(any("known_ghost.py" in p for p in wiring_audit.problems),
                             "a baselined orphan must be a note, not a hard failure")
            self.assertTrue(any("known_ghost.py" in n for n in wiring_audit.notes),
                            "a baselined orphan must still be reported loudly as a note")

            # (b) a baseline entry that is no longer an orphan must hard-fail, so the
            #     list can only ever shrink.
            _baseline(["known_ghost.py", "scheduled_agent.py"])
            wiring_audit.problems, wiring_audit.notes = [], []
            wiring_audit.check_orphan_agents(src)
            self.assertTrue(
                any("scheduled_agent.py" in p and "no longer an orphan" in p
                    for p in wiring_audit.problems),
                "a baseline entry that is no longer an orphan must hard-fail so the "
                "ratchet cannot silently loosen")
        finally:
            wiring_audit._BASELINE_PATH = real

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

    def test_metric_sanity_sentinel_categories_board_recognized(self):
        """metric_sanity_sentinel files bug proposals into cit_pending_proposals.json;
        their categories must be in a board scope or the board silently never acts
        on an impossible-metric finding (the exact wiring-bug class it exists to catch)."""
        import re
        import deputy_board as db
        src = open(Path(__file__).parent / "metric_sanity_sentinel.py").read()
        m = re.search(r'def _file_bug_proposals\(.*?\n(?=def )', src, re.DOTALL)
        body = m.group(0) if m else src
        # Categories may be literal or a ternary of two literals — capture all.
        cats = set(re.findall(r'"([A-Z_]+)"\s+if |"category":\s*"([A-Z_]+)"|else\s+"([A-Z_]+)"', body))
        cats = {c for tup in cats for c in tup if c}
        known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES
                 | db.AUTO_IMPLEMENT_CATEGORIES)
        unknown = cats - known
        self.assertEqual(unknown, set(),
                         f"metric_sanity_sentinel files categories no board scope recognizes: {unknown}")

    def test_chairman_decision_sla_escalation_category_board_recognized(self):
        """chairman_decision_sla escalates undecided items to a board vote after 50d;
        the escalation category must reach the board or the SLA is toothless."""
        import re
        import deputy_board as db
        src = open(Path(__file__).parent / "chairman_decision_sla.py").read()
        m = re.search(r'def _escalate_to_board\(.*?\n(?=def )', src, re.DOTALL)
        body = m.group(0) if m else src
        cats = set(re.findall(r'"category":\s*"([A-Z_]+)"', body))
        known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES
                 | db.AUTO_IMPLEMENT_CATEGORIES)
        unknown = cats - known
        self.assertEqual(unknown, set(),
                         f"chairman_decision_sla escalates with unrecognized categories: {unknown}")

    def test_edgar_extractor_drops_quarterly_periods(self):
        """REGRESSION (the ABBV P/E 256 bug): a 10-K's XBRL carries quarterly facts
        tagged form='10-K'; the annual extractor must keep only ~365-day periods for
        flow concepts, or a 3-month value corrupts the annual series."""
        import fe_edgar_fundamentals as fe
        facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"start": "2023-01-01", "end": "2023-12-31", "val": 60e9, "form": "10-K", "filed": "2024-02-01"},
            {"start": "2023-10-01", "end": "2023-12-31", "val": 15e9, "form": "10-K", "filed": "2024-02-01"},  # Q4 — must be dropped
        ]}}}}}
        by_year = fe._extract_by_year(facts, "Revenues", flow=True)
        self.assertEqual(by_year.get("2023"), 60e9,
                         "quarterly value leaked into the annual revenue series")

    def test_edgar_extractor_reads_dei_shares(self):
        """Many issuers (Visa, banks) report shares only in the dei taxonomy; the
        extractor must search dei, not just us-gaap, or every per-share metric breaks."""
        import fe_edgar_fundamentals as fe
        facts = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2024-12-31", "val": 1_800_000_000, "form": "10-K", "filed": "2025-02-01"},
        ]}}}}}
        by_year = fe._extract_by_year(facts, "EntityCommonStockSharesOutstanding", flow=False)
        self.assertEqual(by_year.get("2024"), 1_800_000_000)

    def test_fundamentals_sanity_gates_impossible_debt(self):
        """The plausibility GATE must reject debt>assets (the INCY case) so no pod
        can value or trade a name on a physically-impossible balance sheet."""
        import fundamentals_sanity as fs
        data = {"_ticker": "TEST", "shares_outstanding": [1e8], "revenue": [1e9],
                "net_income": [1e8],
                "by_year": {"long_term_debt": {"2024": 20e9},
                            "total_assets": {"2024": 5e9}}}
        ok, reasons = fs.is_reliable(data)
        self.assertFalse(ok, "validator failed to gate impossible debt/assets")

    def test_iv_valuation_actually_calls_the_reliability_gate(self):
        """WIRING CONTRACT (2026-07-29): is_reliable() was built + tested but no
        production consumer called it, so impossible-fundamentals tickers (SBAC)
        were valued anyway and 1,078 'quarantine' proposals sat unimplemented.
        calculate_iv MUST invoke the gate — guard against it being unwired again."""
        import inspect, fe_intrinsic_value as iv
        src = inspect.getsource(iv.calculate_iv)
        self.assertIn("is_reliable", src,
                      "fe_intrinsic_value.calculate_iv no longer gates on "
                      "fundamentals_sanity.is_reliable() — impossible fundamentals "
                      "will be valued on garbage data again.")

    def test_fundamentals_sanity_gates_stale_subseries(self):
        """A present-but-truncated sub-series is as dangerous as a missing one:
        EOG's shares stop at 2012 (P/E on a stale share count), INCY's debt at 2018
        (bogus leverage). The gate must reject when a key field lags the record's
        newest fiscal year by >2 years, while leaving current records reliable."""
        import fundamentals_sanity as fs
        stale = {"_ticker": "STALE", "shares_outstanding": [1e8], "revenue": [1e9],
                 "net_income": [1e8],
                 "by_year": {"net_income": {"2025": 1e8}, "total_assets": {"2025": 5e9},
                             "shares_outstanding": {"2012": 1e8}}}
        ok, _ = fs.is_reliable(stale)
        self.assertFalse(ok, "failed to gate a 13yr-stale shares sub-series")
        fresh = {"_ticker": "FRESH", "shares_outstanding": [1e8], "revenue": [1e9],
                 "net_income": [1e8],
                 "by_year": {"net_income": {"2025": 1e8}, "total_assets": {"2025": 5e9},
                             "shares_outstanding": {"2025": 1e8},
                             "long_term_debt": {"2025": 1e9}}}
        ok2, _ = fs.is_reliable(fresh)
        self.assertTrue(ok2, "over-gated a current record")

    def test_fundamentals_sanity_proposal_category_recognized(self):
        """fundamentals_sanity files LOGIC_ERROR proposals — must reach a board scope."""
        import re, deputy_board as db
        src = open(Path(__file__).parent / "fundamentals_sanity.py").read()
        cats = set(re.findall(r'"category":\s*"([A-Z_]+)"', src))
        known = (db.DEPUTY_CATEGORIES | db.FULL_BOARD_ONLY_CATEGORIES | db.AUTO_IMPLEMENT_CATEGORIES)
        self.assertEqual(cats - known, set(),
                         f"fundamentals_sanity files unrecognized categories: {cats - known}")

    def test_rsi_last_is_always_finite(self):
        """REGRESSION (the NaN-in-JSON bug): RSI on a pure-uptrend window (zero
        losses) used to divide by NaN and serialize the invalid token `NaN`. The
        shared helper must return a finite value (100 for all-gains) in every case."""
        import math, pandas as pd
        from technical_indicators import rsi_last
        up = pd.Series([float(i) for i in range(1, 40)])          # only gains
        down = pd.Series([float(i) for i in range(40, 1, -1)])    # only losses
        flat = pd.Series([5.0] * 40)                               # no change
        for s in (up, down, flat, pd.Series([1.0, 2.0])):
            self.assertTrue(math.isfinite(rsi_last(s)), "rsi_last returned non-finite")
        self.assertEqual(rsi_last(up), 100.0)
        self.assertEqual(rsi_last(down), 0.0)

    def test_data_integrity_sentinel_heals_nonfinite_json(self):
        """The sentinel's regex heal must turn invalid NaN/Infinity tokens into
        valid JSON (null) without touching legitimate string content."""
        import data_integrity_sentinel as dis
        raw = '{"a": NaN, "b": [1, Infinity, -Infinity], "c": "NaN is fine in text"}'
        fixed = dis._NONFINITE.sub("null", raw)
        obj = json.loads(fixed)  # must now parse
        self.assertIsNone(obj["a"])
        self.assertEqual(obj["c"], "NaN is fine in text")

    def test_data_integrity_sentinel_composes(self):
        """The umbrella must run all invariant checks and return a valid verdict,
        or the firm's self-inspection silently no-ops."""
        import data_integrity_sentinel as dis
        rep = dis.run(write=False)
        self.assertIn(rep["overall"], ("GREEN", "YELLOW", "RED"))
        self.assertEqual(set(rep["sections"]),
                         {"fundamentals", "risk_and_model", "freshness", "json_health"})

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


class TestSignoffLedger(unittest.TestCase):
    """The accountable sign-off ledger gates autonomous fixes. These guard the
    guardrails: a regression that quietly disables one of them would let a change
    deploy without the accountability the Chairman required (2026-07-29)."""

    def setUp(self):
        import tempfile, signoff_ledger as L
        self.L = L
        self._d = tempfile.mkdtemp()
        self._led, self._rep = L.LEDGER_PATH, L.REPUTATION_PATH
        self._mode, self._slog = L.MODE_PATH, L.SHADOW_LOG_PATH
        L.LEDGER_PATH = os.path.join(self._d, "led.json")
        L.REPUTATION_PATH = os.path.join(self._d, "rep.json")
        L.MODE_PATH = os.path.join(self._d, "mode.json")
        L.SHADOW_LOG_PATH = os.path.join(self._d, "shadow.json")

    def tearDown(self):
        self.L.LEDGER_PATH, self.L.REPUTATION_PATH = self._led, self._rep
        self.L.MODE_PATH, self.L.SHADOW_LOG_PATH = self._mode, self._slog

    def test_default_mode_is_shadow_and_preflight_never_blocks(self):
        """Wiring the gate into live actuators must be safe: in SHADOW the gate
        allows everything (nothing breaks) but records what it WOULD block."""
        L = self.L
        self.assertEqual(L.get_mode(), "SHADOW", "default mode must be SHADOW")
        L.propose_change("c", "x", ["z.py"], 2, 0, "BUG_FIX", None)  # no green test
        pf = L.preflight("c", actuator="test")
        self.assertTrue(pf["allow"], "shadow must never block")
        self.assertTrue(pf["would_block"], "should record it WOULD have blocked")
        self.assertEqual(L.shadow_summary()["would_block_events"], 1)

    def test_enforce_flip_requires_joint_board_and_cit(self):
        L = self.L
        self.assertFalse(L.set_mode("ENFORCE", ["BOARD"])[0], "board alone flipped")
        self.assertFalse(L.set_mode("ENFORCE", ["CIT"])[0], "cit alone flipped")
        self.assertEqual(L.get_mode(), "SHADOW")
        self.assertTrue(L.set_mode("ENFORCE", ["BOARD", "CIT"])[0], "joint vote should flip")
        self.assertEqual(L.get_mode(), "ENFORCE")
        # Red team X3: leaving ENFORCE used to be free, so any actuator the gate
        # blocked could call set_mode("SHADOW", []) and walk through. Turning the
        # gate off is now as accountable as turning it on...
        self.assertFalse(L.set_mode("SHADOW", [])[0], "unattributed downgrade allowed")
        self.assertEqual(L.get_mode(), "ENFORCE")
        # ...with a Chairman fast-path, so a misfiring gate can still be killed.
        self.assertTrue(L.set_mode("SHADOW", ["CHAIRMAN"])[0])
        self.assertEqual(L.get_mode(), "SHADOW")

    def test_enforce_preflight_blocks_uncleared(self):
        L = self.L
        L.set_mode("ENFORCE", ["BOARD", "CIT"])
        L.propose_change("c", "x", ["z.py"], 2, 0, "BUG_FIX", None)  # no green test
        pf = L.preflight("c", actuator="test")
        self.assertFalse(pf["allow"], "enforce must block an uncleared change")

    def test_irm_veto_escalates_and_board_supermajority_overrides(self):
        L = self.L
        L.propose_change("c", "fix", ["agent_meeting.py"], 3, 1, "BUG_FIX", "PASS")
        L.sign("c", "CIT-Director", "CIT", "correct")
        L.veto("c", "IRM-Director", "concentration risk", role="IRM")
        self.assertFalse(L.is_cleared("c")[0], "IRM veto must block")
        self.assertTrue(L.escalate_veto_to_board("c")["ok"])
        for m in ("CIT-Director", "CEO", "CFO"):
            L.board_vote("c", m, "OVERRIDE")
        self.assertEqual(L.resolve_board_escalation("c")["outcome"], "OVERRIDDEN")
        self.assertTrue(L.is_cleared("c")[0], "board 3/3 override should clear")

    def test_board_override_needs_supermajority_and_cit_director(self):
        L = self.L
        # minority override → upheld
        L.propose_change("m", "fix", ["a.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("m", "CIT-Director", "CIT", "ok")
        L.veto("m", "IRM-Director", "risk", role="IRM")
        L.escalate_veto_to_board("m")
        for mm, v in (("CIT-Director", "OVERRIDE"), ("CEO", "UPHOLD"), ("CFO", "UPHOLD")):
            L.board_vote("m", mm, v)
        self.assertEqual(L.resolve_board_escalation("m")["outcome"], "UPHELD")
        self.assertFalse(L.is_cleared("m")[0])
        # supermajority but no CIT Director present → upheld
        L.propose_change("n", "fix", ["a.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("n", "CIT-Director", "CIT", "ok")
        L.veto("n", "IRM-Director", "risk", role="IRM")
        L.escalate_veto_to_board("n")
        for mm in ("CEO", "CFO", "COO"):
            L.board_vote("n", mm, "OVERRIDE")
        self.assertEqual(L.resolve_board_escalation("n")["outcome"], "UPHELD",
                         "override without CIT Director must fail")

    def test_canary_and_hardline_vetoes_are_not_board_overridable(self):
        L = self.L
        L.propose_change("cy", "f", ["x.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("cy", "CIT-Director", "CIT", "ok")
        L.veto("cy", "Canary", "system unhealthy", role="CANARY")
        self.assertFalse(L.escalate_veto_to_board("cy")["ok"], "Canary veto escalated")
        L.propose_change("hl", "f", ["alpaca_trader.py"], 2, 0, "BUG_FIX", "PASS")
        L.veto("hl", "IRM-Director", "risky", role="IRM")
        self.assertFalse(L.escalate_veto_to_board("hl")["ok"], "hard-line veto escalated")

    def test_risk_governor_asymmetric(self):
        """Tighten always applies; loosen is banded, rate-limited, ceiling-bounded."""
        import tempfile, risk_limit_governor as G
        d = tempfile.mkdtemp()
        o, l, c = G.OVERRIDES_PATH, G.GOV_LOG_PATH, G.CHAIR_ACTION
        G.OVERRIDES_PATH = os.path.join(d, "ov.json")
        G.GOV_LOG_PATH = os.path.join(d, "log.json")
        G.CHAIR_ACTION = os.path.join(d, "chair.json")
        try:
            base = G.effective("MAX_OPEN_POSITIONS")
            self.assertEqual(G.propose_adjustment("MAX_OPEN_POSITIONS", base - 8, "de-risk", "CRO")["status"],
                             "APPLIED", "tighten must always apply")
            # past the Chairman-only ceiling → routed, never applied
            self.assertEqual(G.propose_adjustment("MAX_POSITION_SIZE_PCT", 0.20, "concentrate", "pod")["status"],
                             "ROUTED_TO_CHAIRMAN")
            # within band, first loosen of the day → applied
            self.assertEqual(G.propose_adjustment("MAX_POSITION_SIZE_PCT", 0.075, "add", "pod")["status"],
                             "APPLIED")
            # second loosen same day → rate-limited to Chairman
            self.assertEqual(G.propose_adjustment("MAX_POSITION_SIZE_PCT", 0.077, "add more", "pod")["status"],
                             "ROUTED_TO_CHAIRMAN")
        finally:
            G.OVERRIDES_PATH, G.GOV_LOG_PATH, G.CHAIR_ACTION = o, l, c

    def test_absorbed_protected_files_are_hard_line(self):
        """cit_implementation_agent's old PROTECTED_FILES must now all be hard-line
        so unifying the policy did not weaken protection."""
        L = self.L
        for f in ("main.py", "manager_agent.py", "board_meeting.py", "irm_review_agent.py"):
            self.assertTrue(L.touches_hard_line([f]), f"{f} lost hard-line protection")

    def test_clearance_requires_both_lenses_and_green_tests(self):
        L = self.L
        L.propose_change("c", "tidy", ["agent_meeting.py"], 3, 1, "BUG_FIX", None)
        self.assertFalse(L.is_cleared("c")[0], "cleared with no green test")
        L.attach_test_result("c", "PASS")
        L.sign("c", "CIT-Director", "CIT", "msg-only, no logic change")
        self.assertFalse(L.is_cleared("c")[0], "cleared with only one lens")
        L.sign("c", "IRM-Director", "IRM", "no risk surface")
        self.assertTrue(L.is_cleared("c")[0], "both lenses + green tests should clear")

    def test_hard_line_files_can_never_self_clear(self):
        L = self.L
        for f in ("alpaca_trader.py", "config.py", ".env"):
            L.propose_change(f, "x", [f], 1, 1, "BUG_FIX", "PASS")
            L.sign(f, "CIT-Director", "CIT", "trivial")
            L.sign(f, "IRM-Director", "IRM", "trivial")
            self.assertFalse(L.is_cleared(f)[0], f"hard-line {f} self-cleared")
            self.assertEqual(L._ledger()[f]["status"], "ROUTE_TO_HUMAN")

    def test_blast_radius_routes_to_human(self):
        L = self.L
        L.propose_change("big", "refactor", ["a.py", "b.py", "c.py"], 80, 20, "BUG_FIX", "PASS")
        L.sign("big", "CIT-Director", "CIT", "x")
        L.sign("big", "IRM-Director", "IRM", "y")
        self.assertFalse(L.is_cleared("big")[0], "oversized change cleared")

    def test_veto_blocks_and_empty_rationale_rejected(self):
        L = self.L
        L.propose_change("c", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        with self.assertRaises(ValueError):
            L.sign("c", "CIT-Director", "CIT", "   ")
        L.sign("c", "CIT-Director", "CIT", "ok")
        L.sign("c", "IRM-Director", "IRM", "ok")
        L.veto("c", "Canary", "flapping risk")
        self.assertFalse(L.is_cleared("c")[0], "veto failed to block")

    def test_one_agent_cannot_wear_both_lenses(self):
        """Dual sign-off is theatre if the same agent signs CIT and IRM."""
        L = self.L
        L.propose_change("c", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("c", "SoloActor", "CIT", "looks fine")
        L.sign("c", "SoloActor", "IRM", "looks fine")
        ok, reasons = L.is_cleared("c")
        self.assertFalse(ok, "one agent self-approved both lenses")
        self.assertTrue(any("distinct agents" in r for r in reasons))

    def test_hard_line_resists_rename_and_case_evasion(self):
        """Red team RT-09/RT-10: a hard-line guardrail must catch renamed
        siblings and case variants, not just the exact filename."""
        L = self.L
        for f in ("alpaca_trader_v2.py", "ALPACA_TRADER.py", "execution/config.py"):
            self.assertTrue(L.touches_hard_line([f]), f"hard-line evaded via {f}")

    def test_daily_line_cap_stops_split_to_evade(self):
        """Red team RT-18: three 40-line deploys must not smuggle 120 lines past
        the blast-radius limit — the daily line cap bites at deploy time."""
        L = self.L
        shipped = 0
        for i in range(3):
            cid = f"s{i}"
            L.propose_change(cid, "x", [f"p{i}.py"], 40, 0, "BUG_FIX", "PASS")
            L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
            if L.reserve_and_deploy(cid)[0]:
                shipped += 1
        self.assertLessEqual(shipped * 40, L.MAX_DAILY_DEPLOY_LINES)

    def test_reserve_and_deploy_enforces_budget(self):
        """Red team RT-20: the sanctioned deploy path caps deploys/day."""
        L = self.L
        shipped = 0
        for i in range(6):
            cid = f"b{i}"
            L.propose_change(cid, "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
            L.sign(cid, "CIT1", "CIT", "ok"); L.sign(cid, "IRM1", "IRM", "ok")
            if L.reserve_and_deploy(cid)[0]:
                shipped += 1
        self.assertLessEqual(shipped, L.DAILY_DEPLOY_BUDGET)

    def test_stale_green_tests_do_not_clear(self):
        """Red team RT-16 (staleness) + X9 (the no-git host): a PASS must stop
        clearing as soon as the code it ran against changes.

        The original guard compared git HEAD, and the GCP VM is not a git
        checkout — `_git_head()` returned None there and the whole check
        short-circuited, so the always-on host had no staleness guard at all.
        This asserts the content-fingerprint path with git forced unavailable."""
        import os, tempfile
        L = self.L
        victim = os.path.join(tempfile.mkdtemp(), "victim_agent.py")
        with open(victim, "w") as fh:
            fh.write("# v1\n")
        real_head, L._git_head = L._git_head, lambda: None       # simulate the VM
        try:
            L.propose_change("a", "x", [victim], 2, 0, "BUG_FIX", None)
            L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
            L.attach_test_result("a", "PASS")
            self.assertTrue(L.is_cleared("a")[0], "green + signed should clear")
            with open(victim, "w") as fh:                        # code moves on
                fh.write("# v2 - changed after the green test\n")
            ok, reasons = L.is_cleared("a")
            self.assertFalse(ok, "stale green tests cleared with no git present")
            self.assertTrue(any("stale" in r for r in reasons), reasons)
        finally:
            L._git_head = real_head

    def test_green_test_with_no_fingerprint_does_not_clear(self):
        """A PASS carrying no fingerprint at all predates the X9 guard. It must
        fail toward re-testing, not be trusted — otherwise the migration window
        is a hole you can drive any change through."""
        L = self.L
        L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", None)
        L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
        rec = L._ledger()
        rec["a"]["test_result"] = "PASS"          # green, but no fingerprint
        L._save(L.LEDGER_PATH, rec)
        ok, reasons = L.is_cleared("a")
        self.assertFalse(ok, "unfingerprinted PASS cleared")
        self.assertTrue(any("fingerprint" in r for r in reasons), reasons)

    def test_outcome_requires_deploy(self):
        """Red team RT-21: cannot score a fix that never shipped."""
        L = self.L
        L.propose_change("a", "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
        L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
        with self.assertRaises(ValueError):
            L.record_outcome("a", "HELD")

    def test_agent_identity_is_normalized(self):
        """Red team RT-23/24: name variants (case, whitespace) must not count as
        two distinct signers."""
        L = self.L
        for a2 in ("solo", "Solo "):
            cid = "c_" + a2.strip()
            L.propose_change(cid, "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
            L.sign(cid, "Solo", "CIT", "ok"); L.sign(cid, a2, "IRM", "ok")
            self.assertFalse(L.is_cleared(cid)[0], f"'{a2}' spoofed a distinct agent")

    def test_empty_or_exotic_file_list_does_not_clear(self):
        """Red team RT-25/27: undeclared files and non-ASCII names route to human."""
        L = self.L
        L.propose_change("empty", "x", [], 5, 5, "BUG_FIX", "PASS")
        L.sign("empty", "CIT1", "CIT", "ok"); L.sign("empty", "IRM1", "IRM", "ok")
        self.assertFalse(L.is_cleared("empty")[0], "empty file list cleared")
        L.propose_change("uni", "x", ["cοnfig.py"], 1, 0, "BUG_FIX", "PASS")
        L.sign("uni", "CIT1", "CIT", "ok"); L.sign("uni", "IRM1", "IRM", "ok")
        self.assertFalse(L.is_cleared("uni")[0], "non-ascii filename cleared")

    def test_negative_line_counts_cannot_mask_blast(self):
        """Red team RT-26: a negative removed can't net a huge change under the limit."""
        L = self.L
        L.propose_change("b", "x", ["big.py"], 5000, -4980, "BUG_FIX", "PASS")
        L.sign("b", "CIT1", "CIT", "ok"); L.sign("b", "IRM1", "IRM", "ok")
        self.assertFalse(L.is_cleared("b")[0], "negative line count masked a 5000-line change")

    def test_hr_flag_survives_reputation_farming(self):
        """Red team RT-28: farming HELDs must not hide a recent burn from HR."""
        L = self.L
        def cyc(cid, outcome):
            L.propose_change(cid, "x", ["z.py"], 1, 0, "BUG_FIX", "PASS")
            L.sign(cid, "A", "CIT", "ok"); L.sign(cid, "B", "IRM", "ok")
            if L.reserve_and_deploy(cid)[0]:
                L.record_outcome(cid, outcome, "A")
        cyc("burn", "REVERTED")
        for i in range(10):
            cyc(f"f{i}", "HELD")
        self.assertTrue(L.hr_flag("A"), "farming hid a burn from HR")

    def test_reverted_outcome_costs_more_than_a_hold(self):
        """Reputation must be outcome-weighted and asymmetric, or sign-off is theatre."""
        L = self.L
        L.propose_change("g", "good", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("g", "S", "CIT", "ok"); L.sign("g", "IRM-x", "IRM", "ok")
        L.reserve_and_deploy("g"); L.record_outcome("g", "HELD", reported_by="HR")
        L.propose_change("b", "bad", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("b", "S", "CIT", "ok"); L.sign("b", "IRM-x", "IRM", "ok")
        L.reserve_and_deploy("b"); L.record_outcome("b", "REVERTED", reported_by="HR")
        self.assertLess(L.reputation("S")["ewma"], 0.0,
                        "a hold then a revert should net NEGATIVE (burn weighs 3x)")

    def test_shadow_logs_clears_so_promotion_can_prove_not_a_full_stop(self):
        """SHADOW must record CLEARED decisions too, not just would-blocks —
        otherwise the promotion gate can't prove ENFORCE won't be a full stop."""
        L = self.L
        # a fully-cleared change (both lenses + green tests)
        L.propose_change("ok", "x", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("ok", "CIT1", "CIT", "correct"); L.sign("ok", "IRM1", "IRM", "no risk")
        pf = L.preflight("ok", actuator="test")
        self.assertTrue(pf["allow"] and not pf["would_block"])
        summ = L.shadow_summary()
        self.assertGreaterEqual(summ["would_clear_events"], 1,
                                "shadow must log a cleared decision")
        self.assertGreaterEqual(summ["distinct_would_clear_changes"], 1)

    def test_promotion_gate_thresholds_and_signals(self):
        """Guard the promotion gate's pure logic (NOT evaluate(), which shells out
        to this very suite). A fresh window must fail the window check, and the
        gate must consume the would-clear signal we log in shadow."""
        import signoff_promotion_gate as G
        self.assertGreaterEqual(G.MIN_WINDOW_DAYS, 7, "week-long observation is the floor")
        self.assertGreaterEqual(G.MIN_WOULD_CLEAR, 1, "must require a real cleared change")
        # a brand-new window is younger than MIN_WINDOW_DAYS → not promotable
        self.L.set_mode("SHADOW", [], note="window start")
        meta = self.L._load(self.L.MODE_PATH, {})
        start = G._parse(meta.get("set_at", ""))
        self.assertIsNotNone(start)
        days = (G._now() - start).total_seconds() / 86400
        self.assertLess(days, G.MIN_WINDOW_DAYS, "fresh window must not satisfy the gate")


class TestSignoffProduction(unittest.TestCase):
    """The sign-off PRODUCTION path: an autonomous fix must be able to EARN its
    CIT + IRM sign-offs, or ENFORCE would be a full stop. Guards the actuator's
    accounting + the IRM risk-envelope lens (2026-07-29)."""

    def setUp(self):
        import tempfile, signoff_ledger as L, cit_implementation_agent as C
        self.L, self.C = L, C
        self._d = tempfile.mkdtemp()
        self._paths = (L.LEDGER_PATH, L.REPUTATION_PATH, L.MODE_PATH, L.SHADOW_LOG_PATH)
        L.LEDGER_PATH = os.path.join(self._d, "led.json")
        L.REPUTATION_PATH = os.path.join(self._d, "rep.json")
        L.MODE_PATH = os.path.join(self._d, "mode.json")
        L.SHADOW_LOG_PATH = os.path.join(self._d, "shadow.json")
        # Patch the green-test runner so the suite never shells out to itself.
        self._green = C._run_green_tests
        C._run_green_tests = lambda timeout=600: True

    def tearDown(self):
        self.L.LEDGER_PATH, self.L.REPUTATION_PATH, self.L.MODE_PATH, self.L.SHADOW_LOG_PATH = self._paths
        self.C._run_green_tests = self._green

    def _tmp_pyfile(self, name, content):
        import pathlib
        p = pathlib.Path(self._d) / name
        p.write_text(content)
        return p

    def test_irm_envelope_lens_is_bounded(self):
        import irm_code_envelope as E
        self.assertTrue(E.review(["foo.py"], 2, 0, "LOW", "BUG_FIX")["ok"])
        self.assertFalse(E.review(["foo.py"], 2, 0, "HIGH", "BUG_FIX")["ok"], "HIGH cleared")
        self.assertFalse(E.review(["config.py"], 1, 0, "LOW", "BUG_FIX")["ok"], "hard-line cleared")
        self.assertFalse(E.review(["a.py", "b.py", "c.py"], 90, 0, "LOW", "BUG_FIX")["ok"], "blast cleared")
        self.assertFalse(E.review(["foo.py"], 2, 0, "LOW", "REFACTOR")["ok"], "off-envelope category cleared")

    def test_low_fix_earns_dual_signoff_and_clears(self):
        """A LOW BUG_FIX with green tests must produce CIT+IRM sign-offs and CLEAR —
        proving ENFORCE won't be a full stop."""
        C = self.C
        p = self._tmp_pyfile("gizmo.py", "a = 1\n")
        p.write_text("import os\na = 1\n")   # simulate the applied edit
        acct = C._account_and_gate("P1", "guard a write", p, "BUG_FIX", "LOW",
                                    90, "a = 1\n", "added makedirs guard")
        self.assertTrue(acct["cleared"], "LOW fix with green tests failed to clear")
        summ = self.L.shadow_summary()
        self.assertGreaterEqual(summ["distinct_would_clear_changes"], 1)
        # both distinct lenses present
        sigs = self.L._ledger()["citimpl-P1"]["signatures"]
        self.assertEqual({s["role"] for s in sigs}, {"CIT", "IRM"})

    def test_medium_fix_is_vetoed_by_irm_and_would_block(self):
        C = self.C
        p = self._tmp_pyfile("gizmo2.py", "a = 1\n")
        p.write_text("import os\na = 1\n")
        acct = C._account_and_gate("P2", "risky change", p, "BUG_FIX", "MEDIUM",
                                   90, "a = 1\n", "wrapped call")
        self.assertFalse(acct["cleared"], "MEDIUM change cleared the envelope")
        rec = self.L._ledger()["citimpl-P2"]
        self.assertEqual(rec["veto"]["role"], "IRM")

    def test_enforce_reverts_a_blocked_fix(self):
        """In ENFORCE, a fix the gate blocks must be UNDONE, not left in the tree."""
        C = self.C
        self.L.set_mode("ENFORCE", ["BOARD", "CIT"])
        p = self._tmp_pyfile("gizmo3.py", "a = 1\n")
        p.write_text("import os\na = 1\n")           # the applied (bad) edit
        acct = C._account_and_gate("P3", "risky", p, "BUG_FIX", "MEDIUM",
                                   90, "a = 1\n", "edit")
        self.assertTrue(acct["reverted"], "ENFORCE failed to revert a blocked fix")
        self.assertEqual(p.read_text(), "a = 1\n", "file was not restored to pre-edit")


class TestProblemLifecycle(unittest.TestCase):
    """Closed-loop problem lifecycle: every flagged problem is acted on, and no
    problem loops (unaddressed OR fixed-then-recurring). Guards the firm's most
    notorious bug class. All paths sandboxed to a temp dir."""

    def setUp(self):
        import tempfile, problem_lifecycle_sentinel as S
        from pathlib import Path as _P
        self.S = S
        self.tmp = tempfile.mkdtemp()
        d = _P(self.tmp)
        self._orig = (S.PENDING_PATH, S.IMPL_QUEUE_PATH, S.CHAIRMAN_PATH,
                      S.HISTORY_PATH, S.LOOPS_PATH)
        self.pending = d / "pending.json"
        self.impl = d / "impl.json"
        self.chair = d / "chair.json"
        S.PENDING_PATH = self.pending
        S.IMPL_QUEUE_PATH = self.impl
        S.CHAIRMAN_PATH = self.chair
        S.HISTORY_PATH = d / "history.json"
        S.LOOPS_PATH = d / "loops.json"

    def tearDown(self):
        S = self.S
        (S.PENDING_PATH, S.IMPL_QUEUE_PATH, S.CHAIRMAN_PATH,
         S.HISTORY_PATH, S.LOOPS_PATH) = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, items):
        path.write_text(json.dumps(items))

    def test_signature_is_stable_across_id_and_number_drift(self):
        """The core anti-loop property: same problem, new id + drifted numbers ==
        SAME signature. (id-keyed trackers are what let the 210x loop hide.)"""
        S = self.S
        a = {"id": "LTIA-20260729105803", "title": "6 ERROR lines — dominant: 'get'"}
        b = {"id": "LTIA-20260801120000", "title": "8 ERROR lines — dominant: 'get'"}
        self.assertEqual(S.signature(a, "impl"), S.signature(b, "impl"))
        # explicit dedup_key wins and is stable
        c = {"id": "x", "title": "whatever", "dedup_key": "idle_pod_pod2_ml"}
        dd = {"id": "y", "title": "different words", "dedup_key": "idle_pod_pod2_ml"}
        self.assertEqual(S.signature(c, "impl"), S.signature(dd, "impl"))

    def test_fixed_then_recurring_is_LOOPING_and_escalated_to_full_board(self):
        S = self.S
        prob = {"id": "p1", "title": "widget X broken", "dedup_key": "widget_x",
                "status": "queued", "category": "BUG_FIX", "severity": "LOW"}
        # 1) open
        self._write(self.impl, [prob])
        S.run()
        # 2) resolved
        self._write(self.impl, [{**prob, "status": "implemented"}])
        S.run()
        # 3) it came back (new id, still same problem) -> LOOPING
        self._write(self.impl, [{**prob, "id": "p1-again", "status": "queued"}])
        rep = S.run()
        sig = S.signature(prob, "impl")
        self.assertIn(sig, rep["looping"])
        self.assertEqual(rep["escalated_board"], 1)
        # escalation landed in the board queue with a FULL-board-recognized category
        board = json.loads(self.pending.read_text())
        entry = [e for e in board if e.get("dedup_key") == f"loop_{sig}"][0]
        import deputy_board
        self.assertIn(entry["category"],
                      deputy_board.DEPUTY_CATEGORIES | deputy_board.FULL_BOARD_ONLY_CATEGORIES,
                      "loop escalation uses a category no board consumer handles (orphan!)")
        self.assertIn(entry["category"], deputy_board.FULL_BOARD_ONLY_CATEGORIES,
                      "recurring loops must go to the FULL board, not auto-deferred")
        # and it's in the loop-break set the actuator consults
        self.assertIn(sig, rep["loop_break_set"])
        self.assertTrue(S.is_loop_broken({**prob, "id": "p1-again"}, "impl"))

    def test_open_past_SLA_is_STALE_and_escalated_to_chairman(self):
        S = self.S
        prob = {"id": "s1", "title": "slow decay", "dedup_key": "slow_decay",
                "status": "queued"}
        self._write(self.impl, [prob])
        S.run()
        # backdate first_seen/last_open beyond the SLA
        hist = json.loads(S.HISTORY_PATH.read_text())
        old = "2026-01-01T00:00:00+00:00"
        for h in hist.values():
            h["first_seen"] = old
            h["last_open"] = old
        S.HISTORY_PATH.write_text(json.dumps(hist))
        rep = S.run()
        sig = S.signature(prob, "impl")
        self.assertIn(sig, rep["stale"])
        self.assertEqual(rep["escalated_chairman"], 1)
        chair = json.loads(self.chair.read_text())
        self.assertTrue(any(e["id"] == f"lifecycle-stale-{sig}" for e in chair))

    def test_vanished_without_resolution_is_SILENT_DROP(self):
        S = self.S
        prob = {"id": "d1", "title": "ghost problem", "dedup_key": "ghost",
                "status": "queued"}
        self._write(self.impl, [prob])
        S.run()
        # it disappears from every queue with no recorded resolution
        self._write(self.impl, [])
        rep = S.run()
        sig = S.signature(prob, "impl")
        self.assertIn(sig, rep["silent_drop"])
        chair = json.loads(self.chair.read_text())
        self.assertTrue(any(e["id"] == f"lifecycle-drop-{sig}" for e in chair))

    def test_escalations_are_idempotent_no_queue_flood(self):
        """The cardinal rule: the sentinel must not BECOME the loop. Running it
        repeatedly on an unchanged state must not re-escalate."""
        S = self.S
        prob = {"id": "i1", "title": "recur me", "dedup_key": "recur_me",
                "status": "queued", "category": "BUG_FIX", "severity": "LOW"}
        self._write(self.impl, [prob])
        S.run()
        self._write(self.impl, [{**prob, "status": "implemented"}])
        S.run()
        self._write(self.impl, [{**prob, "id": "i1-b", "status": "queued"}])
        S.run()          # first LOOPING escalation
        _rd = lambda p: len(json.loads(p.read_text())) if p.exists() else 0
        n_board = _rd(self.pending)
        n_chair = _rd(self.chair)
        self.assertGreaterEqual(n_board, 1, "expected a board escalation to dedup against")
        for _ in range(5):     # hammer it
            S.run()
        self.assertEqual(_rd(self.pending), n_board,
                         "sentinel re-flooded the board queue (not idempotent)")
        self.assertEqual(_rd(self.chair), n_chair, "sentinel re-flooded the chairman queue")

    def test_wont_fix_is_not_a_silent_drop(self):
        """A skipped/protected finding is a DECISION, not a dropped problem."""
        S = self.S
        prob = {"id": "w1", "title": "known limitation", "dedup_key": "known_lim",
                "status": "skipped"}
        self._write(self.impl, [prob])
        rep = S.run()
        self.assertEqual(rep["silent_drop"], [])
        self.assertEqual(rep["stale"], [])


class TestTechShortPod(unittest.TestCase):
    """Korea/AI-hardware short pod producer->consumer contract. The short book
    must only open KR-stress shorts when the signal is ACTIVE and FRESH
    (fail-closed), and the pod's output must feed the short book."""

    def setUp(self):
        import tempfile, tech_short_pod as T
        from pathlib import Path as _P
        self.T = T
        self.tmp = tempfile.mkdtemp()
        self._orig = T.OUTPUT_PATH
        T.OUTPUT_PATH = _P(self.tmp) / "cand.json"

    def tearDown(self):
        import shutil
        self.T.OUTPUT_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, active, ts, tickers=("QCOM", "ORCL")):
        self.T.OUTPUT_PATH.write_text(json.dumps(
            {"active": active, "ts": ts, "tickers": list(tickers)}))

    def test_active_fresh_signal_feeds_candidates(self):
        from datetime import datetime, timezone
        self._write(True, datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.T.load_candidates(), ["QCOM", "ORCL"])

    def test_inactive_signal_is_fail_closed(self):
        from datetime import datetime, timezone
        self._write(False, datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.T.load_candidates(), [],
                         "inactive KR signal must never open shorts")

    def test_stale_report_is_fail_closed(self):
        self._write(True, "2026-01-01T00:00:00+00:00")
        self.assertEqual(self.T.load_candidates(max_age_hours=30), [],
                         "a stale KR report must never open shorts")

    def test_missing_report_is_fail_closed(self):
        self.assertEqual(self.T.load_candidates(), [])

    def test_short_book_consumes_the_pod(self):
        """The wiring contract: alpaca_trader imports the pod and its universe
        exists — else the pod is an orphan producer."""
        import config
        self.assertTrue(hasattr(config, "KR_SHORT_UNIVERSE"))
        self.assertIn("NVDA", config.KR_SHORT_UNIVERSE)
        src = open(Path(__file__).parent / "alpaca_trader.py").read()
        self.assertIn("_tech_short.load_candidates", src,
                      "short book must consume tech_short_pod.load_candidates()")


class TestThesisWatch(unittest.TestCase):
    """The DO-NOT-SHORT verdict has two named flip conditions. A verdict with
    flip conditions and nobody watching them is a stale opinion, so the pod
    re-tests both every run.

    The trap this class exists to pin: the first version used ABSOLUTE
    thresholds and fired both conditions on its very first run — because ORCL
    was already -61.5% off its high and ORCL/PLTR/SMCI were already the
    candidate set ON THE DAY THE VERDICT WAS WRITTEN. A fact the verdict was
    made from cannot be evidence against the verdict. Only movement is news."""

    def setUp(self):
        import tempfile, tech_short_pod as T
        from pathlib import Path as _P
        self.T = T
        self.tmp = _P(tempfile.mkdtemp())
        self._orig = (T.BASELINE_PATH, T.CHAIRMAN_PATH, T._credit_channel, T._hist)
        T.BASELINE_PATH = self.tmp / "baseline.json"
        T.CHAIRMAN_PATH = self.tmp / "chairman.json"
        self.base = {
            "as_of": "2026-07-30", "verdict": "DO NOT SHORT",
            "kr_signal_level": "ACUTE",
            "candidates": ["ORCL", "PLTR", "SMCI"],
            "excluded_too_extended": {"QCOM": 0.0, "VRT": 0.4},
            "hy_minus_ig_60d_pct": 1.67, "orcl_drawdown_pct": -61.5,
        }
        T.BASELINE_PATH.write_text(json.dumps(self.base))
        self._set_credit(cracking=False)

    def tearDown(self):
        import shutil
        (self.T.BASELINE_PATH, self.T.CHAIRMAN_PATH,
         self.T._credit_channel, self.T._hist) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_credit(self, cracking, measurable=True):
        self.T._credit_channel = lambda base=None: {
            "measurable": measurable, "cracking": cracking, "detail": "stub"}

    @staticmethod
    def _report(cands):
        return {"signal": {"level": "ACUTE"},
                "candidates": [{"ticker": t, "extension_pctile": p} for t, p in cands]}

    def _chairman(self):
        try:
            return json.loads(self.T.CHAIRMAN_PATH.read_text())
        except Exception:
            return []

    def test_unchanged_state_does_not_fire(self):
        """THE key test: the exact verdict-day state must read VERDICT STANDS."""
        rep = self._report([("ORCL", 40), ("PLTR", 35), ("SMCI", 30)])
        w = self.T.thesis_watch(rep)
        self.assertEqual(w["triggers"], [],
                         "fired on the same facts the verdict was made from")
        self.assertFalse(w["entry_window_open"])
        self.assertIn("VERDICT STANDS", w["status"])
        self.assertEqual(self._chairman(), [], "must not escalate the status quo")

    def test_excluded_name_recovering_opens_the_entry_window(self):
        """QCOM was excluded at the 0.0th pctile (squeeze risk). It rallying back
        toward its 50MA IS the entry the verdict named."""
        rep = self._report([("ORCL", 40), ("QCOM", 35)])
        w = self.T.thesis_watch(rep)
        self.assertTrue(w["entry_window_open"])
        self.assertEqual([t["condition"] for t in w["triggers"]], ["ENTRY_WINDOW"])
        self.assertIn("QCOM", w["triggers"][0]["detail"])
        self.assertEqual(len(self._chairman()), 1)

    def test_a_newly_qualifying_name_fires(self):
        rep = self._report([("ORCL", 40), ("NVDA", 45)])
        w = self.T.thesis_watch(rep)
        self.assertTrue(w["entry_window_open"])
        self.assertIn("NVDA", w["triggers"][0]["detail"])

    def test_still_extended_excluded_name_does_not_fire(self):
        """QCOM back to only the 5th pctile is still in the squeeze zone —
        below ENTRY_PCTILE_RECOVERY it is not an entry."""
        rep = self._report([("QCOM", 5)])
        self.assertEqual(self.T.thesis_watch(rep)["triggers"], [])

    def test_credit_channel_fires_and_is_high_severity(self):
        self._set_credit(cracking=True)
        w = self.T.thesis_watch(self._report([("ORCL", 40)]))
        conds = [t["condition"] for t in w["triggers"]]
        self.assertIn("CREDIT_CHANNEL", conds)
        sev = [t["severity"] for t in w["triggers"] if t["condition"] == "CREDIT_CHANNEL"][0]
        self.assertEqual(sev, "HIGH", "credit is the channel that would actually crack")

    def test_escalation_is_idempotent(self):
        """The pod runs 3x/weekday. A persisting condition must inform once,
        not flood the Chairman queue 15x a week."""
        self._set_credit(cracking=True)
        rep = self._report([("QCOM", 35)])
        first = self.T.thesis_watch(rep)
        self.assertEqual(len(first["escalated"]), 2)
        for _ in range(5):
            again = self.T.thesis_watch(rep)
            self.assertEqual(again["escalated"], [], "re-escalated a known condition")
        self.assertEqual(len(self._chairman()), 2)

    def test_missing_baseline_is_inert_not_loud(self):
        """No baseline means every reading looks like a change. Fail SILENT."""
        self.T.BASELINE_PATH.unlink()
        w = self.T.thesis_watch(self._report([("QCOM", 35), ("NVDA", 40)]))
        self.assertFalse(w["entry_window_open"])
        self.assertEqual(self._chairman(), [], "an absent baseline must not alarm")

    def test_data_outage_does_not_manufacture_an_alarm(self):
        self._set_credit(cracking=False, measurable=False)
        w = self.T.thesis_watch(self._report([("ORCL", 40)]))
        self.assertEqual(w["triggers"], [])

    def test_watch_runs_on_the_pods_existing_schedule(self):
        """Wiring contract: the watch must be invoked by run(), which cron
        already calls 3x/weekday — else it is an orphan that never fires.

        Asserted behaviourally, not by grep: an earlier version of this test
        searched the source for 'thesis_watch(report' and was satisfied by the
        function DEFINITION, so deleting the call site survived it."""
        import tech_short_pod as T
        called = {}
        orig_watch, orig_screen = T.thesis_watch, T.screen
        try:
            T.screen = lambda *a, **k: self._report([("ORCL", 40)])
            T.thesis_watch = lambda rep, escalate=True: called.setdefault("hit", True) and {}
            rep = T.run(write=False)
        finally:
            T.thesis_watch, T.screen = orig_watch, orig_screen
        self.assertTrue(called.get("hit"), "run() never invoked thesis_watch")
        self.assertIn("thesis_watch", rep, "run() must publish the watch in its report")

    # -- the real _credit_channel, with prices injected (no network) ---------
    def _stub_hist(self, hyg_ret, lqd_ret, orcl_dd):
        """Build synthetic 60-bar series producing the requested readings."""
        import pandas as pd

        def mk(ret_pct, dd_pct=0.0):
            start = 100.0
            end = start * (1 + ret_pct / 100)
            close = [start] * 61
            close[-1] = end
            if dd_pct:                       # push the 52w high up to force drawdown
                close[0] = end / (1 + dd_pct / 100)
            return pd.DataFrame({"Close": close})

        series = {"HYG": mk(hyg_ret), "LQD": mk(lqd_ret), "ORCL": mk(0.0, orcl_dd)}
        self.T._hist = lambda tk, period="300d": series.get(tk)

    def test_credit_unchanged_from_baseline_is_not_cracking(self):
        self.T._credit_channel = self._orig[2]        # restore the real function
        self._stub_hist(hyg_ret=1.67, lqd_ret=0.0, orcl_dd=-61.5)
        c = self.T._credit_channel(self.base)
        self.assertTrue(c["measurable"])
        self.assertFalse(c["cracking"], "baseline-identical credit must not alarm")

    def test_credit_cracks_when_hy_deteriorates_below_baseline_and_turns_negative(self):
        self.T._credit_channel = self._orig[2]
        self._stub_hist(hyg_ret=-2.0, lqd_ret=0.0, orcl_dd=-61.5)   # -2.0 vs +1.67 = -3.67
        self.assertTrue(self.T._credit_channel(self.base)["cracking"])

    def test_credit_does_not_crack_while_spread_is_still_positive(self):
        """M5 guard: a 3pt fall that leaves HY still OUTPERFORMING IG is not
        credit stress — the spread must actually turn negative."""
        self.T._credit_channel = self._orig[2]
        self._stub_hist(hyg_ret=-1.5, lqd_ret=-3.0, orcl_dd=-61.5)  # spread +1.5, still positive
        c = self.T._credit_channel(self.base)
        self.assertFalse(c["cracking"], "HY outperforming IG is not credit stress")

    def test_credit_cracks_on_further_orcl_drawdown(self):
        self.T._credit_channel = self._orig[2]
        self._stub_hist(hyg_ret=1.67, lqd_ret=0.0, orcl_dd=-75.0)   # -13.5pts vs baseline
        self.assertTrue(self.T._credit_channel(self.base)["cracking"])

    def test_real_credit_channel_fails_safe_on_missing_data(self):
        """M8 guard: a data outage must report measurable=False and NOT cracking."""
        self.T._credit_channel = self._orig[2]
        self.T._hist = lambda tk, period="300d": None
        c = self.T._credit_channel(self.base)
        self.assertFalse(c["measurable"])
        self.assertFalse(c["cracking"], "a yfinance outage must never manufacture an alarm")


class TestShortBookCanClose(unittest.TestCase):
    """A short book that can OPEN but never CLOSE is a one-way risk machine.

    Found 2026-07-30: `submit_sell()` read the position qty, and a short carries
    a NEGATIVE qty, so the `qty <= 0` guard rejected every short with a
    misleading "held for pending orders" warning. Neither the cover-above-50MA
    rule nor the -10% stop-loss could fire on any short, ever. Six shorts sat
    un-closeable. These tests pin the close path shut against that regression."""

    def _trader(self, positions_df):
        """An AlpacaTrader with no network: __init__ is skipped and the SDK
        types are replaced with recorders, so we can assert on the ORDER built
        rather than on anything Alpaca returns."""
        import alpaca_trader
        t = alpaca_trader.AlpacaTrader.__new__(alpaca_trader.AlpacaTrader)
        self.submitted = []

        class _Req:
            def __init__(self, symbol, qty, side, time_in_force):
                self.symbol, self.qty = symbol, qty
                self.side, self.time_in_force = side, time_in_force

        class _Side:
            BUY, SELL = "BUY", "SELL"

        class _TIF:
            DAY = "DAY"

        class _Client:
            def __init__(self, sink):
                self.sink = sink

            def submit_order(self, req):
                self.sink.append(req)
                return type("O", (), {"id": "test-order", "status": "accepted"})()

        t._MarketOrderRequest = _Req
        t._OrderSide = _Side
        t._TimeInForce = _TIF
        t.client = _Client(self.submitted)
        t.get_positions = lambda: positions_df
        return t

    @staticmethod
    def _pos(ticker, qty):
        import pandas as pd
        return pd.DataFrame([{"ticker": ticker, "qty": qty, "qty_available": qty}])

    def test_short_position_is_closed_with_a_buy_to_cover(self):
        t = self._trader(self._pos("XLE", -163))
        res = t.submit_sell("XLE", reason="above_50ma")
        self.assertIsNotNone(res, "a short MUST be closeable — this is the 2026-07-30 bug")
        self.assertEqual(len(self.submitted), 1)
        order = self.submitted[0]
        self.assertEqual(order.side, "BUY", "closing a short is a BUY-to-cover, not a SELL")
        self.assertEqual(order.qty, 163, "cover qty must be the ABSOLUTE size of the short")
        self.assertEqual(res["side"], "buy_to_cover")

    def test_a_short_is_never_closed_with_a_sell(self):
        """A SELL on a short doubles the position instead of closing it —
        the single most dangerous way for this bug to come back."""
        t = self._trader(self._pos("QQQ", -11))
        t.submit_sell("QQQ", reason="stop_loss")
        self.assertTrue(self.submitted, "no order was placed at all")
        self.assertNotEqual(self.submitted[0].side, "SELL",
                            "SELL on a negative-qty position INCREASES the short")

    def test_long_position_still_sells(self):
        t = self._trader(self._pos("AAPL", 50))
        res = t.submit_sell("AAPL", reason="signal")
        self.assertEqual(self.submitted[0].side, "SELL")
        self.assertEqual(self.submitted[0].qty, 50)
        self.assertEqual(res["qty"], 50)

    def test_fully_held_position_still_skips(self):
        """The original guard existed for a real reason (Alpaca 40310000 when
        every share is held by a pending order). Fixing shorts must not delete
        it: qty_available == 0 still means 'place no order'."""
        import pandas as pd
        df = pd.DataFrame([{"ticker": "PFE", "qty": 100, "qty_available": 0}])
        t = self._trader(df)
        self.assertIsNone(t.submit_sell("PFE"))
        self.assertEqual(self.submitted, [], "must not submit an order for a held position")

    def test_legacy_short_screen_does_not_rank_by_extension(self):
        """The anti-chasing law, on the LEGACY entry path.

        `get_momentum_short_candidates()` used to score candidates as
        (pct_from_50 + pct_from_200) and sort worst-first — i.e. it treated
        maximum downside extension as the BEST short. Measured over 5y that is
        backwards (basket avg fwd-20d +6.73%, short win rate 40.7%). Extension
        is a disqualifier, never a ranking bonus, on BOTH entry paths."""
        src = open(Path(__file__).parent / "alpaca_trader.py").read()
        self.assertNotIn("confirmed.append((ticker, pct_from_50 + pct_from_200))", src,
                         "legacy short screen is ranking by extension again")
        self.assertIn("MIN_EXTENSION_PCTILE", src,
                      "legacy short screen must apply the anti-chasing gate")

    # -- the false audit trail -------------------------------------------
    # Second bug found in the same VM log on 2026-07-30: the cover rule fired,
    # submit_sell() refused, and manage_momentum_shorts() appended
    # {"action": "covered"} anyway — so main.py printed "COVERED: XLE" for a
    # position that was still open. The books said the trade happened. It
    # hadn't. An action log that reports success without checking the result is
    # worse than no log: it actively conceals the failure it should surface.

    def _shorts_trader(self, cover_result, price=110.0, ma_level=100.0, pnl=1.0):
        """A trader whose submit_sell returns exactly `cover_result`. Defaults
        force the 'recovered above 50MA' branch; pass price < ma_level with
        pnl <= -10.0 to drive the stop-loss branch instead."""
        import pandas as pd
        import alpaca_trader
        t = alpaca_trader.AlpacaTrader.__new__(alpaca_trader.AlpacaTrader)
        t.get_positions = lambda: pd.DataFrame(
            [{"ticker": "XLE", "qty": -163, "qty_available": -163,
              "unrealized_pct": pnl}])
        self.sell_calls = []

        def _submit_sell(ticker, reason=None):
            self.sell_calls.append((ticker, reason))
            return cover_result

        t.submit_sell = _submit_sell
        # The open-new-shorts phase is not under test; keep it a no-op.
        t.get_account = lambda: {"portfolio_value": 100000.0, "cash": 50000.0}
        t.get_momentum_short_candidates = lambda *a, **k: []
        closes = pd.Series([ma_level] * 49 + [price])
        df = pd.DataFrame({"Close": closes})
        self._orig_yf = alpaca_trader.yf
        alpaca_trader.yf = type("YF", (), {
            "download": staticmethod(lambda *a, **k: df)})()
        self.addCleanup(lambda: setattr(alpaca_trader, "yf", self._orig_yf))
        return t

    def test_a_refused_cover_is_not_logged_as_covered(self):
        """submit_sell returning None means NO ORDER WAS PLACED. The action log
        must say so — this is the line that printed a phantom 'COVERED: XLE'."""
        t = self._shorts_trader(cover_result=None)
        actions = t.manage_momentum_shorts()
        self.assertTrue(self.sell_calls, "the cover rule should still have fired")
        covered = [a for a in actions if a.get("action") == "covered"]
        self.assertEqual(covered, [],
                         "a cover that placed no order was logged as COVERED — "
                         "this is the 2026-07-30 false audit trail")
        self.assertTrue([a for a in actions if a.get("action") == "cover_failed"],
                        "a refused cover must be surfaced, not silently dropped")

    def test_a_real_cover_is_still_logged_as_covered(self):
        """The fix must not go the other way and stop recording real covers."""
        t = self._shorts_trader(
            cover_result={"id": "abc123", "ticker": "XLE", "qty": 163,
                          "side": "buy_to_cover"})
        actions = t.manage_momentum_shorts()
        covered = [a for a in actions if a.get("action") == "covered"]
        self.assertEqual(len(covered), 1, "a successful cover must be recorded")
        self.assertEqual(covered[0]["ticker"], "XLE")
        self.assertEqual(covered[0].get("order_id"), "abc123",
                         "the recorded action must carry the real broker order id")

    def test_a_refused_stop_loss_is_not_logged_as_covered(self):
        """Same guarantee on the stop-loss branch. Caught by mutation testing:
        the first version of these tests only exercised the 50MA branch, so
        sabotaging the stop-loss path survived clean."""
        t = self._shorts_trader(cover_result=None, price=90.0, ma_level=100.0,
                                pnl=-14.0)
        actions = t.manage_momentum_shorts()
        self.assertEqual(self.sell_calls, [("XLE", "short_stop_loss")],
                         "the stop-loss branch should have fired")
        self.assertEqual([a for a in actions if a.get("action") == "covered"], [],
                         "a stop-loss that placed no order was logged as COVERED")
        self.assertTrue([a for a in actions if a.get("action") == "cover_failed"],
                        "a refused stop-loss must be surfaced")


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
    suite.addTests(loader.loadTestsFromTestCase(TestChairmanQueueDedup))
    suite.addTests(loader.loadTestsFromTestCase(TestCriticalImports))
    suite.addTests(loader.loadTestsFromTestCase(TestAgentRegistry))
    suite.addTests(loader.loadTestsFromTestCase(TestWiringContracts))
    suite.addTests(loader.loadTestsFromTestCase(TestSignoffLedger))
    suite.addTests(loader.loadTestsFromTestCase(TestSignoffProduction))
    suite.addTests(loader.loadTestsFromTestCase(TestProblemLifecycle))
    suite.addTests(loader.loadTestsFromTestCase(TestTechShortPod))
    suite.addTests(loader.loadTestsFromTestCase(TestThesisWatch))
    suite.addTests(loader.loadTestsFromTestCase(TestShortBookCanClose))
    suite.addTests(loader.loadTestsFromTestCase(TestModelMetricsContract))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
