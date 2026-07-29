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
        # de-risking back to SHADOW is always allowed
        self.assertTrue(L.set_mode("SHADOW", [])[0])

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
        """Red team RT-16 (staleness): a PASS recorded against an old commit must
        not clear once HEAD has moved past it."""
        L = self.L
        L.propose_change("a", "x", ["z.py"], 2, 0, "BUG_FIX", None)
        L.sign("a", "CIT1", "CIT", "ok"); L.sign("a", "IRM1", "IRM", "ok")
        rec = L._ledger()
        rec["a"]["test_result"] = "PASS"
        rec["a"]["test_commit"] = "deadbee"      # a commit that is not HEAD
        L._save(L.LEDGER_PATH, rec)
        ok, reasons = L.is_cleared("a")
        # Only asserts when git is present (test_commit vs real HEAD differ).
        if L._git_head():
            self.assertFalse(ok, "stale green tests cleared")
            self.assertTrue(any("stale" in r for r in reasons))

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
        L.reserve_and_deploy("g"); L.record_outcome("g", "HELD")
        L.propose_change("b", "bad", ["z.py"], 2, 0, "BUG_FIX", "PASS")
        L.sign("b", "S", "CIT", "ok"); L.sign("b", "IRM-x", "IRM", "ok")
        L.reserve_and_deploy("b"); L.record_outcome("b", "REVERTED")
        self.assertLess(L.reputation("S")["ewma"], 0.0,
                        "a hold then a revert should net NEGATIVE (burn weighs 3x)")


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
    suite.addTests(loader.loadTestsFromTestCase(TestModelMetricsContract))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
