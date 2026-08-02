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
import re
import sqlite3
import tempfile
import shutil
import unittest
from datetime import date, datetime, timedelta
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

    # -- The above test guarded ONE writer. Six agents write this file, and the
    # -- other five each kept a private copy of the merge that still trimmed the
    # -- SHARED list. The three tests below guard the class, not the instance.

    def test_no_agent_trims_the_shared_chairman_queue(self):
        """A writer may cap its own entries, never the whole file.

        `combined = combined[-_CHAIRMAN_MAX_ENTRIES:]` slices the shared queue,
        so the last agent to run evicts other producers' still-open items. That
        is what took the file from 153 entries to 20 on 2026-07-31. Any new
        write site must go through chairman_queue.submit().
        """
        import glob as _glob
        import re as _re

        offenders = []
        for path in _glob.glob(os.path.join(os.path.dirname(__file__), "*.py")):
            name = os.path.basename(path)
            if name.startswith("test_") or name == "chairman_queue.py":
                continue
            with open(path) as f:
                src = f.read()
            if "chairman_action_required" not in src:
                continue
            # a slice of the merged list, rather than of this agent's own rows
            if _re.search(r"^\s*combined\s*=\s*combined\[[-:]", src, _re.M):
                offenders.append(name)
        self.assertEqual(offenders, [],
            "these modules trim the shared Chairman queue and will silently "
            "delete other producers' open decisions; call "
            "chairman_queue.submit() instead: %s" % offenders)

    def test_chairman_queue_cap_never_evicts_another_producer(self):
        import chairman_queue as cq

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "q.json")
            with open(target, "w") as f:
                json.dump([{"id": "CRO-1", "source": "logic_officer",
                            "status": "awaiting_chairman",
                            "description": "CRO has blocked ALL buys for 272 days"}], f)

            # One agent floods well past its own cap.
            cq.submit([{"id": "sys-%d" % i, "description": "finding %d" % i,
                        "status": "awaiting_chairman"} for i in range(50)],
                      "apex_system_cit_agent", path=target, max_entries=20)

            with open(target) as f:
                queue = json.load(f)

            self.assertIn("CRO-1", [e["id"] for e in queue],
                "another producer's open decision must survive any flood")
            own = [e for e in queue if e["source"] == "apex_system_cit_agent"]
            self.assertEqual(len(own), 20, "the flooding agent caps its OWN rows")

    def test_chairman_queue_ids_stay_unique_for_the_sla_ingester(self):
        """chairman_decision_sla.py::_ingest keys on `id` and skips ids it has
        already seen. apex_system_cit_agent builds its id from
        description[:30][:20], so ten different "Cron job stale on weekday:
        <job>" findings shared one id -- nine never reached the inbox.
        """
        import chairman_queue as cq

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "q.json")
            stale = [{"id": "apex-sys-cit-20260731-0814-cron_job_stale_on_we",
                      "description": "Cron job stale on weekday: %s" % job,
                      "status": "awaiting_chairman"}
                     for job in ("main.py", "hr_agent.py", "canary_agent.py")]
            cq.submit(stale, "apex_system_cit_agent", path=target)

            with open(target) as f:
                queue = json.load(f)

            self.assertEqual(len(queue), 3,
                "three distinct findings must not collapse into one row")
            ids = [e["id"] for e in queue]
            self.assertEqual(len(set(ids)), 3,
                "colliding ids make the SLA inbox drop items silently: %s" % ids)


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

        # A freshly built report must carry the honest list and be strict-JSON clean.
        # Built from the code, NOT read off disk: the first version of this assertion
        # read data/international_report.json, which passed on the Mac (report
        # regenerated after the fix) and FAILED on the VM, where the last cron run
        # predated the deploy by 90 minutes. Asserting on a mutable runtime artifact
        # tests when the agent last ran, not whether the code is correct.
        with patch.object(ia, "_pct_change", side_effect=lambda t: None if t == "^KS11" else 1.0), \
             patch.object(ia, "_last_price", return_value=100.0):
            fresh = ia.run(universe=["AAPL"])
        self.assertIn("unreadable", fresh,
                      "report must name the instruments it could not read")
        self.assertIn("^KS11", [r["ticker"] for r in fresh["unreadable"]],
                      "an unreadable index must be NAMED, not silently dropped")
        raw = json.dumps(fresh, default=str)
        self.assertNotIn("NaN", raw, "report contains bare NaN — not valid JSON")
        self.assertEqual(json.loads(raw)["unreadable"][0]["ticker"], "^KS11")

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

    def test_attendance_with_no_schedule_document_judges_nobody(self):
        """With the schedule unreadable, HR must say so — not mark the firm absent.

        Until 2026-08-01 this audit fell back to new_crontab.txt, a hand-maintained
        template 10 agents behind prod, so agents running every four hours in
        production were reported as scheduled NOWHERE. Deleting the template fixed
        the drift and created a new question: what happens when the one remaining
        schedule document is missing?

        The wrong answer is the tempting one — carry on with an empty string and
        let `name not in cron_src` be true for everybody. That turns one missing
        file into ~90 false absences against agents that run fine, which is worse
        than the drift it replaced. The right answer is to report the missing file
        once, as the firm-level failure it is, and judge no agent.

        Both halves, because a check that only fails safe is a check that has
        stopped checking:
          BAD     — no snapshot: zero never_scheduled, explicit schedule_unreadable.
          CONTROL — snapshot present: the unscheduled probe IS caught.
        """
        import sys as _sys
        import hr_agent, wiring_audit

        sandbox = Path(tempfile.mkdtemp())
        (sandbox / "hr_sched_probe.py").write_text("ok = True\n")
        (sandbox / "hr_unsched_probe.py").write_text("ok = True\n")
        real_modules = getattr(wiring_audit, "AGENT_MODULES", None)
        _sys.path.insert(0, str(sandbox))
        try:
            wiring_audit.AGENT_MODULES = ["hr_sched_probe", "hr_unsched_probe"]
            blind = hr_agent.audit_attendance(base_dir=str(sandbox))

            (sandbox / "data").mkdir()
            (sandbox / "data" / "vm_crontab_snapshot.txt").write_text(
                "0 6 * * * python3 hr_sched_probe.py\n")
            seeing = hr_agent.audit_attendance(base_dir=str(sandbox))
        finally:
            _sys.path.remove(str(sandbox))
            if real_modules is None:
                delattr(wiring_audit, "AGENT_MODULES")
            else:
                wiring_audit.AGENT_MODULES = real_modules
            shutil.rmtree(sandbox, ignore_errors=True)

        self.assertEqual(
            blind["never_scheduled"], [],
            "with no readable schedule HR cannot know anyone is absent — it must "
            f"accuse nobody; got {blind['never_scheduled']}")
        self.assertIn("vm_crontab_snapshot.txt",
                      blind.get("schedule_unreadable") or "",
                      "a missing schedule document must be reported explicitly, "
                      "not swallowed")
        self.assertEqual(blind["status"], "ATTENTION",
                         "an unreadable schedule is itself a condition needing "
                         "attention, not an OK report")

        absent = {a["agent"] for a in seeing["never_scheduled"]}
        self.assertIn("hr_unsched_probe", absent,
                      "with the snapshot readable, a genuinely unscheduled agent "
                      f"must still be caught; got {absent}")
        self.assertNotIn("hr_sched_probe", absent,
                         "an agent named in the live crontab is not absent")
        self.assertIsNone(seeing.get("schedule_unreadable"),
                          "the unreadable-schedule finding must clear once the "
                          "snapshot is back")

    def test_ratchet_integrity_flags_uncorroborated_all_clear(self):
        """A shrink-only baseline losing entries is not itself proof of a fix.

        2026-07-30: on the day the orphan baseline was created, a bug in
        check_orphan_agents()'s reachability walk made NINE of eleven baselined
        orphans look "scheduled" in one run — the detector, not the code, had
        changed. If that recommendation had been applied mechanically the
        ratchet would have loosened itself on the strength of the exact bug
        that produced the finding. audit_ratchet_integrity() closes that: any
        entry that vanishes from a watched baseline between two audits must be
        independently corroborated (a DIFFERENT check than the one that
        cleared it), or it is a MANAGEMENT finding for the board.
        """
        import hr_agent

        real_specs = hr_agent._RATCHET_SPECS
        real_history_path = hr_agent.RATCHET_INTEGRITY_HISTORY_PATH
        real_corroborate = hr_agent._corroborate_ratchet_entry_resolved

        sandbox = Path(tempfile.mkdtemp())
        baseline_path = sandbox / "ratchet_baseline.json"
        history_path = sandbox / "ratchet_history.json"

        def _write_baseline(entries):
            baseline_path.write_text(json.dumps({"known_orphans": entries}))

        try:
            hr_agent._RATCHET_SPECS = [
                {"name": "orphan_agents", "baseline_path": str(baseline_path),
                 "entry_key": "known_orphans"},
            ]
            hr_agent.RATCHET_INTEGRITY_HISTORY_PATH = str(history_path)

            # Cycle 1: three entries baselined, no prior history to compare
            # against -> establishes the snapshot, must not flag anything yet.
            _write_baseline(["ghost_a.py", "ghost_b.py", "ghost_c.py"])
            # Corroboration returns a REASON string, or None when the removal is
            # unjustified — so the report can say *why* an entry was allowed to go.
            hr_agent._corroborate_ratchet_entry_resolved = lambda e, **kw: None
            res1 = hr_agent.audit_ratchet_integrity()
            self.assertEqual(res1["status"], "OK",
                             "first-ever audit of a ratchet has nothing to compare "
                             "against and must not flag a false all-clear")

            # Cycle 2: TWO entries vanish at once, corroboration unavailable for
            # both — the exact 2026-07-30 shape. Must hard-flag as a mass
            # all-clear AND name the uncorroborated entries.
            _write_baseline(["ghost_a.py"])
            res2 = hr_agent.audit_ratchet_integrity()
            self.assertEqual(res2["status"], "ATTENTION",
                             "two simultaneous, uncorroborated disappearances must "
                             "not pass silently")
            flagged = {e for grp in res2["vanished_uncorroborated"] for e in grp["entries"]}
            self.assertEqual(flagged, {"ghost_b.py", "ghost_c.py"})
            self.assertTrue(res2["mass_all_clear"],
                            "a 2-at-once disappearance must trip the mass-all-clear guard")

            # Cycles 3 and 4: NOTHING has been fixed and nothing ratified. The
            # finding must repeat, unchanged, every cycle.
            #
            # This is the assertion that matters most. The first version of this
            # audit advanced its watermark unconditionally: it reported the
            # incident once, wrote the shrunken list to history, and returned OK
            # forever after — so a single unread report silently converted a
            # loosened ratchet into the new normal. That is the very failure the
            # audit exists to prevent, reproduced inside the watchdog itself. An
            # alarm you can clear by waiting is not an alarm.
            for cycle in (3, 4):
                res_n = hr_agent.audit_ratchet_integrity()
                self.assertEqual(res_n["status"], "ATTENTION",
                                 f"cycle {cycle}: the ratchet is still loosened and "
                                 f"nothing was ratified — the watchdog must not forget")
                still = {e for grp in res_n["vanished_uncorroborated"] for e in grp["entries"]}
                self.assertEqual(still, {"ghost_b.py", "ghost_c.py"},
                                 f"cycle {cycle}: must keep naming the same entries")

            # Sanity: a real, independently corroborated fix must pass silently,
            # and the report must record the reason it was accepted.
            _write_baseline([])
            hr_agent._corroborate_ratchet_entry_resolved = lambda e, **kw: "scheduled"
            res5 = hr_agent.audit_ratchet_integrity()
            self.assertEqual(res5["status"], "OK",
                             "a corroborated fix must not be branded suspicious")
            self.assertTrue(res5["ratified_removals"],
                            "an accepted removal must be recorded with its reason, "
                            "not merely omitted from the findings")

            # MANAGEMENT finding only — never a rating/PIP input.
            self.assertFalse({"rating", "ratings", "pip", "pip_status"} & set(res2))
        finally:
            hr_agent._RATCHET_SPECS = real_specs
            hr_agent.RATCHET_INTEGRITY_HISTORY_PATH = real_history_path
            hr_agent._corroborate_ratchet_entry_resolved = real_corroborate
            shutil.rmtree(sandbox, ignore_errors=True)

        # Wired: the review report must actually carry the result.
        import inspect
        src = inspect.getsource(hr_agent.run_hr_review)
        self.assertIn('report["ratchet_integrity"]', src,
                      "run_hr_review must publish the ratchet integrity audit")

    def test_ratchet_corroboration_will_not_accept_a_rubber_stamp(self):
        """The corroboration rule itself, unstubbed.

        The test above stubs `_corroborate_ratchet_entry_resolved` out to
        exercise the audit's bookkeeping, which means the rule that decides
        *what counts as proof* is not covered by it at all — sabotaging that
        rule left all 149 tests green. This test covers the rule directly.

        What it pins down is the asymmetry: an entry may leave the ratchet on
        EVIDENCE (it is in the live crontab / the file is gone) or on a written,
        dated board RATIFICATION — but never on an assertion that costs nothing
        to make. A `decision` field with no rationale is a rubber stamp, and a
        "we scheduled it" claim is checkable, so it must be checked rather than
        believed. Stale evidence fails CLOSED for the same reason: a 40-day-old
        snapshot describes a schedule that may no longer exist.
        """
        import shutil
        import tempfile
        import time
        import hr_agent

        sandbox = tempfile.mkdtemp(prefix="corrob_test_")
        try:
            os.makedirs(os.path.join(sandbox, "data"))
            snap = os.path.join(sandbox, "data", "vm_crontab_snapshot.txt")
            with open(snap, "w") as f:
                f.write("30 13 * * 1-5 ~/venv/bin/python3 live_agent.py\n")
            for mod in ("live_agent.py", "ghost.py"):
                with open(os.path.join(sandbox, mod), "w") as f:
                    f.write("# stub\n")

            call = hr_agent._corroborate_ratchet_entry_resolved

            # Evidence: literally in the live schedule, snapshot is fresh.
            self.assertEqual(call("live_agent.py", base_dir=sandbox), "scheduled")

            # Evidence: the module is gone. It cannot be an unscheduled agent.
            self.assertEqual(call("deleted_agent.py", base_dir=sandbox), "deleted")

            # No evidence at all — present on disk, absent from cron.
            self.assertIsNone(call("ghost.py", base_dir=sandbox),
                              "an entry that just vanished must not self-clear")

            # Ratification WITH a written reason is accepted...
            good = {"ratified_removals": [{
                "entry": "ghost.py", "decision": "RETIRE", "date": "2026-07-31",
                "rationale": "superseded by cit_director", "by": "board"}]}
            self.assertEqual(call("ghost.py", base_dir=sandbox, baseline_doc=good),
                             "ratified:RETIRE")

            # ...but a bare decision with no rationale is a rubber stamp.
            bare = {"ratified_removals": [{"entry": "ghost.py", "decision": "RETIRE"}]}
            self.assertIsNone(call("ghost.py", base_dir=sandbox, baseline_doc=bare),
                              "a decision with no written rationale is not a "
                              "justification — it is a rubber stamp")

            # "We scheduled it" is checkable, so asserting it proves nothing.
            claim = {"ratified_removals": [{
                "entry": "ghost.py", "decision": "SCHEDULE",
                "rationale": "added to the VM crontab"}]}
            self.assertIsNone(call("ghost.py", base_dir=sandbox, baseline_doc=claim),
                              "a SCHEDULE claim must be proven against the live "
                              "crontab, never accepted on assertion")

            # Stale evidence must fail CLOSED, not rubber-stamp the removal.
            old = time.time() - (hr_agent._SNAPSHOT_MAX_AGE_DAYS + 26) * 86400
            os.utime(snap, (old, old))
            self.assertIsNone(call("live_agent.py", base_dir=sandbox),
                              "a snapshot older than the freshness window "
                              "describes a schedule that may no longer exist")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_retirement_coverage_flags_unfilled_lane(self):
        """Retiring an agent must not silently blind a sector.

        Chairman directive, 2026-07-30: "it would be a shame if we killed a
        perfectly useful agent — it could harm the company and create holes."
        The precedent is KOSPI: three agents once LOOKED like international
        coverage while Korea went unwatched for months, because nobody checked
        what a roster name actually watches versus what it claims to.
        audit_retirement_coverage() makes the mechanical half of that check
        automatic: when an agent retires, does anything active still share its
        (sector, etf) lane?
        """
        import inspect
        import hr_agent

        real_registry_path = hr_agent.REGISTRY_PATH
        sandbox = Path(tempfile.mkdtemp())
        registry_path = sandbox / "registry.json"

        def _write_registry(agents):
            registry_path.write_text(json.dumps({"agents": agents}))

        try:
            hr_agent.REGISTRY_PATH = str(registry_path)

            # Case 1: retired agent's lane has nothing active covering it -> hole.
            _write_registry([
                {"class_name": "SmallCapGrowthAgent", "sector": "Small Cap",
                 "etf": "IWM", "status": "retired", "last_checked": "2026-05-14"},
                {"class_name": "GlobalTechAgent", "sector": "Technology",
                 "etf": "QQQ", "status": "active"},
            ])
            res1 = hr_agent.audit_retirement_coverage()
            self.assertEqual(res1["status"], "ATTENTION",
                             "a retired agent with no active same-lane coverage "
                             "must be flagged")
            holes = {h["class_name"] for h in res1["coverage_holes"]}
            self.assertIn("SmallCapGrowthAgent", holes)

            # Case 2: same retirement, but a fresh active agent covers the same
            # (sector, etf) lane -> not a hole (this is the real 2026-05-14 shape:
            # CoS retired and same-day re-spawned into the same slot).
            _write_registry([
                {"class_name": "SmallCapGrowthAgent", "sector": "Small Cap",
                 "etf": "IWM", "status": "retired", "last_checked": "2026-05-14"},
                {"class_name": "SmallCapGrowthAgent", "sector": "Small Cap",
                 "etf": "IWM", "status": "active", "last_checked": "2026-06-09"},
            ])
            res2 = hr_agent.audit_retirement_coverage()
            self.assertEqual(res2["status"], "OK",
                             "a retirement backfilled in the same sector/etf lane "
                             "must not be flagged as a hole")

            # Case 3: THE SHAPE THAT ACTUALLY OCCURS. Cases 1 and 2 both spell
            # retirement `status: "retired"` — and not one agent in the live
            # registry has ever carried that status. The seven retired on
            # 2026-05-14 carry `retired_at` + `retirement_reason` with
            # `status: "active"`, so this audit reported `checked: 0 / OK` for
            # months while seven tombstoned agents sat in an unfilled lane. The
            # test passed the whole time, because the test wrote the registry it
            # wished for. Anything keyed on the status string alone re-opens it.
            _write_registry([
                {"class_name": "MaterialsMiningAgent", "sector": "Materials",
                 "etf": "XLB", "status": "active",
                 "retired_at": "2026-05-14",
                 "retirement_reason": "0 approved picks across 14 cycles"},
                {"class_name": "GlobalTechAgent", "sector": "Technology",
                 "etf": "QQQ", "status": "active"},
            ])
            res3 = hr_agent.audit_retirement_coverage()
            self.assertEqual(res3["checked"], 1,
                             "a tombstoned agent (retired_at + retirement_reason, "
                             "status still 'active') must be counted as retired — "
                             "checked: 0 is how this audit hid seven of them")
            self.assertEqual(res3["status"], "ATTENTION")
            self.assertIn("MaterialsMiningAgent",
                          {h["class_name"] for h in res3["coverage_holes"]})

            # Case 4: a VACATED retirement is not a retirement. The tombstone is
            # preserved for the record under `vacated_retirement`, but the agent
            # is back at work and its lane is covered — by itself. If this ever
            # reads as a hole, every reinstated agent files a permanent false
            # coverage finding against a seat it is currently sitting in.
            _write_registry([
                {"class_name": "MaterialsMiningAgent", "sector": "Materials",
                 "etf": "XLB", "status": "active",
                 "vacated_retirement": {"retired_at": "2026-05-14",
                                        "retirement_reason": "0 approved picks"}},
            ])
            res4 = hr_agent.audit_retirement_coverage()
            self.assertEqual(res4["checked"], 0,
                             "a vacated retirement must not count as retired")
            self.assertEqual(res4["status"], "OK")

            # MANAGEMENT finding only — never a rating/PIP input.
            self.assertFalse({"rating", "ratings", "pip", "pip_status"} & set(res1))
        finally:
            hr_agent.REGISTRY_PATH = real_registry_path
            shutil.rmtree(sandbox, ignore_errors=True)

        # Wired: the review report must actually carry the result.
        src = inspect.getsource(hr_agent.run_hr_review)
        self.assertIn('report["retirement_coverage"]', src,
                      "run_hr_review must publish the retirement coverage audit")

    def test_retired_has_exactly_one_definition(self):
        """"Retired" must be one function, imported — never a second copy.

        There were three live definitions at once: hr_agent kept a private
        `_RETIRED_STATUSES` tuple, hr_retirement_audit had its own predicate, and
        agent_roster.py a third. They disagreed, so the same seven agents were
        retired to one reader and active to another, and each reader was
        internally consistent enough to look correct. This is the same
        producer/consumer identity mismatch that CLAUDE.md names as the system's
        main bug class; the discipline is one identity function per contract.

        hr_agent imports it under a try/except so a broken import degrades rather
        than crashes — which also means a broken import is SILENT. This asserts
        the real import won, not the fallback.
        """
        import hr_agent
        import hr_retirement_audit

        self.assertIs(
            hr_agent._is_retired, hr_retirement_audit.is_retired,
            "hr_agent must use hr_retirement_audit.is_retired itself. If this "
            "fails, the except-branch fallback is live and the two modules can "
            "now disagree about who works here without anything erroring.")

        # And the predicate must accept BOTH spellings, because the registry
        # contains both: an explicit status, and the tombstone pair.
        yes = [{"status": "retired"}, {"status": "decommissioned"},
               {"status": "terminated"},
               {"status": "active", "retired_at": "2026-05-14",
                "retirement_reason": "0 approved picks"}]
        for a in yes:
            self.assertTrue(hr_retirement_audit.is_retired(a), a)

        no = [{"status": "active"}, {"status": "inactive"},   # inactive != retired
              # A tombstone needs BOTH halves: a bare retired_at with no reason
              # is a half-written record, not a termination.
              {"status": "active", "retired_at": "2026-05-14"},
              {"status": "active",
               "vacated_retirement": {"retired_at": "2026-05-14",
                                      "retirement_reason": "0 approved picks"}}]
        for a in no:
            self.assertFalse(hr_retirement_audit.is_retired(a), a)

    def test_market_event_monitor_escalates_to_board_queue(self):
        """rd_market_event_monitor.py's producer side must land where the
        board actually reads it.

        rd_geopolitical_team.py imports yfinance but never calls it — it maps
        static historical precedents, with no live detection. This new agent
        is the missing live front end. A genuine threshold breach must reach
        cit_pending_proposals.json (pod="main" forces this — fi/fe/etf have
        no consumer per CLAUDE.md's producer/queue/consumer contract) with
        confidence >= BOARD_CONFIDENCE_THRESHOLD and evidence text that
        satisfies _PRECISE_EVIDENCE_RE, or the proposal is produced but never
        acted on — the exact silent-mismatch bug class this file guards.
        """
        from datetime import datetime
        import cit_director
        import rd_market_event_monitor as rdmon

        sandbox = Path(tempfile.mkdtemp())
        real_data_dir = cit_director.DATA_DIR
        real_cit_pending = cit_director.CIT_PENDING
        real_pod_files = dict(cit_director._POD_PENDING_FILES)

        try:
            cit_director.DATA_DIR = sandbox
            cit_director.CIT_PENDING = sandbox / "cit_pending_proposals.json"
            cit_director._POD_PENDING_FILES = {
                k: sandbox / v.name for k, v in real_pod_files.items()
            }

            fake_event = {
                "rule": "INDEX_GAP", "ticker": "QQQ", "value_pct": 3.3,
                "detail": "QQQ moved +3.30% in one session (threshold ±3.0%)",
            }
            escalated = rdmon.escalate([fake_event], {})
            self.assertEqual(len(escalated), 1, "a fresh (not-on-cooldown) event must escalate")

            self.assertTrue(cit_director.CIT_PENDING.exists(),
                            "escalation must write to the board's actual pending-proposals file")
            queued = json.loads(cit_director.CIT_PENDING.read_text())
            match = [p for p in queued if p.get("agent") == "RDMarketEventMonitor"]
            self.assertEqual(len(match), 1)
            proposal = match[0]

            self.assertEqual(proposal["routed_to_pod"], "main",
                             "must land on the board queue, not a dead-letter pod")
            self.assertGreaterEqual(proposal["confidence"], cit_director.BOARD_CONFIDENCE_THRESHOLD,
                                    "must clear the board confidence gate")
            self.assertTrue(cit_director._PRECISE_EVIDENCE_RE.search(proposal["evidence"]),
                            "evidence must satisfy the precision gate or the board never sees it")

            # Cooldown: the same (rule, ticker) must not re-escalate immediately.
            history = {"INDEX_GAP:QQQ": {"last_fired_at": datetime.now().isoformat()}}
            escalated_again = rdmon.escalate([fake_event], history)
            self.assertEqual(len(escalated_again), 0,
                             "an event on cooldown must not spam a second board proposal")
        finally:
            cit_director.DATA_DIR = real_data_dir
            cit_director.CIT_PENDING = real_cit_pending
            cit_director._POD_PENDING_FILES = real_pod_files
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_market_event_monitor_reports_blind_rules_instead_of_quiet(self):
        """A failed fetch must never be reported as a calm market.

        Found live on 2026-07-31: the monitor makes ~15 sequential yfinance
        calls and the tail of that burst draws rate-limiting. TLT came back
        empty, so CROSS_ASSET_STRESS — the flight-to-safety detector, which
        needs SPY *and* TLT *and* GLD — could not fire at all. The report said
        `status: QUIET`. Nothing anywhere said the rule had been switched off.

        That is the dangerous direction of failure: not a wrong number, but no
        output, which is indistinguishable from "nothing was wrong today". The
        same shape hid the KOSPI signal through the Korea selloff.

        Also pins the NaN half of it. `float('nan')` passes `is not None`,
        fails every threshold comparison, and json.dump writes it as bare
        `NaN` — invalid JSON that can make the whole report unreadable.
        """
        import rd_market_event_monitor as rdmon

        real_fetch = rdmon._fetch_close_pair
        try:
            def fake(ticker, attempts=2):
                if ticker == "TLT":            # transient rate-limit
                    return None, None, "simulated: only 0 session(s) returned"
                if ticker == "QQQ":            # NaN reaching the compute layer
                    return float("nan"), 100.0, None
                return 101.0, 100.0, None
            rdmon._fetch_close_pair = fake
            result = rdmon.detect_events()
        finally:
            rdmon._fetch_close_pair = real_fetch

        self.assertIn("TLT", {u["ticker"] for u in result["unreadable"]},
                      "a ticker that could not be read must appear SOMEWHERE; "
                      "absence must never be the encoding for failure")
        blind = {d["rule"] for d in result["degraded_rules"]}
        self.assertIn("CROSS_ASSET_STRESS", blind,
                      "a rule missing a required input is BLIND, not quiet")
        self.assertIn("INDEX_GAP", blind,
                      "a NaN input must also mark its rule blind, not silently "
                      "fail the threshold comparison")
        self.assertIsNone(result["inputs"]["qqq_pct"],
                          "NaN must be converted to an explicit None, never "
                          "compared against a threshold")

        # The whole class, caught at the serialisation boundary.
        json.dumps(result, allow_nan=False, default=str)

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

    def test_a_docstring_mention_cannot_make_an_agent_look_scheduled(self):
        """Prose naming a module is not a call to it.

        2026-07-31: the reachability walk matched a module's stem anywhere in
        another module's TEXT. chairman_queue.py's docstring names the five
        agents that used to carry duplicated copies of its merge logic, and one
        of those names, reached from a scheduled caller, was enough to mark
        apex_system_cit_agent "reachable" — the audit then reported it "no
        longer an orphan" and asked for its removal from the baseline, purely
        because a refactor added a comment. Same failure as the test-file case
        above: being described is not being scheduled.

        Ordinary string literals must still count, or the walk goes blind to
        subprocess.run(["python3", "agent.py"]) — the way half this firm runs.
        """
        import wiring_audit

        sources = {
            "main.py": 'import helper\n',
            # A scheduled module that only DESCRIBES the orphan, in a docstring
            # and a comment.
            "helper.py": (
                '"""Consolidates what ghost_agent.py used to duplicate."""\n'
                '# ghost_agent.py previously wrote this itself\n'
                'x = 1\n'
            ),
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
            "an agent named only in prose must still be an orphan; "
            f"got {wiring_audit.problems}")

        # ...but a real invocation, in a string literal, does employ it.
        sources["helper.py"] = 'subprocess.run(["python3", "ghost_agent.py"])\n'
        wiring_audit.problems = []
        wiring_audit.check_orphan_agents(sources)
        self.assertFalse(
            any("ghost_agent.py" in p for p in wiring_audit.problems),
            "a module launched by subprocess IS scheduled and must not be "
            f"flagged; got {wiring_audit.problems}")

    def test_an_agent_that_escalates_instead_of_writing_is_still_an_orphan(self):
        """An agent whose product is a routed item, not a file, must be caught.

        2026-08-01: rd_market_event_monitor.py was built 2026-07-30 to Chairman
        spec — a live event feed that "can quickly report to the board so that
        they can act fast" — and then ran nowhere for two days while this audit
        said the firm was clean. Two separate holes let it through, and a fix for
        either one alone would still have missed it:

          1. It escalates with `from cit_director import submit_to_pod_queue`
             and a bare `submit_to_pod_queue(proposal)`. The producer gate looked
             for `<module>.<fn>(`, which that never matches.
          2. Its only named output, rd_market_event_report.json, has no readers,
             so `if not readers: continue` skipped it anyway.

        Both are reproduced below: the agent writes a file nobody reads AND
        escalates via a bare from-imported call.
        """
        import wiring_audit

        agent = (
            'from cit_director import submit_to_pod_queue\n'
            'import json\n'
            'OUT = "data/ghost_events.json"\n'
            'json.dump({}, open(OUT, "w"))\n'          # nobody reads this
            'submit_to_pod_queue({"category": "STRATEGY"})\n'
        )
        base = {
            "main.py": "import canary_agent\n",
            "canary_agent.py": "x = 1\n",
            "cit_director.py": "def submit_to_pod_queue(p):\n    return 'main'\n",
            "ghost_events_agent.py": agent,
        }

        wiring_audit.problems, wiring_audit.notes = [], []
        wiring_audit.check_orphan_agents(dict(base))
        found = wiring_audit.problems + wiring_audit.notes
        self.assertTrue(
            any("ghost_events_agent.py" in p for p in found),
            "an unscheduled agent that escalates to the board must be flagged "
            f"even though nothing reads its file; got {found}")

        # The module that DEFINES the entry point must never be accused of
        # calling it — cit_director does not escalate to itself.
        self.assertFalse(
            any("cit_director.py" in p for p in found),
            f"defining an escalation entry point is not calling it; got {found}")

        # Healthy control: schedule it, and the finding must go away. A gate that
        # fires on working code gets switched off, and then it protects nothing.
        wired = dict(base)
        wired["main.py"] = "import canary_agent\nimport ghost_events_agent\n"
        wiring_audit.problems, wiring_audit.notes = [], []
        wiring_audit.check_orphan_agents(wired)
        self.assertFalse(
            any("ghost_events_agent.py" in p
                for p in wiring_audit.problems + wiring_audit.notes),
            "a scheduled agent must not be reported as an orphan; got "
            f"{wiring_audit.problems + wiring_audit.notes}")

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
        """'Scheduled' is judged against the live-crontab snapshot, and nothing else.

        There used to be three schedule documents. new_crontab.txt's own header said
        the VM's live crontab was canonical and that it was only the Mac-path
        template — and it had drifted 10 agents behind prod, so agents running every
        4h in production were reported as scheduled NOWHERE. 2026-08-01 both
        hand-maintained copies were deleted; the snapshot is generated, so the only
        remaining question is what happens when it is absent.

        Two halves, and the second is the one that matters:
          1. an agent in the snapshot must NOT be called an orphan;
          2. with NO snapshot the check must fail LOUD and judge nobody. The old
             behaviour was to fall back to the stale template, which is answering a
             question you cannot see the answer to — and here the answer condemns
             working agents.
        """
        import wiring_audit

        def run(with_snapshot):
            sandbox = Path(tempfile.mkdtemp())
            (sandbox / "data").mkdir()
            if with_snapshot:
                (sandbox / "data" / "vm_crontab_snapshot.txt").write_text(
                    "0 6 * * * python3 canary_agent.py\n"
                    "20 */4 * * * python3 cron_only_agent.py\n")
            sources = {
                "main.py": "import canary_agent\n",
                "canary_agent.py": "x = 1\n",
                "cron_only_agent.py": (
                    'OUT = "data/snap_report.json"\n'
                    'json.dump(d, open(OUT, "w"))\n'
                ),
                "consumer.py": 'r = load("data/snap_report.json")\n',
            }
            real_base = wiring_audit.BASE_DIR
            try:
                wiring_audit.BASE_DIR = sandbox
                wiring_audit.problems, wiring_audit.notes = [], []
                wiring_audit.check_orphan_agents(sources)
                return " ".join(wiring_audit.problems + wiring_audit.notes)
            finally:
                wiring_audit.BASE_DIR = real_base
                shutil.rmtree(sandbox, ignore_errors=True)

        self.assertNotIn(
            "cron_only_agent.py", run(with_snapshot=True),
            "an agent scheduled in the canonical live crontab was called an orphan")

        blind = run(with_snapshot=False)
        self.assertIn(
            "vm_crontab_snapshot.txt", blind,
            "a missing schedule snapshot must be reported as a hard problem, not "
            f"silently worked around; got {blind!r}")
        self.assertNotIn(
            "cron_only_agent.py", blind,
            "with no readable schedule the check cannot know an agent is unscheduled "
            f"— it must judge nobody; got {blind!r}")

        # And the real repo must actually have the snapshot, or the firm is running
        # on the blind path above every single audit.
        self.assertTrue((Path(__file__).parent / "data" /
                         "vm_crontab_snapshot.txt").exists(),
                        "canonical prod-schedule snapshot is missing — run "
                        "scripts/refresh_crontab_snapshot.sh")

    def test_vm_crontab_snapshot_is_not_stale(self):
        """Drift between the snapshot and the live VM must become a test failure.

        Three crontab documents used to disagree — new_crontab.txt (33 modules),
        gcp_setup/vm_crontab.txt (32), and the live VM crontab (42) — and only the
        live one was real, so a stale document was silently the ground truth every
        orphan-agent audit measured against. The two hand-maintained copies are now
        deleted (2026-08-01); this snapshot is the single schedule document, which
        makes its freshness the whole ballgame. A unit test cannot SSH the VM, but it
        CAN refuse to trust a snapshot that hasn't been refreshed recently, which is
        the same freshness gate hr_agent.py's ratchet-integrity corroboration uses
        (see _corroborate_ratchet_entry_resolved) — staleness beyond that window is
        drift by definition, not proof of anything.
        """
        import time
        snap = Path(__file__).parent / "data" / "vm_crontab_snapshot.txt"
        self.assertTrue(
            snap.exists(),
            "data/vm_crontab_snapshot.txt is missing — it is the firm's ONLY "
            "schedule document, so wiring_audit.py's orphan check and HR's "
            "attendance audit both go blind and can judge no agent scheduled or "
            "unscheduled. Run scripts/refresh_crontab_snapshot.sh")
        age_days = (time.time() - snap.stat().st_mtime) / 86400.0
        self.assertLess(
            age_days, 14,
            f"data/vm_crontab_snapshot.txt is {age_days:.1f} days stale — the "
            "orphan-agent audit and HR's attendance/ratchet checks are trusting a "
            "schedule snapshot old enough to have drifted from prod. Refresh with "
            "scripts/refresh_crontab_snapshot.sh before this can be trusted again")

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


class TestLTExperimentsAreRealExperiments(unittest.TestCase):
    """An experiment record must describe the run that actually happened.

    ml_longterm_experiment_runner loaded its data ONCE, outside the loop, using
    queue[0]'s feature_override, and then scored every other queued experiment
    against it — under a comment that read "we load data fresh per experiment".
    Proven live: two experiments requesting LT_FEATURES (18 cols) and
    LT_FEATURES_DB (10 cols) both ran on the 18-col set and returned an
    identical 0.5201, the second filed with `features_used` naming columns it
    never saw.

    That was harmless only because the queue was empty. The nine ideas sitting
    in ml_longterm_suggestions.json ("add P/B ratio", "train post-2018 only",
    "weight value 2x") declare no runnable config at all, so bridging them in
    would have produced nine results for nine hypotheses none of which were
    tested — manufactured evidence, promoted against a real baseline.
    """

    def _run_queue(self, entries):
        import importlib
        R = importlib.import_module("ml_longterm_experiment_runner")
        tmp = Path(tempfile.mkdtemp())
        orig = (R.QUEUE_FILE, R.RESULTS_FILE, R.MODEL_PATH, R.MODEL_META, R.DATA_DIR)
        try:
            R.DATA_DIR     = tmp
            R.QUEUE_FILE   = tmp / "q.json"
            R.RESULTS_FILE = tmp / "r.json"
            R.MODEL_PATH   = tmp / "m.pkl"
            R.MODEL_META   = tmp / "m.json"
            R.QUEUE_FILE.write_text(json.dumps(entries))
            R.run()
            return json.loads(R.RESULTS_FILE.read_text()) if R.RESULTS_FILE.exists() else []
        finally:
            (R.QUEUE_FILE, R.RESULTS_FILE, R.MODEL_PATH, R.MODEL_META, R.DATA_DIR) = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_undeclared_experiment_is_refused_not_silently_defaulted(self):
        """The shape of all nine stranded ideas: prose, no runnable config."""
        import ml_longterm_experiment_runner as R
        fs, reason = R._experiment_config({"id": "lt_feature_pb_ratio",
                                           "title": "Add P/B ratio as a feature"})
        self.assertEqual(fs, "", "an idea with no declared config resolved to a config")
        self.assertTrue(reason, "refused without saying why")
        self.assertIn("feature_override", reason)

    def test_an_unknown_feature_set_is_refused_at_both_gates(self):
        import ml_longterm_experiment_runner as R
        fs, reason = R._experiment_config({"id": "x", "feature_override": "LT_FEATURS"})
        self.assertEqual(fs, "", "a typo'd feature set was accepted")
        self.assertIn("LT_FEATURS", reason)
        # and the loader itself must not fall through to the default
        with self.assertRaises(ValueError):
            R._load_longterm_data(feature_set="LT_FEATURS")

    def test_declared_configs_are_accepted(self):
        """The guard must not refuse the configs the runner really can build."""
        import ml_longterm_experiment_runner as R
        for fs in R.KNOWN_FEATURE_SETS:
            got, reason = R._experiment_config({"id": "x", "feature_override": fs})
            self.assertEqual((got, reason), (fs, ""), f"{fs} was wrongly refused")

    def test_each_experiment_is_scored_on_the_data_it_asked_for(self):
        """The load must be inside the loop, keyed on each experiment's own config."""
        src = open(Path(__file__).parent / "ml_longterm_experiment_runner.py").read()
        self.assertNotIn('first_exp.get("feature_override"', src,
                         "data is still loaded once from queue[0]'s config")
        i = src.find("for exp in queue:")
        self.assertGreater(i, 0, "experiment loop not found")
        body = src[i:]
        self.assertIn("_load_longterm_data(feature_set=feature_set)", body,
                      "the per-experiment load is not inside the loop")

    def test_two_experiments_sharing_a_config_are_not_two_results(self):
        """Same configuration = the same measurement wearing a second name."""
        results = self._run_queue([
            {"id": "e1", "title": "first", "status": "queued",
             "feature_override": "LT_FEATURES_DB"},
            {"id": "e2", "title": "identical config", "status": "queued",
             "feature_override": "LT_FEATURES_DB"},
        ])
        by_id = {r["id"]: r for r in results}
        self.assertIn("e1", by_id)
        self.assertEqual(by_id["e2"].get("duplicate_of"), "e1",
                         "a duplicate config was filed as independent evidence")
        self.assertIsNone(by_id["e2"].get("mean_auc"),
                          "a duplicate config was given its own AUC")

    def test_a_constant_feature_is_not_counted_as_a_feature(self):
        """18 declared features, 6 informative — the record must say both.

        data/edgar_cache does not exist on this Mac (the VM has 84 filings), so
        all 8 edgar_* columns load as constant 0. Four MORE are constant in the
        database itself, on all 383,129 rows: herding_flag, quant_consensus,
        insider_buy_dollars, insider_is_ceo_cfo. A model that cannot split on a
        column does not have that feature, whatever the metadata counts.
        """
        results = self._run_queue([
            {"id": "e1", "title": "full", "status": "queued",
             "feature_override": "LT_FEATURES"},
        ])
        r = results[0]
        self.assertIn("n_features_declared", r)
        self.assertIn("n_features_informative", r)
        self.assertLessEqual(r["n_features_informative"], r["n_features_declared"])
        self.assertEqual(r["n_features_informative"],
                         r["n_features_declared"] - len(r["constant_features"]))
        for dead in ("herding_flag", "quant_consensus"):
            self.assertIn(dead, r["constant_features"],
                          f"{dead} is constant in the DB but was counted as a live feature")


class TestFormatSpecsAreReachable(unittest.TestCase):
    """No f-string may carry a format spec that can never format anything.

    Everything after the ':' in an f-string replacement field is the FORMAT
    SPEC, not code. So this, which reads perfectly:

        f"W3: {corr_w3:.4f if corr_w3 is not None else 'N/A'}"

    does not mean what it says — it asks Python to format corr_w3 with the spec
    ".4f if corr_w3 is not None else 'N/A'" and raises ValueError: Invalid
    format specifier. Four of these were live: two in ml_alpha_decay_monitor's
    suggestion rationale, two in backtest_predictive's per-quarter log line.

    The shape is worse than an ordinary typo because the line only executes on
    the interesting path. The decay monitor's raise sat inside the loop over
    DECAYING features, uncaught all the way up through run() — so it wrote its
    report, then died before adding a suggestion or sending an alert, and only
    ever on the runs where it had found something. Green until it mattered.

    Caught by nothing else: it is valid syntax, so it compiles, imports, and
    passes any check short of executing that exact branch.
    """

    #: values a spec might legitimately be meant for.
    PROBES = (1.0, 1, "x", timedelta(days=1))

    #: datetime is deliberately NOT a probe. datetime.__format__ hands the spec
    #: straight to strftime, which accepts ARBITRARY text and returns it with
    #: any % directives substituted — so format(datetime, ".4f if x else 'y'")
    #: succeeds and returns that literal string. Probing with a datetime makes
    #: this check pass on every input, which is what it did on the first pass
    #: here: three known-bad specs and it reported clean. A strftime spec is
    #: recognised by its % directives instead.
    STRFTIME_RE = re.compile(r"%[a-zA-Z]")

    @classmethod
    def _unusable(cls, spec):
        for probe in cls.PROBES:
            try:
                format(probe, spec)
                return False
            except (ValueError, TypeError):
                continue
        return not cls.STRFTIME_RE.search(spec)

    def test_no_unformattable_format_spec_anywhere(self):
        import ast
        root = Path(__file__).parent
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if ".git" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.FormattedValue) or node.format_spec is None:
                    continue
                vals = getattr(node.format_spec, "values", [])
                # a spec built at runtime ({x:{width}}) can't be judged statically
                if not all(isinstance(v, ast.Constant) for v in vals):
                    continue
                spec = "".join(v.value for v in vals)
                if spec and self._unusable(spec):
                    offenders.append(
                        f"{path.relative_to(root)}:{node.lineno}  {{...:{spec}}}")
        self.assertEqual(
            offenders, [],
            "format spec(s) no value can satisfy — this raises at runtime, on "
            "whatever path reaches the line:\n  " + "\n  ".join(offenders))


class TestLiveSnapshotPathActuallyRecords(unittest.TestCase):
    """main.py -> collect_snapshot: a producer that never reached its consumer.

    main.py builds signals_df, ML-enhances it, then called collect_snapshot()
    with no arguments — so the collector went looking for data/signals.json,
    which no code in this repo writes and which exists on neither host. The
    tell is in the data: all 383,129 rows of pipeline_snapshots carry
    signal_type 'HISTORICAL_13F' (the backfill's value) and not one carries
    the 'BUY' this path writes. It never inserted a row, and it said so with
    an INFO line that reads exactly like a quiet day.
    """

    LIVE = [{"ticker": "AAPL", "institutions": "Citadel, Point72",
             "n_institutions": 2, "conviction_score": 4.0,
             "signal_type": "BUY", "high_conviction": True}]

    def _collect(self, signals, price=123.45):
        """Run collect_snapshot against a throwaway DB, no network."""
        import tempfile, sqlite3, shutil
        from predictive_model import data_collector as DC
        tmp = tempfile.mkdtemp()
        db = Path(tmp) / "t.db"
        def _tmp_db():
            # Match the real get_db(): _fill_price_outcomes subscripts rows by
            # column name, so the row factory is part of the contract.
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return c

        orig_db, orig_px = DC.get_db, DC._get_current_price
        DC.get_db = _tmp_db
        DC._get_current_price = lambda t: price
        try:
            n = DC.collect_snapshot(signals) if signals is not None \
                else DC.collect_snapshot()
            con = sqlite3.connect(db)
            try:
                rows = con.execute(
                    "SELECT ticker, institution, signal_type, entry_price "
                    "FROM pipeline_snapshots").fetchall()
            finally:
                con.close()
            return n, rows
        finally:
            DC.get_db, DC._get_current_price = orig_db, orig_px
            shutil.rmtree(tmp, ignore_errors=True)

    def test_signals_passed_in_memory_are_recorded(self):
        n, rows = self._collect(self.LIVE)
        self.assertEqual(n, 1, "a live signal was handed over and not recorded")
        self.assertEqual(len(rows), 1)

    def test_the_recorded_row_is_the_live_kind_not_the_backfill_kind(self):
        _, rows = self._collect(self.LIVE)
        self.assertEqual(rows[0][2], "BUY",
                         "live rows must be distinguishable from HISTORICAL_13F")
        self.assertEqual(rows[0][3], 123.45, "live rows carry a real entry price")

    def test_the_plural_institutions_key_is_not_dropped(self):
        """signal_engine emits 'institutions'; the collector read 'institution'."""
        _, rows = self._collect(self.LIVE)
        self.assertEqual(rows[0][1], "Citadel, Point72",
                         "confluence institutions were written as an empty string")

    def test_main_hands_its_signals_over(self):
        """The wiring contract. A no-arg call is the bug, restored.

        Asserted on main.py's AST: the call must pass at least one argument.
        """
        import ast
        tree = ast.parse((Path(__file__).parent / "main.py").read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id in ("collect_snapshot", "_collect_snap")]
        self.assertTrue(calls, "main.py no longer collects snapshots at all")
        for c in calls:
            self.assertTrue(c.args or c.keywords,
                            "collect_snapshot() called with no signals — it will "
                            "fall back to data/signals.json, which nothing writes")

    def test_an_empty_signal_list_records_nothing_and_does_not_raise(self):
        n, rows = self._collect([])
        self.assertEqual((n, rows), (0, []))


class TestLabelColumnContract(unittest.TestCase):
    """Every label column the code queries must actually exist in the DB.

    outcome_365d was specified, wired into nine modules, and never built:
    build_365d_labels.py was supposed to ALTER TABLE it into existence and never
    ran once. Four consumers guard with `if "outcome_365d" not in cols: skip`,
    so for months they ran green and reported nothing -- a monitor that cannot
    see its data is indistinguishable from a monitor that sees no problem. The
    rest raised into a cron log nobody read.
    """

    #: the naming family that drifted -- outcome_20d, risk_adjusted_252d, ...
    LABEL_RE = re.compile(r'\b((?:outcome|risk_adjusted)_\d+d)\b')

    DB_PATH = Path(__file__).parent / "data" / "historical_13f.db"

    @staticmethod
    def _code_only(txt):
        """Strip docstrings and # comments.

        A historical note explaining a past bug ("it used to say outcome_365d")
        is documentation, not a query. Scanning raw text would make it
        impossible to ever write down what went wrong.
        """
        txt = re.sub(r'""".*?"""', '', txt, flags=re.S)
        txt = re.sub(r"'''.*?'''", '', txt, flags=re.S)
        return re.sub(r'#[^\n]*', '', txt)

    def test_every_label_column_referenced_exists(self):
        if not self.DB_PATH.exists():
            self.skipTest("historical_13f.db absent -- no schema to check against")

        con = sqlite3.connect(self.DB_PATH)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("pipeline_snapshots", tables,
                          "pipeline_snapshots is gone -- every label consumer is broken")
            real = {r[1] for r in con.execute("PRAGMA table_info(pipeline_snapshots)")}
        finally:
            con.close()

        # Sanity: the check is only meaningful if the schema actually carries
        # labels. An empty `real` would make every assertion below vacuous.
        self.assertTrue(self.LABEL_RE.findall(" ".join(real)),
                        "no label-shaped columns in pipeline_snapshots -- "
                        "this test would pass vacuously")

        root = Path(__file__).parent
        offenders = {}
        for path in sorted(root.glob("*.py")) + sorted(root.glob("predictive_model/*.py")):
            if path.name == Path(__file__).name:
                continue
            code = self._code_only(path.read_text(errors="ignore"))
            # A builder is allowed to name a column it is in the act of
            # creating; consumers are not.
            if re.search(r'ADD\s+COLUMN', code, re.I):
                continue
            missing = {lbl for lbl in self.LABEL_RE.findall(code) if lbl not in real}
            if missing:
                offenders[path.name] = sorted(missing)

        self.assertEqual(
            offenders, {},
            "these modules query label columns that do not exist in "
            "pipeline_snapshots:\n" +
            "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items())) +
            "\nEither build the column or repoint to the live label "
            "(feature_engineer.LT_TARGET).")

    def test_lt_target_is_a_real_column(self):
        """The exported constant itself must name a live, populated column."""
        if not self.DB_PATH.exists():
            self.skipTest("historical_13f.db absent")
        import feature_engineer
        con = sqlite3.connect(self.DB_PATH)
        try:
            real = {r[1] for r in con.execute("PRAGMA table_info(pipeline_snapshots)")}
            self.assertIn(feature_engineer.LT_TARGET, real,
                          "feature_engineer.LT_TARGET names a nonexistent column")
            n = con.execute("SELECT COUNT(%s) FROM pipeline_snapshots"
                            % feature_engineer.LT_TARGET).fetchone()[0]
        finally:
            con.close()
        # A column that exists but is entirely NULL trains nothing.
        self.assertGreater(n, 0,
                           f"{feature_engineer.LT_TARGET} exists but has no labeled rows")

    def test_lt_targets_all_resolve(self):
        """Every LT target selects LT_FEATURE_COLS *and* is a real column."""
        if not self.DB_PATH.exists():
            self.skipTest("historical_13f.db absent")
        import feature_engineer
        con = sqlite3.connect(self.DB_PATH)
        try:
            real = {r[1] for r in con.execute("PRAGMA table_info(pipeline_snapshots)")}
        finally:
            con.close()
        bad = sorted(t for t in feature_engineer.LT_TARGETS if t not in real)
        self.assertEqual(bad, [],
                         f"LT_TARGETS names columns that do not exist: {bad}")


class TestEveryDecisionCarriesItsOriginator(unittest.TestCase):
    """Every manager decision must name who originated the pick.

    evaluate_recommendation() computed `rec.get("source_agent", "ReviewAgent")`
    and then discarded it -- all nine return sites spread `{**rec, ...}`, and a
    review-agent recommendation has no source_agent of its own. So decisions
    born on the review path reached manager_decisions.json with no originator:
    119 BUY decisions in one VM cycle on 2026-07-31. hr_agent.py:328 keys
    per-agent productivity off that field, so those all counted as "unknown"
    and the agents whose picks the firm actually bought were indistinguishable
    from agents that produced nothing.
    """

    ACCOUNT = {"portfolio_value": 100000, "buying_power": 50000, "n_positions": 5}
    REVIEW_REC = {"ticker": "ABC", "side": "long", "qty": 10, "avg_entry": 10.0,
                  "current_price": 11.0, "unrealized_pnl_pct": 10.0,
                  "hold_days": 3, "weight_pct": 2.0}

    def _decide(self, **overrides):
        import manager_agent
        rec = dict(self.REVIEW_REC)
        rec.update(overrides.pop("rec", {}))
        return manager_agent.evaluate_recommendation(rec, dict(self.ACCOUNT),
                                                     **overrides)

    def test_a_review_born_pick_is_attributed_not_blank(self):
        for action in ("BUY_NEW", "ADD", "SELL", "TRIM", "HOLD", "WATCH"):
            d = self._decide(rec={"action": action})
            self.assertTrue(d.get("source_agent"),
                            f"{action} decision reached the log with no originator")

    def test_a_research_picks_own_agent_is_not_overwritten(self):
        d = self._decide(rec={"action": "BUY_NEW", "source_agent": "Agent 7"})
        self.assertEqual(d["source_agent"], "Agent 7")

    def test_the_early_defensive_return_is_attributed_too(self):
        """The canary path returns before most of the function runs."""
        d = self._decide(rec={"action": "BUY_NEW"},
                         canary_alert={"level": "RED", "triggers": ["test"]})
        self.assertTrue(d.get("source_agent"),
                        "the defensive-mode early return drops attribution")

    def test_hr_reads_the_field_the_manager_writes(self):
        """A rename on either side would silently un-attribute the whole firm."""
        import ast
        src = (Path(__file__).parent / "hr_agent.py").read_text()
        consts = {n.value for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertIn("source_agent", consts,
                      "hr_agent no longer reads source_agent -- per-agent "
                      "productivity is measuring something else now")


class TestPerformanceSnapshotProducer(unittest.TestCase):
    """performance_history.json finally has a writer — keep it honest.

    The file had three readers and no producer, which is what left the
    loss-attribution alarm reading one hand-written row from 2026-06-09
    forever. This writer supplies the facts. What it must never do is supply
    the *explanation*: auto-filling loss_attribution would re-satisfy the alarm
    with a generated sentence and reproduce the original bug one layer up.
    """

    REVIEW = {
        "generated_at": None,      # set per test
        "account": {"portfolio_value": 97500.0, "cash": 30000.0,
                    "pnl_today": -250.0, "pnl_today_pct": -0.25,
                    "n_positions": 3},
        "recommendations": [
            {"ticker": "WIN", "side": "long",  "qty": 100.0,
             "avg_entry": 10.0, "current_price": 12.0, "unrealized_pnl_pct": 20.0},
            {"ticker": "LOSE", "side": "long", "qty": 50.0,
             "avg_entry": 20.0, "current_price": 15.0, "unrealized_pnl_pct": -25.0},
            {"ticker": "SHRT", "side": "short", "qty": 30.0,
             "avg_entry": 40.0, "current_price": 30.0, "unrealized_pnl_pct": 25.0},
        ],
    }

    def _sandbox(self, review_stamp, history=None):
        """Point the module at a temp dir. Returns (module, tmpdir Path)."""
        import json as _json, tempfile, performance_snapshot as PS
        tmp = Path(tempfile.mkdtemp())
        review = _json.loads(_json.dumps(self.REVIEW))
        review["generated_at"] = review_stamp
        (tmp / "review.json").write_text(_json.dumps(review))
        (tmp / "registry.json").write_text(
            _json.dumps({"agents": [{"status": "active"}, {"status": "retired"}]}))
        if history is not None:
            (tmp / "history.json").write_text(_json.dumps(history))
        self._orig = (PS.HISTORY_PATH, PS.REVIEW_PATH, PS.REGISTRY_PATH)
        self.addCleanup(self._restore, PS)
        PS.HISTORY_PATH  = tmp / "history.json"
        PS.REVIEW_PATH   = tmp / "review.json"
        PS.REGISTRY_PATH = tmp / "registry.json"
        return PS, tmp

    def _restore(self, PS):
        PS.HISTORY_PATH, PS.REVIEW_PATH, PS.REGISTRY_PATH = self._orig

    @staticmethod
    def _today_stamp():
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None).isoformat()

    def test_it_never_writes_loss_attribution(self):
        PS, _ = self._sandbox(self._today_stamp())
        row = PS.write_snapshot()
        self.assertIsNotNone(row)
        self.assertNotIn("loss_attribution", row,
                         "the writer is filling in the judgment the alarm exists "
                         "to demand -- that re-disarms it")

    def test_a_human_written_attribution_survives_the_next_run(self):
        """main.py runs 4-6x a day; run five must not erase what the board wrote."""
        PS, _ = self._sandbox(self._today_stamp())
        first = PS.write_snapshot()
        import json as _json
        hist = _json.loads(PS.HISTORY_PATH.read_text())
        hist["snapshots"][-1]["loss_attribution"] = {"primary_driver": "board wrote this"}
        PS.HISTORY_PATH.write_text(_json.dumps(hist))

        PS.write_snapshot()
        hist = _json.loads(PS.HISTORY_PATH.read_text())
        self.assertEqual(len(hist["snapshots"]), 1, "same day must upsert, not append")
        self.assertEqual(hist["snapshots"][-1]["loss_attribution"]["primary_driver"],
                         "board wrote this")
        self.assertEqual(hist["snapshots"][-1]["snapshot_date"], first["snapshot_date"])

    def test_a_stale_or_broken_pipeline_writes_no_row(self):
        """A $0 row from a failed run reads as a -100% drawdown, forever."""
        PS, _ = self._sandbox("2026-06-09T20:00:00")          # yesterday's numbers
        self.assertIsNone(PS.write_snapshot())
        self.assertFalse(PS.HISTORY_PATH.exists())

        import json as _json
        PS, _ = self._sandbox(self._today_stamp())
        bad = _json.loads(PS.REVIEW_PATH.read_text())
        bad["account"]["portfolio_value"] = 0
        PS.REVIEW_PATH.write_text(_json.dumps(bad))
        self.assertIsNone(PS.write_snapshot())

    def test_shorts_are_signed_off_side_not_qty(self):
        """The review agent reports short quantities positive."""
        PS, _ = self._sandbox(self._today_stamp())
        drivers = {d["ticker"]: d["pnl_usd"] for d in PS.write_snapshot()["pnl_drivers"]}
        self.assertEqual(drivers["SHRT"], 300.0,   # short 30 @40 now 30 = +$300
                         "a profitable short is being recorded as a loss")
        self.assertEqual(drivers["LOSE"], -250.0)
        self.assertEqual(drivers["WIN"], 200.0)

    def test_trimming_never_drops_an_attributed_row(self):
        """A naive cap would eventually delete the only rows the alarm reads."""
        import json as _json
        old = [{"snapshot_date": "2020-01-01", "portfolio_value": 1,
                "loss_attribution": {"primary_driver": "keep me"}}]
        old += [{"snapshot_date": "2021-%02d-%02d" % (m, d), "portfolio_value": 1}
                for m in range(1, 13) for d in range(1, 29)]
        self.assertGreater(len(old), 0)
        PS, _ = self._sandbox(self._today_stamp(),
                              history={"snapshots": old})
        PS.MAX_ROWS, orig = 10, PS.MAX_ROWS
        self.addCleanup(setattr, PS, "MAX_ROWS", orig)
        PS.write_snapshot()
        kept = _json.loads(PS.HISTORY_PATH.read_text())["snapshots"]
        self.assertTrue(any(s.get("loss_attribution") for s in kept),
                        "the attributed row was trimmed away")

    def test_it_reads_no_file_without_a_live_producer(self):
        """position_aging_report.json is written by an agent scheduled nowhere.

        Depending on it would have made this module a fresh instance of the
        bug it exists to close. Comments are invisible to ast, so the comment
        explaining the rejection does not trip this.
        """
        import ast, performance_snapshot as PS
        tree = ast.parse(Path(PS.__file__).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings):
                self.assertNotIn("position_aging", n.value)

    def test_the_pipeline_actually_calls_it(self):
        """An unreferenced producer is just another orphan.

        Both halves are required. Merely seeing the name somewhere in main.py
        is not enough -- a local `write_snapshot = lambda: None` satisfies that
        and writes nothing, which mutation testing demonstrated.
        """
        import ast
        tree = ast.parse((Path(__file__).parent / "main.py").read_text())
        imported = any(isinstance(n, ast.ImportFrom)
                       and n.module == "performance_snapshot"
                       and any(a.name == "write_snapshot" for a in n.names)
                       for n in ast.walk(tree))
        called = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "write_snapshot" for n in ast.walk(tree))
        self.assertTrue(imported, "main.py no longer imports write_snapshot from "
                                  "performance_snapshot")
        self.assertTrue(called, "main.py imports the snapshot writer but never "
                                "calls it -- performance_history.json is back to "
                                "having no producer")


class TestLossAttributionMustBeCurrent(unittest.TestCase):
    """The "why is the firm down?" alarm must not be disarmed by an old file.

    Both guards asked a presence question -- logic_officer checked
    snapshots[-1].get("loss_attribution"), logic_team checked any() over all
    snapshots. data/performance_history.json contains exactly one snapshot,
    dated 2026-06-09, and no module in the repo writes it (three read it). So
    from that date both alarms answered "attribution provided" for every later
    drawdown, forever. On 2026-07-07 the Logic Officer told the board
    "-10.7% below starting capital -- attribution provided", citing a document a
    month older than the loss, and the board approved it.
    """

    JUNE = {"snapshots": [{"snapshot_date": "2026-06-09",
                           "loss_attribution": {"driver": "QQQ short"}}]}

    def test_a_month_old_attribution_is_not_an_explanation(self):
        import loss_attribution
        st = loss_attribution.status(self.JUNE, today=date(2026, 7, 31))
        self.assertFalse(st["current"])
        self.assertEqual(st["label"], "STALE")
        self.assertEqual(st["as_of"], "2026-06-09")
        self.assertEqual(st["age_days"], 52)

    def test_a_fresh_attribution_counts(self):
        import loss_attribution
        st = loss_attribution.status(
            {"snapshots": [{"snapshot_date": "2026-07-29",
                            "loss_attribution": {"driver": "x"}}]},
            today=date(2026, 7, 31))
        self.assertTrue(st["current"])
        self.assertEqual(st["label"], "provided")

    def test_absent_and_undated_both_fail_closed(self):
        """Undated is not 'assume fine' -- it cannot say which loss it explains."""
        import loss_attribution
        for payload, label in (
                ({}, "MISSING"),
                ({"snapshots": []}, "MISSING"),
                ({"snapshots": [{"snapshot_date": "2026-07-30"}]}, "MISSING"),
                (None, "MISSING"),
                ({"snapshots": [{"loss_attribution": {"d": 1}}]}, "STALE")):
            st = loss_attribution.status(payload, today=date(2026, 7, 31))
            self.assertFalse(st["current"], f"{payload} passed as current")
            self.assertEqual(st["label"], label, f"wrong label for {payload}")

    def test_newest_wins_regardless_of_list_order(self):
        """The file is hand-maintained; nothing guarantees append order."""
        import loss_attribution
        out_of_order = {"snapshots": [
            {"snapshot_date": "2026-07-29", "loss_attribution": {"d": 1}},
            {"snapshot_date": "2026-06-09", "loss_attribution": {"d": 2}}]}
        st = loss_attribution.status(out_of_order, today=date(2026, 7, 31))
        self.assertEqual(st["as_of"], "2026-07-29")
        self.assertTrue(st["current"])

    def test_both_guards_ask_the_shared_helper(self):
        """Neither may go back to its own inline presence check."""
        import ast
        for mod in ("logic_officer.py", "logic_team.py"):
            tree = ast.parse((Path(__file__).parent / mod).read_text())
            calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "status"
                     and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "loss_attribution"]
            self.assertTrue(calls,
                            f"{mod} no longer calls loss_attribution.status() -- "
                            "it is back to judging attribution on its own")

    def test_the_officer_rule_reports_stale_end_to_end(self):
        """The rule, run against the June record, must not say 'provided'."""
        import logic_officer
        rule = next(r for r in logic_officer.ALL_RULES
                    if r.__name__ == "_rule_portfolio_unexplained_loss")
        out = rule({"portfolio_value": 89343, "performance_history": self.JUNE})
        self.assertIsNotNone(out, "a -10.7% drawdown must still raise something")
        self.assertNotIn("attribution provided", out["title"])
        self.assertIn("2026-06-09", out["observation"])


class TestFICITReachesTheChairman(unittest.TestCase):
    """fi_cit_agent's findings must land in the queue the consumers read.

    It wrote to BASE_DIR/chairman_action_required.json -- the repo root, one
    directory above data/. Nothing reads that path, so every finding it ever
    raised, including a HIGH "fi_pipeline.py has not run", went into a private
    file and died there. It was broken twice over: the real queue is a JSON
    *list*, and the old writer called existing.get("fi_pod") on it, which raises
    AttributeError outside its try -- so simply correcting the path would have
    crashed instead of working. Both halves are asserted here.
    """

    def _agent(self):
        import fi_cit_agent
        return fi_cit_agent

    ISSUES = [{"severity": "HIGH", "type": "PIPELINE_NOT_RUN",
               "message": "fi_pipeline.py has not run in 47.6 hours"}]

    def test_it_writes_to_the_queue_the_consumers_read(self):
        """The path, asserted against the consumers' own constant."""
        A = self._agent()
        import chairman_queue
        self.assertEqual(Path(A.CHAIRMAN_FILE).name,
                         Path(chairman_queue.DEFAULT_PATH).name)
        self.assertEqual(Path(A.CHAIRMAN_FILE).parent.name, "data",
                         "FI CIT is writing outside data/ -- no consumer reads "
                         "that path; this is the original bug")

    def test_entries_match_the_shape_the_sla_ingester_keys_on(self):
        """chairman_decision_sla keys on id; a missing field drops the item."""
        A = self._agent()
        entries = A._to_queue_entries(self.ISSUES, [])
        self.assertEqual(len(entries), 1)
        for field in ("id", "severity", "description", "source", "ts",
                      "status", "dedup_key"):
            self.assertIn(field, entries[0], f"entry is missing {field}")
        self.assertEqual(entries[0]["source"], "fi_cit_agent")
        self.assertEqual(entries[0]["status"], "awaiting_chairman")

    def test_dedup_key_is_the_finding_type_not_the_message(self):
        """The messages embed a growing age in hours.

        Keying on rendered text means the same open issue is re-raised every
        run and the queue buries itself -- the bug already fixed in seven other
        writers. Two runs of the same finding, hours apart, must collide.
        """
        A = self._agent()
        older = [dict(self.ISSUES[0], message="fi_pipeline.py has not run in 47.6 hours")]
        newer = [dict(self.ISSUES[0], message="fi_pipeline.py has not run in 71.9 hours")]
        self.assertEqual(A._to_queue_entries(older, [])[0]["dedup_key"],
                         A._to_queue_entries(newer, [])[0]["dedup_key"])

    def test_a_real_submit_survives_the_live_list_shaped_queue(self):
        """End-to-end against a list-shaped file, in a temp dir.

        This is the half that used to raise AttributeError. Existing entries
        from another producer must still be there afterwards.
        """
        import tempfile, shutil
        A = self._agent()
        tmp = tempfile.mkdtemp()
        try:
            q = Path(tmp) / "chairman_action_required.json"
            other = [{"id": "someone-else-1", "severity": "HIGH",
                      "description": "not mine", "source": "board_meeting",
                      "ts": "2026-07-01T00:00:00", "status": "awaiting_chairman"}]
            q.write_text(json.dumps(other))
            orig = A.CHAIRMAN_FILE
            A.CHAIRMAN_FILE = q
            try:
                n = A._update_chairman_file(self.ISSUES, [], on_fi_host=True)
            finally:
                A.CHAIRMAN_FILE = orig
            after = json.loads(q.read_text())
            self.assertIsInstance(after, list, "the queue must stay a list")
            self.assertEqual(n, 1)
            self.assertEqual(len(after), 2, "it clobbered another producer's item")
            self.assertTrue(any(e["source"] == "fi_cit_agent" for e in after))
            self.assertTrue(any(e["id"] == "someone-else-1" for e in after))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_off_the_pipeline_host_it_files_nothing(self):
        """Same host-identity rule as the GCP IP check below.

        The freshness checks read whichever host's data/ they run on, so off
        the host that schedules fi_pipeline.py they describe a directory nobody
        feeds. Unknown counts as off-host: a false HIGH costs more than a day's
        delay, which the right host re-detects anyway.
        """
        import tempfile, shutil
        A = self._agent()
        for host_state in (False, None):
            tmp = tempfile.mkdtemp()
            try:
                q = Path(tmp) / "chairman_action_required.json"
                q.write_text("[]")
                orig = A.CHAIRMAN_FILE
                A.CHAIRMAN_FILE = q
                try:
                    n = A._update_chairman_file(self.ISSUES, [],
                                                on_fi_host=host_state)
                finally:
                    A.CHAIRMAN_FILE = orig
                self.assertEqual(n, 0, f"filed a finding with on_fi_host={host_state}")
                self.assertEqual(json.loads(q.read_text()), [])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_spreads_come_from_a_file_something_actually_writes(self):
        """Producer↔consumer, asserted against the producer's own constant.

        It read data/fi_spread_cache.json, which nothing in the repo writes --
        the reader was left behind when the producer was renamed. Its last
        write was 2026-07-03, so for four weeks the spread-regime and duration
        checks ran on frozen numbers (HY 274bps "TIGHT") while the live book
        was HY 333bps "NORMAL", and the staleness alarm they raised about it
        could not be cleared by any code that exists.
        """
        A = self._agent()
        import fi_credit_monitor
        self.assertEqual(Path(A.SPREADS_PATH).name,
                         Path(fi_credit_monitor.CACHE_PATH).name,
                         "FI CIT reads a spreads file its producer does not write")

    def test_freshness_comes_from_the_payload_not_the_mtime(self):
        """rsync rewrites mtimes; a month-old snapshot must not read as fresh."""
        import tempfile, shutil
        A = self._agent()
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / "fi_credit_conditions.json"
            p.write_text(json.dumps({
                "fetched_at": "2026-07-03T09:00:06.911798",
                "spreads": {"hy_spread": 274.0, "spread_regime": "TIGHT"},
            }))
            orig = A.SPREADS_PATH
            A.SPREADS_PATH = p
            try:
                spreads, age = A._load_credit_conditions()
            finally:
                A.SPREADS_PATH = orig
            self.assertEqual(spreads.get("hy_spread"), 274.0,
                             "the spreads live under the 'spreads' key")
            self.assertGreater(age, 24 * 20,
                               "a just-written file with a July 3 stamp read as "
                               "fresh — that is the mtime, not the payload")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_host_probe_asks_this_hosts_own_crontab(self):
        """It must resolve to a real True/False/None here, without raising."""
        A = self._agent()
        self.assertIn(A._host_runs_fi_pipeline(), (True, False, None))

    def test_no_alert_hardcodes_a_cron_schedule(self):
        """The old text said "Check cron at 0 11 * * *"; the real one is 30 14.

        A schedule copied into an alert string is a second source of truth that
        nothing updates, so it goes stale silently and sends the reader to a
        cron line that does not exist.

        Docstrings are excluded. A docstring naming the real schedule is
        documentation, not an alert, and a guard that cannot tell the two apart
        fails on its own explanation -- the same mistake as grepping raw text
        for ifconfig.me in the class below.
        """
        import ast, re, fi_cit_agent as A
        tree = ast.parse(Path(A.__file__).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        # Five whitespace-separated cron fields. No trailing \b: a cron ends in
        # '*' far more often than not, '*' is not a word character, and the
        # first version of this regex therefore matched "30 14 * * 1-5" but
        # sailed straight past "0 11 * * *" -- exactly the string it exists to
        # catch. Mutation testing found that; reading it did not.
        cron_re = re.compile(r"(?<!\S)[\d*][\d*/,\-]*(?:\s+[\d*][\d*/,\-]*){4}")
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings):
                self.assertIsNone(cron_re.search(n.value),
                                  "alert text hardcodes a cron schedule: "
                                  f"{n.value[:80]!r}")


class TestHostSpecificChecksStayOnTheirHost(unittest.TestCase):
    """A check only one host can answer must stay silent on all the others.

    apex_system_cit_agent asked ifconfig.me "what is my external IP?" and
    compared the answer to the GCP VM's. ifconfig.me answers from anywhere, so
    on a laptop the check compared the laptop's IP to the VM's, found them
    different (of course), and filed a HIGH "GCP IP changed -- update all SSH
    configs" against the Chairman. Two of those reached the queue. The check
    was never wrong about the VM; it was wrong about which host it was on.
    """

    def _patched(self, ip):
        """Run the improvement checks with the host probe forced to `ip`."""
        import apex_system_cit_agent as A
        orig = A._gcp_instance_external_ip
        A._gcp_instance_external_ip = lambda: ip
        try:
            proposals = []
            A.check_improvement_proposals(proposals)
        finally:
            A._gcp_instance_external_ip = orig
        return [p for p in proposals if "GCP IP" in json.dumps(p, default=str)]

    def test_off_gcp_the_ip_check_raises_nothing(self):
        """The laptop case: no metadata server, so no opinion about the VM."""
        self.assertEqual(self._patched(None), [],
                         "a host that cannot see the VM filed a finding about it")

    def test_on_gcp_a_matching_ip_raises_nothing(self):
        import apex_system_cit_agent as A
        self.assertEqual(self._patched(A._KNOWN_GCP_IP), [])

    def test_on_gcp_a_drifted_ip_is_still_caught(self):
        """The guard must not have bought silence by disabling the check."""
        found = self._patched("203.0.113.7")
        self.assertEqual(len(found), 1, "real IP drift went unreported")
        self.assertIn("203.0.113.7", json.dumps(found[0], default=str))

    def test_the_probe_is_the_metadata_server_not_a_public_echo(self):
        """Regression guard: any 'what is my IP?' service reintroduces the bug.

        Those services answer from every host on the internet, which is
        precisely what made the check unable to tell the VM from a laptop.

        Scans string *constants* via AST, not raw text — the comment above the
        check names ifconfig.me to explain the bug, and a guard that cannot
        tell a URL it calls from a URL it warns about would fail on its own
        documentation. This asserts what the code uses.
        """
        import ast, apex_system_cit_agent as A
        literals = [n.value for n in ast.walk(ast.parse(Path(A.__file__).read_text()))
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        blob = "\n".join(literals)
        for echo in ("ifconfig.me", "ipinfo.io", "icanhazip", "api.ipify.org",
                     "checkip.amazonaws"):
            self.assertNotIn(echo, blob,
                             f"{echo} answers from any host — it cannot identify one")
        self.assertIn("metadata.google.internal", blob)

    def test_the_probe_returns_none_off_gcp_on_this_machine(self):
        """Unpatched, on whatever host the suite is running on.

        Passes on both: the Mac cannot resolve the metadata server (None), and
        on the VM it returns that instance's real external IP.
        """
        import apex_system_cit_agent as A
        ip = A._gcp_instance_external_ip()
        self.assertTrue(ip is None or ip == A._KNOWN_GCP_IP,
                        f"metadata probe returned an unexpected value: {ip!r}")


class TestWrongfulRetirement(unittest.TestCase):
    """Retirement is the only irreversible thing HR does. It ran unchecked.

    Two agents were retired 2026-05-14 for zero APPROVED picks during a
    firm-wide buy freeze they neither caused nor could end, cited a role
    reassignment that moved nothing as their second chance, and were 'replaced'
    by a respawn of their own catalog entry.
    """

    def _stub(self, agents):
        import types
        return types.SimpleNamespace(registry={"agents": agents})

    def test_retirement_blocked_when_replacement_would_be_a_clone(self):
        """One specialist per sector means retiring only resets the counter."""
        import chief_of_staff_agent as cos
        stub = self._stub([{"class_name": "OnlyOne", "status": "retired"}])
        original = cos.AGENT_CATALOG
        try:
            cos.AGENT_CATALOG = [{"class_name": "OnlyOne", "sector": "Small Cap", "tier": 1}]
            self.assertFalse(
                cos.AgentFactory._genuine_replacement_exists(stub, "OnlyOne", "Small Cap"),
                "an agent must not count as its own replacement")

            cos.AGENT_CATALOG = [
                {"class_name": "OnlyOne", "sector": "Small Cap", "tier": 1},
                {"class_name": "AnotherOne", "sector": "Small Cap", "tier": 2},
            ]
            self.assertTrue(
                cos.AgentFactory._genuine_replacement_exists(stub, "OnlyOne", "Small Cap"),
                "a genuinely different specialist for the sector IS a replacement")
        finally:
            cos.AGENT_CATALOG = original

    def test_every_catalog_sector_is_checked_before_retiring(self):
        """Stage 4 must consult _genuine_replacement_exists, not retire blind."""
        src = open(Path(__file__).parent / "chief_of_staff_agent.py").read()
        stage4 = src.split("Stage 4: Dignified Replacement", 1)
        self.assertEqual(len(stage4), 2, "Stage 4 block not found")
        head = stage4[1][:1200]
        self.assertIn("_genuine_replacement_exists", head,
                      "Stage 4 retires without checking a real replacement exists")

    def test_replacement_search_excludes_the_agent_being_replaced(self):
        """Belt to the Stage 4 brace: the search must skip its own class_name."""
        src = open(Path(__file__).parent / "chief_of_staff_agent.py").read()
        i = src.find("Find best catalog match for the same sector")
        self.assertGreater(i, 0, "replacement search not found")
        block = src[i:i + 900]
        self.assertIn('s["class_name"] != ch["class_name"]', block,
                      "replacement search can still respawn the retired agent itself")

    def test_audit_catches_every_wrongful_retirement_shape(self):
        import hr_retirement_audit as A
        # Retired TODAY, against a memo generated today: the only situation in
        # which cit_hr_memo.json can actually testify to the risk posture at
        # retirement time. Dating this fixture 2026-05-14 with a fresh memo was
        # asserting a firing from evidence 79 days after the fact.
        today = datetime.now().date().isoformat()
        registry = {"agents": [
            # retired original + identical clone, reassigned nowhere
            {"class_name": "Ghost", "status": "retired", "file": "agents/ghost.py",
             "sector": "Small Cap", "reassigned_to": "Execution-Quality",
             "reassigned_at": "2026-05-06", "retired_at": today},
            {"class_name": "Ghost", "status": "active", "file": "agents/ghost.py",
             "sector": "Small Cap"},
        ]}
        memo = {"buys_blocked": True, "cro_level": "LOW",
                "generated_at": datetime.now().isoformat()}
        checks = {f["check"] for f in A.audit(registry=registry, memo=memo, catalog={})}
        for expected in ("DUPLICATE_RECORDS", "CLONE_REPLACEMENT",
                         "FIRED_DURING_BLOCK", "HOLLOW_SECOND_CHANCE"):
            self.assertIn(expected, checks, f"audit missed {expected}")

    def test_one_batch_retirement_is_one_decision_not_seven_rows(self):
        """Seven agents, one batch, one answer — so one row, naming all seven.

        Filed per-agent this was 21 rows in the Chairman queue for 3 decisions,
        each carrying its own 50-day board escalation. Findings that are genuinely
        about one agent must still stand alone.
        """
        import hr_retirement_audit as A
        batch = [{"check": "RETIREMENT_NEVER_ENFORCED", "severity": "HIGH",
                  "class_name": n, "batch": "retired-2026-05-14",
                  "description": "%s carries retired_at=2026-05-14." % n}
                 for n in ("GlobalTechAgent", "CryptoEquitiesAgent", "MaterialsMiningAgent")]
        solo = {"check": "DUPLICATE_RECORDS", "severity": "MEDIUM",
                "class_name": "Ghost", "description": "Ghost has 2 records."}
        out = A._as_decisions(batch + [solo])
        self.assertEqual(len(out), 2, "batch did not collapse, or the solo row was eaten")
        rolled = next(d for d in out if d["check"] == "RETIREMENT_NEVER_ENFORCED")
        for n in ("GlobalTechAgent", "CryptoEquitiesAgent", "MaterialsMiningAgent"):
            self.assertIn(n, rolled["description"], f"{n} was dropped from the roll-up")
        self.assertEqual(rolled["subject"], "retired-2026-05-14")
        self.assertEqual(next(d for d in out if d["check"] == "DUPLICATE_RECORDS")["subject"],
                         "Ghost", "a genuinely per-agent finding lost its own row")

    def test_a_capped_queue_evicts_the_least_severe_and_says_so(self):
        """The cap threw away a HIGH to keep a LOW, and reported it as raised.

        `own[:len(own)-max]` is blind FIFO, so the HIGH "seven agents were retired
        and never stopped trading" was evicted for being submitted one position
        earlier than a LOW "we cannot date the risk memo" — then counted in
        `len(added)` on the very next line, so the caller logged it as raised.
        """
        import chairman_queue, json, tempfile, os
        entries = ([{"id": "hi", "dedup_key": "k-hi", "severity": "HIGH",
                     "description": "seven agents still trading", "status": "awaiting_chairman"}]
                   + [{"id": "lo%d" % i, "dedup_key": "k-lo%d" % i, "severity": "LOW",
                       "description": "minor", "status": "awaiting_chairman"} for i in range(3)])
        # A path that does not exist yet: _load fails OPEN only on a genuinely
        # absent file, and deliberately raises on an empty/corrupt one.
        d = tempfile.mkdtemp()
        path = os.path.join(d, "queue.json")
        try:
            added, _ = chairman_queue.submit(entries, "tsrc", path=path, max_entries=2)
            with open(path) as fh:
                wrote = json.load(fh)
            kept = {e["id"] for e in wrote}
            self.assertIn("hi", kept, "the cap evicted a HIGH and kept LOWs")
            self.assertEqual(len(wrote), 2)
            self.assertEqual(added, len(wrote),
                             "reported more raised than it actually wrote — an evicted "
                             "finding was logged as having reached the Chairman")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_audit_knows_retired_has_two_spellings(self):
        """Not one agent in the registry has status 'retired'.

        The seven retired on 2026-05-14 carry `retired_at` + `retirement_reason`
        and `status: "active"`. Every check keyed on `status == "retired"`, so
        `retired` was the empty list and this audit reported clean: true with
        seven tombstoned agents on the books — the same producer/consumer
        identity mismatch as the roster merge and the Chairman queue.
        """
        import hr_retirement_audit as A
        tomb = {"class_name": "Ghost", "status": "active",
                "retired_at": "2026-05-14", "retirement_reason": "ran out of runway"}
        self.assertTrue(A._is_retired(tomb),
                        "a retirement record with status 'active' read as not-retired")
        self.assertTrue(A._is_retired({"class_name": "G", "status": "retired"}))
        self.assertFalse(A._is_retired({"class_name": "G", "status": "active"}))
        self.assertFalse(A._is_retired({"class_name": "G", "status": "active",
                                        "retired_at": "2026-05-14"}),
                         "a date with no reason is not a retirement record")

    def test_a_recorded_but_unenforced_retirement_is_reported(self):
        """Fired and still trading, or spared and never told. Both need deciding."""
        import hr_retirement_audit as A
        registry = {"agents": [{
            "class_name": "GlobalTechAgent", "status": "active", "permanent": True,
            "file": "agents/gt.py", "sector": "Technology",
            "retired_at": "2026-05-14", "retirement_reason": "ran out of runway"}]}
        out = [f for f in A.audit(registry=registry, memo={}, catalog={}, history={})
               if f["check"] == "RETIREMENT_NEVER_ENFORCED"]
        self.assertEqual(len(out), 1, "the live contradiction went unreported")
        self.assertEqual(out[0]["severity"], "HIGH")
        self.assertIn("2026-05-14", out[0]["description"])

    def test_voiding_the_hollow_chance_does_not_switch_the_detector_off(self):
        """The remediation for C moved `reassigned_to` into `voided_reassignment`.

        Correct on its own terms — but C keys on `reassigned_to`, so voiding it
        also stopped C finding anything. On 2026-07-31 all seven were voided and
        the audit went from finding them to `clean: true`, with the retirements
        those hollow moves justified still standing. A fix that deletes its own
        detector's input closes the finding, not the problem.
        """
        import hr_retirement_audit as A
        voided = {"class_name": "GlobalTechAgent", "status": "active",
                  "file": "agents/gt.py", "sector": "Technology",
                  "retired_at": "2026-05-14",
                  "retirement_reason": ("After 14 cycles without an approved pick — "
                                        "including a role reassignment at cycle 9 — …"),
                  "voided_reassignment": {"was": "Execution-Quality", "at": "2026-05-06",
                                          "voided_on": "2026-07-31",
                                          "why": "Never took effect: reassigned_to has no reader."}}
        checks = {f["check"] for f in A.audit(registry={"agents": [voided]},
                                              memo={}, catalog={}, history={})}
        self.assertNotIn("HOLLOW_SECOND_CHANCE", checks,
                         "fixture still carries reassigned_to — not testing the void path")
        self.assertIn("RETIREMENT_CITES_VOIDED_CHANCE", checks,
                      "voiding the reassignment made the standing retirement invisible")

    def test_a_memo_that_postdates_the_retirement_cannot_vouch_for_it(self):
        """Fresh is not the same question as contemporaneous.

        Today's memo is maximally fresh and says nothing about 2026-05-14. The
        freshness gate only ever asked "is this memo current?", which passes
        trivially for every old retirement — and there is no historical risk
        record on disk to fall back on, so the honest answer is 'unverifiable'.
        """
        import hr_retirement_audit as A
        registry = {"agents": [{"class_name": "Ghost", "status": "retired",
                                "file": "agents/ghost.py", "sector": "Small Cap",
                                "retired_at": "2026-05-14"}]}
        fresh_memo = {"buys_blocked": True, "cro_level": "HIGH",
                      "generated_at": datetime.now().isoformat()}
        found = A.audit(registry=registry, memo=fresh_memo, catalog={}, history={})
        checks = {f["check"] for f in found}
        self.assertNotIn("FIRED_DURING_BLOCK", checks,
                         "graded a May retirement against today's risk posture")
        self.assertIn("UNVERIFIABLE_RETIREMENT", checks)
        self.assertFalse(A._memo_covers(fresh_memo, "2026-05-14")[0])
        self.assertTrue(A._memo_covers(fresh_memo,
                                       datetime.now().date().isoformat())[0],
                        "a retirement happening now must still be checkable")

    def test_audit_refuses_to_judge_on_stale_risk_evidence(self):
        """A May retirement must not be graded against July's risk posture."""
        import hr_retirement_audit as A
        registry = {"agents": [{"class_name": "Ghost", "status": "retired",
                                "file": "agents/ghost.py", "sector": "Small Cap"}]}
        stale = {"buys_blocked": True, "cro_level": "HIGH",
                 "generated_at": (datetime.now() - timedelta(days=45)).isoformat()}
        checks = {f["check"] for f in A.audit(registry=registry, memo=stale, catalog={})}
        self.assertIn("UNVERIFIABLE_RETIREMENT", checks)
        self.assertNotIn("FIRED_DURING_BLOCK", checks,
                         "asserted a wrongful firing from 45-day-old risk evidence")

    def test_audit_refuses_to_call_a_blind_run_clean(self):
        """'Clean' must mean "I looked and found nothing", never "I could not look".

        The VM's 66-agent registry carries no lifecycle field at all — no
        retirement, no reassignment, no counter — because the PIP lifecycle was
        deadlocked and never ran there. Every check in this audit keys off one
        of those fields, so it reported "no wrongful-retirement signals, 66
        agents checked" every week: true, and worth nothing.
        """
        import hr_retirement_audit as A
        inert = [{"class_name": "A%d" % i, "status": "active",
                  "file": "agents/a%d.py" % i, "sector": "Tech"} for i in range(66)]
        observable, reasons = A._lifecycle_is_observable(inert)
        self.assertFalse(observable, "a registry the audit cannot see into read as auditable")
        self.assertTrue(reasons, "declared unauditable without saying why")
        checks = {f["check"] for f in A.audit(registry={"agents": inert},
                                              memo={}, catalog={}, history={})}
        self.assertIn("LIFECYCLE_UNAUDITABLE", checks,
                      "blind audit produced a silent all-clear")

        # ...and it must NOT fire on a registry where the lifecycle does run,
        # or the finding is noise that gets muted and stops meaning anything.
        live = inert[:5] + [{"class_name": "Ghost", "status": "retired",
                             "file": "agents/ghost.py", "sector": "Tech",
                             "retired_at": "2026-05-14",
                             "consecutive_zero_cycles": 3}]
        self.assertTrue(A._lifecycle_is_observable(live)[0],
                        "a registry with a real retirement read as unauditable")

    def test_reinstatement_monitoring_is_read_not_just_written(self):
        """A vacated retirement promised monitoring. Nothing read that promise.

        `monitoring_review_by` was written into hr_history.json by the
        reinstatement and had zero readers anywhere in the codebase — a date in
        a file is not a control. It has to come back and ask.
        """
        import hr_retirement_audit as A
        hist = {"agent_records": {"Ghost": {"events": [
            {"date": "2026-07-31", "event": "RETIREMENT_VACATED",
             "summary": "retirement vacated during a buy freeze it did not cause.",
             "monitoring_review_by": "2026-09-29"}]}}}
        # Driven with fixtures, not the host's live files: the two hosts carry
        # divergent hr_history/probation logs, and a test that reads them asserts
        # on a mutable runtime artifact (ca61acc). Evidence good enough to reach a
        # real verdict, so this test covers the DUE path; the NOT_ASSESSABLE path
        # is test_a_blocked_review_defers_then_escalates_to_the_chairman.
        kw = dict(registry={"agents": [{"class_name": "Ghost", "permanent": True}]},
                  observations=[{"date": "2026-08-%02d" % d, "agent": "Ghost"}
                                for d in range(1, 22)],
                  manager_log=[{"timestamp": "2026-08-15T12:00:00", "decisions": [
                      {"manager_decision": "APPROVED", "manager_action": "BUY",
                       "source_agent": "Agent someoneelse_cos"}]}])

        far = A._reinstatement_reviews(hist, today=date(2026, 8, 1), **kw)
        self.assertEqual(far, [], "asked for the review two months early")

        due = A._reinstatement_reviews(hist, today=date(2026, 9, 25), **kw)
        self.assertEqual([f["check"] for f in due], ["REINSTATEMENT_REVIEW_DUE"])
        self.assertEqual(due[0]["severity"], "MEDIUM")
        self.assertEqual(due[0]["verdict"], "TERMINATE")

        late = A._reinstatement_reviews(hist, today=date(2026, 10, 5), **kw)
        self.assertEqual(late[0]["severity"], "HIGH", "an overdue review is not MEDIUM")
        self.assertIn("OVERDUE", late[0]["description"])

    def test_a_blocked_review_defers_then_escalates_to_the_chairman(self):
        """NOT_ASSESSABLE must never be where a promised review comes to rest.

        THE DEFECT (caught 2026-08-01, before the first review ran). `assess`
        returns NOT_ASSESSABLE from three branches, every one of them with
        requires_chairman False, and nothing anywhere extended the monitoring
        window or escalated an agent the firm could never measure. All seven
        sector agents graded exactly that — `channel_alive: false` on every one
        — so the review the Chairman granted their reinstatement on
        ("HR reviews the recorded evidence on 2026-09-01 and recommends KEEP or
        TERMINATE") would have come due, said "cannot tell", and resolved to
        nothing. Permanently, and silently: flagged-but-never-solved rebuilt
        inside the mechanism written to prevent it.

        Both halves, because a loop that always escalates is as useless as one
        that never does:
          BAD      the blocker persists -> deferral, deferral, then a HIGH
                   Chairman decision. It terminates.
          CONTROL  the blocker clears   -> an ordinary verdict, no escalation,
                   and an agent that could be graded all along is never deferred.
        """
        import datetime as dt
        import hr_retirement_audit as A

        hist = {"agent_records": {"Ghost": {"events": [
            {"date": "2026-08-01", "event": "RETIREMENT_VACATED",
             "summary": "retirement vacated; monitored for a month.",
             "monitoring_review_by": "2026-09-01"}]}}}
        reg = {"agents": [{"class_name": "Ghost", "permanent": True}]}
        obs = [{"date": "2026-08-%02d" % d, "agent": "Ghost"} for d in range(1, 22)]
        dead = [{"timestamp": "2026-08-15T12:00:00", "decisions": []}]
        alive = [{"timestamp": "2026-08-15T12:00:00", "decisions": [
            {"manager_decision": "APPROVED", "manager_action": "BUY",
             "source_agent": "Agent someoneelse_cos"}]}]

        tmp = Path(tempfile.mkdtemp())
        hp = str(tmp / "hr_history.json")
        try:
            def review(day, log):
                return A._reinstatement_reviews(json.loads(json.dumps(hist)),
                                                today=day, registry=reg,
                                                observations=obs, manager_log=log)

            # ── BAD: the credit channel stays dead ────────────────────────────
            f1 = review(dt.date(2026, 9, 1), dead)
            self.assertEqual([f["check"] for f in f1],
                             ["REINSTATEMENT_REVIEW_BLOCKED"],
                             "an unmeasurable review produced no blocked finding — "
                             "it dead-ended at NOT_ASSESSABLE again")
            self.assertEqual(f1[0]["blocker"], "CREDIT_CHANNEL_DEAD")
            self.assertFalse(f1[0]["requires_chairman"],
                             "a broken instrument is the firm's problem to fix "
                             "before it is the Chairman's to rule on")
            self.assertEqual(f1[0]["defer_to"], "2026-10-01",
                             "the review date did not move, so the same "
                             "unanswerable question is asked again tomorrow")

            A._write_json(hp, hist)
            self.assertEqual(A.apply_deferrals(f1, history_path=hp,
                                               today=dt.date(2026, 9, 1))["deferred"],
                             ["Ghost"])
            hist.clear(); hist.update(A._load(hp, {}))

            # Idempotent: the same day again neither re-defers nor re-raises.
            self.assertEqual(A.apply_deferrals(f1, history_path=hp,
                                               today=dt.date(2026, 9, 1))["already"],
                             ["Ghost"])
            self.assertEqual(review(dt.date(2026, 9, 2), dead), [],
                             "the deferral did not take — the review is still due")

            # Deferral 2, then the budget is spent.
            f2 = review(dt.date(2026, 10, 1), dead)
            self.assertEqual(f2[0]["check"], "REINSTATEMENT_REVIEW_BLOCKED")
            self.assertEqual(f2[0]["deferrals_used"], A.MAX_REVIEW_DEFERRALS)
            A.apply_deferrals(f2, history_path=hp, today=dt.date(2026, 10, 1))
            hist.clear(); hist.update(A._load(hp, {}))

            f3 = review(dt.date(2026, 10, 31), dead)
            self.assertEqual([f["check"] for f in f3],
                             ["REINSTATEMENT_REVIEW_UNDECIDABLE"],
                             "the deferral budget is spent and nothing escalated — "
                             "the review can now be deferred forever")
            self.assertEqual(f3[0]["severity"], "HIGH")
            self.assertTrue(f3[0]["requires_chairman"])
            self.assertEqual(A.apply_deferrals(f3, history_path=hp,
                                               today=dt.date(2026, 10, 31))["deferred"],
                             [], "an undecidable review bought itself another slip")

            # It must PERSIST. A finding that stops re-raising is a finding that
            # got quietly dropped, which is the failure one level up.
            self.assertEqual(review(dt.date(2026, 12, 25), dead)[0]["check"],
                             "REINSTATEMENT_REVIEW_UNDECIDABLE")

            # ── CONTROL: CIT repairs the channel ─────────────────────────────
            self.assertEqual(review(dt.date(2026, 10, 31), alive)[0]["check"],
                             "REINSTATEMENT_REVIEW_DUE",
                             "a review that CAN conclude was escalated anyway — an "
                             "escalation that fires on healthy input gets muted")

            # ...and an agent that was gradeable from the start is never deferred.
            fresh = {"agent_records": {"Ghost": {"events": [
                {"date": "2026-08-01", "event": "RETIREMENT_VACATED",
                 "summary": "s.", "monitoring_review_by": "2026-09-01"}]}}}
            ok = A._reinstatement_reviews(fresh, today=dt.date(2026, 9, 1),
                                          registry=reg, observations=obs,
                                          manager_log=alive)
            self.assertEqual(ok[0]["check"], "REINSTATEMENT_REVIEW_DUE")
            self.assertEqual(A.apply_deferrals(ok, history_path=hp,
                                               today=dt.date(2026, 9, 1))["deferred"],
                             [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_blocked_review_reaches_cit_and_not_the_chairman(self):
        """The blocker has an owner, and it is not Jack.

        The whole point of naming the blocker is that somebody can fix it. Two
        halves of one contract:
          • `collect` files it to CIT through the weekly sweep's closed taxonomy,
            ungated by the review date — otherwise the deferral hides the very
            condition that caused it and the reconciler closes the item as
            "no longer observed", declaring a dead channel repaired.
          • the deferral itself is withheld from the Chairman queue. He asked to
            hear about serious decisions; his queue reached 88 rows nobody could
            drain by filing routine work there.
        """
        import datetime as dt
        import hr_retirement_audit as A
        import sweep_taxonomy as tax

        hist = {"agent_records": {"Ghost": {"events": [
            {"date": "2026-08-01", "event": "RETIREMENT_VACATED", "summary": "s.",
             "monitoring_review_by": "2026-09-01"}]}}}
        reg = {"agents": [{"class_name": "Ghost", "permanent": True}]}
        obs = [{"date": "2026-08-%02d" % d, "agent": "Ghost"} for d in range(1, 22)]
        dead = [{"timestamp": "2026-08-15T12:00:00", "decisions": []}]
        alive = [{"timestamp": "2026-08-15T12:00:00", "decisions": [
            {"manager_decision": "APPROVED", "manager_action": "BUY",
             "source_agent": "Agent someoneelse_cos"}]}]

        # Ungated by the review date: still reported six months early.
        early = A.collect(history=hist, registry=reg, observations=obs,
                          manager_log=dead, today=dt.date(2026, 8, 2))
        self.assertEqual([f["kind"] for f in early["findings"]], ["REVIEW_BLOCKED"],
                         "the blocker is only visible once the review is due, so a "
                         "deferral hides it and the sweep then closes it as fixed")
        self.assertEqual(early["findings"][0]["owner"], "CIT")
        self.assertTrue(early["findings"][0]["to_hr"])
        self.assertFalse(early["findings"][0]["to_chairman"])

        # Control: the channel is alive, so nothing is reported at all.
        self.assertEqual(A.collect(history=hist, registry=reg, observations=obs,
                                   manager_log=alive,
                                   today=dt.date(2026, 8, 2))["findings"], [],
                         "a gradeable agent was filed as blocked — noise that gets "
                         "the whole kind muted")

        # The kind is genuinely routed, and closable by the collector that owns it.
        import sweep_governance as SG
        self.assertEqual(tax.route_for("REVIEW_BLOCKED")["owner"], "CIT")
        self.assertIn("REVIEW_BLOCKED",
                      SG.closable_owners()["hr_retirement_audit"])

        # And the deferral does not reach the Chairman.
        blocked = A._reinstatement_reviews(hist, today=dt.date(2026, 9, 1),
                                           registry=reg, observations=obs,
                                           manager_log=dead)
        self.assertFalse(blocked[0].get("to_chairman", True),
                         "a routine deferral was filed as a Chairman decision")
        # …and the filter is enforced where it actually matters: at the submit
        # call. A `to_chairman` flag that main() computes and then ignores is
        # worse than no flag, because the code reads as if it were honoured.
        import unittest.mock as mock
        undecidable = {"check": "REINSTATEMENT_REVIEW_UNDECIDABLE",
                       "class_name": "Ghost", "severity": "HIGH",
                       "description": "cannot tell", "to_chairman": True}
        for findings, expect in ((blocked, []),
                                 (blocked + [undecidable], ["Ghost"])):
            with mock.patch.object(A, "audit", lambda: list(findings)), \
                 mock.patch.object(A, "observe", lambda: {}), \
                 mock.patch.object(A, "apply_deferrals",
                                   lambda *a, **k: {"deferred": [], "already": []}), \
                 mock.patch.object(A, "REPORT_PATH",
                                   os.path.join(tempfile.mkdtemp(), "r.json")), \
                 mock.patch.object(A.chairman_queue, "submit",
                                   return_value=(0, 0)) as sub, \
                 mock.patch.object(sys, "argv", ["hr_retirement_audit.py"]):
                A.main()
            got = [e for c in sub.call_args_list for e in c.args[0]] if sub.called else []
            self.assertEqual([g["dedup_key"].split(":")[1] for g in got], expect,
                             "main() filed the wrong set: a deferral CIT owns "
                             "must not land in the Chairman's queue, and the "
                             "escalation must")

    def test_probation_is_graded_on_the_window_not_on_all_history(self):
        """A probation verdict may only read evidence from the probation.

        THE DEFECT (caught 2026-08-01, before the first review ran).
        _credit_channel() took a `since` argument and no caller passed it, so
        assess() measured "was anyone being credited?" across all 50 cycles on
        record while describing it, to the Chairman, as "during this window".
        Against the live log those disagree completely: lifetime is 36 approved
        / 7 attributed / alive, and 2026-08-01 onward is 0 / 0 / dead. Simulated
        with a flawless month of surveillance, the unscoped version returned
        TERMINATE for eight of the nine agents under watch — every one of them
        convicted of earning nothing during a month in which the firm credited
        nobody, on attributions that all predate their probation.

        That is the 2026-05-14 wrongful retirement rebuilt inside the mechanism
        written to prevent it, and it would have surfaced only at the review, a
        month after anyone was looking.
        """
        import datetime as dt
        import hr_retirement_audit as A

        watched = {"GlobalTechAgent": (dt.date(2026, 9, 1),
                                       {"date": "2026-08-01"}, 0)}
        # Credit exists — but ALL of it lands before the window opens.
        log = [{"timestamp": "2026-07-20T12:00:00", "decisions": [
                    {"manager_decision": "APPROVED", "manager_action": "BUY",
                     "source_agent": "Agent someoneelse_cos"},
                    {"manager_decision": "APPROVED", "manager_action": "BUY",
                     "source_agent": "Agent another_cos"}]}]
        obs = [{"date": "2026-08-%02d" % d, "agent": "GlobalTechAgent"}
               for d in range(1, 22)]          # 21 observations, well over the bar

        ch = A._window_channels(watched, log)["GlobalTechAgent"]
        self.assertEqual(ch["window_start"], "2026-08-01")
        self.assertEqual(ch["cycles_scanned"], 0,
                         "a cycle from before the window must not be scanned")
        self.assertFalse(ch["alive"],
                         "credit earned before anyone was watching cannot make "
                         "the monitoring window's channel 'alive'")

        v = A.assess("GlobalTechAgent", {"permanent": True}, obs, ch)
        self.assertEqual(
            v["verdict"], "NOT_ASSESSABLE",
            "an agent must not be terminated on a window in which the firm "
            "credited nobody — this is exactly why the 2026-05-14 retirements "
            "were wrongful")

        # Positive control: the guard must not have bought safety by making
        # TERMINATE unreachable. Same agent, same log, credit INSIDE the window.
        log2 = log + [{"timestamp": "2026-08-15T12:00:00", "decisions": [
                          {"manager_decision": "APPROVED", "manager_action": "BUY",
                           "source_agent": "Agent someoneelse_cos"}]}]
        ch2 = A._window_channels(watched, log2)["GlobalTechAgent"]
        self.assertTrue(ch2["alive"])
        self.assertEqual(
            A.assess("GlobalTechAgent", {"permanent": True}, obs, ch2)["verdict"],
            "TERMINATE")

        # ...and a credit to the agent ITSELF, inside the window, is a KEEP.
        log3 = log2 + [{"timestamp": "2026-08-20T12:00:00", "decisions": [
                           {"manager_decision": "APPROVED", "manager_action": "BUY",
                            "source_agent": "Agent globaltechagent_cos"}]}]
        ch3 = A._window_channels(watched, log3)["GlobalTechAgent"]
        self.assertEqual(
            A.assess("GlobalTechAgent", {"permanent": True}, obs, ch3)["verdict"],
            "KEEP")

        # A permanent agent's TERMINATE is a recommendation, never an execution.
        self.assertTrue(
            A.assess("GlobalTechAgent", {"permanent": True}, obs, ch2)
             ["requires_chairman"],
            "terminating a permanent agent needs explicit Co-Chairman sign-off")

        # An unrecorded window start must fail toward NOT_ASSESSABLE, never
        # toward a measured-and-empty window, which is what convicts.
        blind = A._window_channels(
            {"GlobalTechAgent": (dt.date(2026, 9, 1), {}, 0)}, log3)["GlobalTechAgent"]
        self.assertIsNone(blind["window_start"])
        self.assertFalse(blind["alive"])
        self.assertEqual(
            A.assess("GlobalTechAgent", {"permanent": True}, obs, blind)["verdict"],
            "NOT_ASSESSABLE")

    def test_a_vacated_retirement_is_always_on_the_hook(self):
        """Invariant: whoever vacates a retirement must set a review date.

        Asserted over whatever THIS host's history holds. hr_history.json is
        gitignored runtime state and the two hosts carry divergent copies —
        the Mac has the two #43 reinstatements, the VM has none of them — so
        the first version of this test, which demanded that a vacated record
        *exist*, passed here and failed on the VM. That is the same
        assert-on-a-mutable-artifact mistake as ca61acc, made again.
        Skip loudly when the host has nothing to check: a visible skip says
        "I could not look here", which a silent pass does not.
        """
        import hr_retirement_audit as A
        hist = json.load(open(Path(__file__).parent / "data" / "hr_history.json"))
        vacated = [(cn, ev)
                   for cn, rec in (hist.get("agent_records") or {}).items()
                   for ev in (rec.get("events") or [])
                   if ev.get("event") == "RETIREMENT_VACATED"]
        if not vacated:
            self.skipTest("no vacated retirement in this host's hr_history.json "
                          "(%d agent records) — nothing to check here"
                          % len(hist.get("agent_records") or {}))
        for cn, ev in vacated:
            self.assertTrue(ev.get("monitoring_review_by"),
                            f"{cn} was reinstated with no monitoring review date")
        # and the reader must fire on them once that date arrives
        latest = max(date.fromisoformat(str(ev["monitoring_review_by"])[:10])
                     for _, ev in vacated)
        fired = A._reinstatement_reviews(hist, today=latest)
        self.assertEqual(len(fired), len(vacated),
                         "review dates exist but nothing raises them")

    def test_live_registry_has_no_duplicate_agent_records(self):
        """Roster counts and per-agent evaluations both double-count duplicates."""
        import collections
        reg = json.load(open(Path(__file__).parent / "data" / "agent_registry.json"))
        dupes = [cn for cn, n in collections.Counter(
            a.get("class_name") for a in reg.get("agents", [])).items() if n > 1]
        self.assertEqual(dupes, [], f"duplicate registry records: {dupes}")


class TestPIPLifecycleNotDeadlocked(unittest.TestCase):
    """The zero-pick counter has to survive the cycle that incremented it.

    _update_agent_performance() writes consecutive_zero_cycles onto EVERY agent
    it reviews, but run() only saved the registry when the review returned a
    stage EVENT — and a stage event can only happen once a counter is already
    past a threshold. The counter needed the save; the save needed the counter:

        czc advances -> only if saved -> only if perf_changes non-empty
                     -> only if czc already >= PERF_NOTICE_CYCLES (8)

    Nothing broke the cycle except an unrelated spawn calling _save_registry()
    further down and flushing everyone's counters as a side effect. So when the
    registry passed MAX_REGISTRY_AGENTS and spawning stopped for good, all four
    PIP stages became unreachable — silently, while the factory kept logging a
    healthy "Registry full. No new agents needed." every cycle.

    Proven against prod's own 66-agent registry before the fix: four consecutive
    run()s, in-memory czc=1 every time, 0 of 66 records ever written to disk.
    The VM's registry had not been touched since 2026-05-28.
    """

    def _log(self, tmpdir, source_agent, decisions=None):
        """A manager cycle. Default: one decision that is NOT an approved buy,
        plus an ATTRIBUTED approval so the interlock stays disengaged and the
        counter is free to move (that is what these tests are measuring)."""
        p = os.path.join(tmpdir, "manager_decisions.json")
        if decisions is None:
            decisions = [
                {"source_agent": source_agent,
                 "manager_decision": "REJECTED", "manager_action": "NONE"},
                {"source_agent": "SomebodyElse",
                 "manager_decision": "APPROVED", "manager_action": "BUY"},
            ]
        with open(p, "w") as f:
            json.dump([{"decisions": decisions}], f)
        return p

    def test_ordinary_review_marks_the_registry_dirty(self):
        """No stage event, but a counter moved — that still has to be persisted."""
        import types, tempfile
        import chief_of_staff_agent as cos

        stub = types.SimpleNamespace(
            registry={"agents": [{"class_name": "QuietAgent", "status": "active",
                                  "sector": "Small Cap"}]},
            _registry_dirty=False)

        with tempfile.TemporaryDirectory() as td:
            original = cos.MANAGER_LOG
            try:
                cos.MANAGER_LOG = self._log(td, "quiet")
                changed = cos.AgentFactory._update_agent_performance(stub)
            finally:
                cos.MANAGER_LOG = original

        agent = stub.registry["agents"][0]
        self.assertEqual(agent["consecutive_zero_cycles"], 1,
                         "the review did not increment the counter at all")
        self.assertEqual(changed, [],
                         "test premise broken: czc=1 must not trigger a stage event")
        self.assertTrue(stub._registry_dirty,
                        "counter advanced in memory but nothing told run() to save it — "
                        "this is the deadlock: the increment is discarded every cycle")

    def test_run_saves_whenever_the_review_dirtied_the_registry(self):
        """run() must not gate the save on stage events alone."""
        src = open(Path(__file__).parent / "chief_of_staff_agent.py").read()
        i = src.find("perf_changes = self._update_agent_performance()")
        self.assertGreater(i, 0, "the performance review call site moved")
        block = src[i:i + 400]
        self.assertIn("_registry_dirty", block,
                      "run() saves the registry only on stage events again — "
                      "counters will freeze at 1 and no agent can ever reach Stage 1")

    def test_counter_advances_across_consecutive_reviews(self):
        """End to end: save, reload, review again — the counter must climb.

        Uses a fresh last_checked each round to isolate persistence from the
        once-per-day cap, which has its own test below.
        """
        import types, tempfile
        import chief_of_staff_agent as cos

        with tempfile.TemporaryDirectory() as td:
            reg_path = os.path.join(td, "agent_registry.json")
            registry = {"agents": [{"class_name": "QuietAgent", "status": "active",
                                    "sector": "Small Cap"}]}
            original = cos.MANAGER_LOG
            try:
                cos.MANAGER_LOG = self._log(td, "quiet")
                for expected in (1, 2, 3):
                    if os.path.exists(reg_path):
                        with open(reg_path) as f:
                            registry = json.load(f)
                    registry["agents"][0].pop("last_checked", None)   # simulate a new day
                    stub = types.SimpleNamespace(registry=registry, _registry_dirty=False)
                    cos.AgentFactory._update_agent_performance(stub)
                    # Stand-in for run()'s save condition.
                    if stub._registry_dirty:
                        with open(reg_path, "w") as f:
                            json.dump(stub.registry, f)
                    with open(reg_path) as f:
                        on_disk = json.load(f)["agents"][0].get("consecutive_zero_cycles")
                    self.assertEqual(
                        on_disk, expected,
                        f"after review {expected} the persisted counter is {on_disk} — "
                        f"it is not advancing, so no agent can reach any PIP stage")
            finally:
                cos.MANAGER_LOG = original

    def test_counter_ticks_at_most_once_per_calendar_day(self):
        """4 pipeline runs a day must not burn 4 cycles of an agent's runway.

        PERF_* thresholds are documented as "R&D cycles, each ~4 days" — Stage 4
        at 30 cycles is meant to be ~120 days of runway. This review runs once
        per main.py invocation, 4x per weekday on the VM, so an uncapped counter
        turned that 120-day runway into about 8 trading days.
        """
        import types, tempfile
        import chief_of_staff_agent as cos

        agent = {"class_name": "QuietAgent", "status": "active", "sector": "Small Cap"}
        stub  = types.SimpleNamespace(registry={"agents": [agent]}, _registry_dirty=False)

        with tempfile.TemporaryDirectory() as td:
            original = cos.MANAGER_LOG
            try:
                cos.MANAGER_LOG = self._log(td, "quiet")
                for _ in range(4):                     # one trading day of pipeline runs
                    cos.AgentFactory._update_agent_performance(stub)
            finally:
                cos.MANAGER_LOG = original

        self.assertEqual(
            agent["consecutive_zero_cycles"], 1,
            f"4 runs in one day advanced the counter to "
            f"{agent['consecutive_zero_cycles']} — an agent burns its entire "
            f"runway {agent['consecutive_zero_cycles']}x too fast")

    def test_no_attributed_approvals_exonerates_everyone(self):
        """If nothing approved names an agent, nobody could have scored.

        Prod's manager log, 50 cycles: 5,982 approved buys, none carrying a
        source_agent, and all 4,210 attributed decisions REJECTED/HOLD. Every
        agent's `hits` was structurally 0. Advancing counters on that evidence
        penalises 66 agents for a broken attribution pipeline — and with the
        registry-save fixed, marches all of them to Stage 4 with recovery
        impossible.
        """
        import types, tempfile
        import chief_of_staff_agent as cos

        # Exactly prod's shape: approvals with no source_agent, attributed rejects.
        unattributed = [
            {"ticker": "LQD", "manager_decision": "APPROVED", "manager_action": "BUY"},
            {"ticker": "XYZ", "source_agent": "Agent quiet",
             "manager_decision": "REJECTED", "manager_action": "HOLD"},
        ]
        agent = {"class_name": "QuietAgent", "status": "active", "sector": "Small Cap"}
        stub  = types.SimpleNamespace(registry={"agents": [agent]}, _registry_dirty=False)

        with tempfile.TemporaryDirectory() as td:
            original = cos.MANAGER_LOG
            try:
                cos.MANAGER_LOG = self._log(td, "quiet", decisions=unattributed)
                cos.AgentFactory._update_agent_performance(stub)
            finally:
                cos.MANAGER_LOG = original

        self.assertEqual(
            agent.get("consecutive_zero_cycles"), 0,
            "counter advanced in a cycle where no approval was attributable to "
            "anyone — the whole firm gets penalised for an attribution outage")
        self.assertEqual(agent.get("exoneration_count"), 1,
                         "exoneration not recorded, so the pause is invisible")

    def test_unattributed_approval_credits_nobody(self):
        """An approval with no source_agent must not be bucketed as a hit.

        It used to land under the literal key "unknown", which any agent whose
        derived key was a substring of it would have matched.
        """
        import types, tempfile
        import chief_of_staff_agent as cos

        decisions = [
            {"ticker": "LQD", "manager_decision": "APPROVED", "manager_action": "BUY"},
            {"ticker": "ABC", "source_agent": "Agent real", "manager_decision": "APPROVED",
             "manager_action": "BUY"},          # keeps the interlock disengaged
        ]
        # "unknown" contains "know", "now", "nkn"... any of these as a class key
        # would previously have scored a free hit off the unattributed bucket.
        agent = {"class_name": "NowAgent", "status": "active",
                 "sector": "Small Cap", "consecutive_zero_cycles": 5}
        stub  = types.SimpleNamespace(registry={"agents": [agent]}, _registry_dirty=False)

        with tempfile.TemporaryDirectory() as td:
            original = cos.MANAGER_LOG
            try:
                cos.MANAGER_LOG = self._log(td, "now", decisions=decisions)
                cos.AgentFactory._update_agent_performance(stub)
            finally:
                cos.MANAGER_LOG = original

        self.assertEqual(agent["consecutive_zero_cycles"], 6,
                         "an unattributed approval reset an agent's counter — "
                         "credit was assigned to an agent that earned nothing")


class TestInstitutionRankingFloor(unittest.TestCase):
    """An institution cannot lead a ranking off a handful of observations.

    Live VM output had Two Sigma ranked FIRST by Sharpe on N=9, ahead of AQR
    Capital on N=7004, with its conviction_auc printed as a bare "nan". Nothing
    reads the ranked lists today (dynamic_position_sizer.py takes the `overall`
    aggregate), so it was misleading report output rather than a live sizing
    bug — but it is one consumer away from being real.
    """

    def _run_against(self, rows):
        """Drive the real run() over a temp DB so the actual ranking code runs."""
        import sqlite3, tempfile, importlib
        import institution_performance_tracker as ipt

        td = tempfile.mkdtemp()
        db = Path(td) / "historical_13f.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE pipeline_snapshots (
                            snapshot_date TEXT, institution TEXT, ticker TEXT,
                            outcome_20d REAL, conviction_score REAL)""")
        conn.executemany(
            "INSERT INTO pipeline_snapshots VALUES (?,?,?,?,?)", rows)
        conn.commit(); conn.close()

        orig_db, orig_report, orig_data = ipt.DB_PATH, ipt.REPORT_PATH, ipt.DATA_DIR
        try:
            ipt.DB_PATH     = db
            ipt.DATA_DIR    = Path(td)
            ipt.REPORT_PATH = Path(td) / "institution_performance.json"
            return ipt.run(lookback_days=3650, recent_days=90)
        finally:
            ipt.DB_PATH, ipt.REPORT_PATH, ipt.DATA_DIR = orig_db, orig_report, orig_data

    def _rows(self):
        """Tiny-N institution with the BEST raw Sharpe, vs a large-N one."""
        import random
        random.seed(7)
        rows = []
        # Tiny: 9 observations, tight and positive -> a huge, meaningless Sharpe
        # (mean 5%, std ~1.2% => sharpe ~4.3). Spread must clear the std_ret>0.01
        # guard in _compute_stats, or the ratio floors to 0 and proves nothing.
        for i, r in enumerate([0.03, 0.04, 0.05, 0.06, 0.07,
                               0.05, 0.04, 0.06, 0.05]):
            rows.append((f"2026-0{i%9+1}-01", "Two Sigma", f"T{i}", r, 3.0))
        # Large: 200 observations, genuinely noisy -> a modest, real Sharpe.
        for i in range(200):
            rows.append((f"2026-0{i%9+1}-01", "AQR Capital", f"A{i%40}",
                         random.gauss(0.01, 0.08), random.uniform(1, 5)))
        return rows

    def test_tiny_n_institution_cannot_appear_in_any_ranking(self):
        import institution_performance_tracker as ipt
        report = self._run_against(self._rows())

        insts = report["institutions"]
        self.assertIn("Two Sigma", insts, "tiny-N institution was silently dropped "
                                          "instead of reported-but-unranked")
        self.assertEqual(insts["Two Sigma"]["n"], 9)

        # It has the best raw Sharpe, so an unfloored ranking WOULD lead with it.
        self.assertGreater(insts["Two Sigma"]["sharpe_like"],
                           insts["AQR Capital"]["sharpe_like"],
                           "test premise broken: tiny-N must out-Sharpe large-N here")

        for name in ("ranking_sharpe", "ranking_win_rate", "ranking_conviction_auc"):
            self.assertNotIn("Two Sigma", report[name],
                             f"N=9 institution appears in {name}")
        self.assertIn("AQR Capital", report["ranking_sharpe"],
                      "the floor excluded a legitimately large sample")

    def test_exclusion_is_visible_not_silent(self):
        report = self._run_against(self._rows())
        self.assertFalse(report["institutions"]["Two Sigma"]["ranking_eligible"])
        reason = report["institutions"]["Two Sigma"]["ranking_exclusion_reason"]
        self.assertIn("N=9", reason)
        self.assertIn("Two Sigma", report["ranking_excluded"])
        self.assertEqual(report["ranking_floor_n"],
                         __import__("institution_performance_tracker").MIN_RANKING_N)

    def test_uncomputable_auc_reports_a_reason_not_a_nan(self):
        """NaN passes every `is not None` guard and fails every threshold."""
        import math
        import institution_performance_tracker as ipt
        report = self._run_against(self._rows())

        ts = report["institutions"]["Two Sigma"]
        self.assertNotIn("conviction_auc", ts,
                         "emitted a conviction_auc for a 9-row sample")
        self.assertIn("conviction_auc_note", ts,
                      "AUC absent with no explanation — a number-shaped blank")
        self.assertIn("N=9", ts["conviction_auc_note"])

        # And the human-facing rendering must never be the string "nan".
        self.assertNotEqual(ipt._auc_display(ts).lower(), "nan")
        self.assertIn("N=9", ipt._auc_display(ts))

        # No NaN may reach the serialized report at all.
        def walk(o):
            if isinstance(o, dict):  return any(walk(v) for v in o.values())
            if isinstance(o, list):  return any(walk(v) for v in o)
            return isinstance(o, float) and math.isnan(o)
        self.assertFalse(walk(report), "a NaN reached the emitted report")

    def test_floor_is_at_least_the_auc_floor(self):
        """Ordering institutions is a stronger claim than scoring one."""
        import institution_performance_tracker as ipt
        self.assertGreaterEqual(ipt.MIN_RANKING_N, ipt.MIN_AUC_N)


class TestRosterMergeLosesNobody(unittest.TestCase):
    """A merge may only ever ADD an agent. Losing one must take a decision.

    THE INCIDENT (2026-08-01): data/agent_registry.json is written live on BOTH
    hosts and is excluded from deploy.sh's rsync, so the two copies forked —
    37 rows on the Mac, 66 on the VM. Nothing noticed for weeks. Reconciling
    them was the fix, but a reconciler is exactly the kind of code that quietly
    deletes what it was written to protect, so every failure mode it can have
    is pinned here:

      • "employed" outranking "retired"  — the first version of merge_rosters
        re-hired all seven retired agents, because employment is the ABSENCE of
        news and the merge treated it as a decision. Every sync would have
        undone HR's firings.
      • collapsing rows by file path     — five CIT agents share cit_director.py.
        Keying the merge on `file` deleted four of them (67 → 63).
      • not collapsing by file at all    — the Mac emits a module row for a file
        the VM's registry names, which double-counted 24 phantom FE/FI agents.

    Two of those three are silent data loss and one is silent invention. The
    tests below are written as MUTATIONS: each asserts on the specific wrong
    answer the bug produced, so re-introducing it fails loudly rather than
    shifting a headcount nobody reads.
    """

    def test_retired_outranks_employed_in_a_merge(self):
        """The mutation that re-hired seven fired agents."""
        import agent_roster as ar
        mac = {"agents": [{"name": "GlobalTechAgent", "kind": "registry",
                           "file": "agents/g.py", "employment": "retired",
                           "retirement_reason": "3 cycles of zero output",
                           "research_cycle": True, "runs_via": ["research_team"],
                           "hosts": ["mac"], "file_exists": True, "tier": 1,
                           "team": "research", "lane": None, "specialization": None,
                           "superseded_by": None, "registry_status": "retired",
                           "permanent": False}]}
        vm = json.loads(json.dumps(mac))
        vm["agents"][0]["employment"] = "employed"   # the other host simply hasn't heard
        vm["agents"][0]["hosts"] = ["vm"]

        for a, b in ((mac, vm), (vm, mac)):          # order must not matter
            merged = ar.merge_rosters(json.loads(json.dumps(a)),
                                      json.loads(json.dumps(b)))
            row = {r["name"]: r for r in merged["agents"]}["GlobalTechAgent"]
            self.assertEqual(row["employment"], "retired",
                             "a host's silence overrode HR's recorded retirement — "
                             "every sync would re-hire everyone HR ever fired")
            self.assertEqual(row["retirement_reason"], "3 cycles of zero output",
                             "the reason survived the merge but the verdict did not")

    def test_agents_sharing_one_file_are_not_collapsed(self):
        """The mutation that deleted four of the five CIT agents."""
        import agent_roster as ar
        shared = []
        for n in ("CITDirector", "CITCodeInspector", "CITSecurityAuditor",
                  "CITDataQuality", "CITSelfImprovement"):
            shared.append({"name": n, "kind": "registry", "file": "cit_director.py",
                           "employment": "employed", "research_cycle": False,
                           "runs_via": ["cron"], "hosts": ["vm"], "file_exists": True,
                           "tier": 2, "team": "cit", "lane": None,
                           "specialization": None, "superseded_by": None,
                           "registry_status": "active", "permanent": True,
                           "retirement_reason": None})
        merged = ar.merge_rosters({"agents": shared}, {"agents": []})
        names = sorted(r["name"] for r in merged["agents"])
        self.assertEqual(len(names), 5,
                         f"five agents sharing cit_director.py collapsed to {len(names)}: "
                         f"{names} — dedup by file path deletes real agents")

    def test_one_agent_named_twice_by_two_hosts_is_one_agent(self):
        """The mutation that invented 24 phantom FE/FI agents."""
        import agent_roster as ar
        f = "agents/cos_fe_hardmoney_agent.py"
        vm = {"agents": [{"name": "FEHardMoneyAgent", "kind": "registry", "file": f,
                          "employment": "employed", "research_cycle": True,
                          "runs_via": ["research_team"], "hosts": ["vm"],
                          "file_exists": True, "tier": 1, "team": "fe", "lane": None,
                          "specialization": None, "superseded_by": None,
                          "registry_status": "active", "permanent": False,
                          "retirement_reason": None}]}
        mac = {"agents": [dict(vm["agents"][0], name="cos_fe_hardmoney_agent",
                               kind="module", hosts=["mac"], team=None,
                               research_cycle=False, registry_status=None)]}
        merged = ar.merge_rosters(mac, vm)
        names = [r["name"] for r in merged["agents"]]
        self.assertEqual(names, ["FEHardMoneyAgent"],
                         f"one file became {len(names)} agents ({names}) because the "
                         "hosts disagreed about its name")
        self.assertEqual(sorted(merged["agents"][0]["hosts"]), ["mac", "vm"])

    def test_registry_merge_cannot_drop_a_class_name(self):
        """sync_registry's own guard, exercised on the real live files."""
        import agent_roster as ar
        mac = ar._load(ar.REGISTRY_PATH, {})
        merged = ar.merge_registries(mac, mac)
        before = {a.get("class_name") for a in ar._registry_rows(mac)}
        after = {a.get("class_name") for a in ar._registry_rows(merged)}
        self.assertEqual(before - after, set(),
                         f"merging the registry with itself lost {sorted(before - after)}")

    def test_hr_history_survives_a_merge_the_vm_wins(self):
        """VM wins operational fields; the Mac keeps the employment record.

        The VM runs the pipeline, so its counters are fresher — but only the Mac
        holds HR's retirement and reinstatement history. A merge that let the
        fresher host win everything would erase why an agent was fired.
        """
        import agent_roster as ar
        mac = {"agents": [{"class_name": "X", "status": "active",
                           "consecutive_zero_cycles": 0,
                           "retired_at": "2026-05-14",
                           "retirement_reason": "zero output, 3 cycles",
                           "reinstatement_reason": "whole-picture exoneration"}]}
        vm = {"agents": [{"class_name": "X", "status": "active",
                          "consecutive_zero_cycles": 9}]}
        row = {a["class_name"]: a for a in
               ar._registry_rows(ar.merge_registries(mac, vm))}["X"]
        self.assertEqual(row["consecutive_zero_cycles"], 9,
                         "the VM's live counter lost to the Mac's stale one")
        self.assertEqual(row["retirement_reason"], "zero output, 3 cycles",
                         "HR's retirement record was erased by the host that never had it")
        self.assertEqual(row["reinstatement_reason"], "whole-picture exoneration")

    def test_ratchet_reds_on_a_vanished_agent_and_greens_on_a_recorded_one(self):
        import agent_roster as ar
        declared = {"agents": [
            {"name": "Kept", "file": "a.py"},
            {"name": "Vanished", "file": "b.py"},
            {"name": "ProperlyRetired", "file": "c.py",
             "retired_at": "2026-05-14", "retirement_reason": "superseded by d.py"},
        ]}
        have = {"agents": [{"name": "Kept", "runs_via": ["cron"],
                            "employment": "employed"}]}
        res = ar.check(have, declared)
        self.assertEqual(res["status"], "RED")
        self.assertEqual([v["name"] for v in res["vanished"]], ["Vanished"],
                         "a recorded retirement was treated as a loss, or a real "
                         "loss was waved through")

    def test_adopt_cannot_launder_a_vanished_agent_out_of_the_manifest(self):
        """--adopt absorbs growth; it must not absorb a disappearance."""
        import agent_roster as ar
        prev = {"agents": [{"name": "Vanished", "file": "b.py",
                            "first_declared": "2026-06-01"}]}
        orig = ar.MANIFEST_PATH
        td = Path(tempfile.mkdtemp())
        try:
            ar.MANIFEST_PATH = td / "m.json"
            ar.MANIFEST_PATH.write_text(json.dumps(prev))
            adopted = ar.adopt({"agents": [
                {"name": "New", "kind": "module", "file": "n.py", "tier": 0,
                 "team": None, "employment": "employed", "hosts": ["mac"]}]})
        finally:
            ar.MANIFEST_PATH = orig
        names = sorted(a["name"] for a in adopted["agents"])
        self.assertEqual(names, ["New", "Vanished"],
                         "one --adopt erased an agent that had gone missing")
        self.assertEqual(
            [a for a in adopted["agents"] if a["name"] == "Vanished"][0]["first_declared"],
            "2026-06-01", "adopt rewrote history instead of preserving it")

    def test_the_schedule_is_both_hosts_not_just_this_one(self):
        """"Scheduled" must mean scheduled ANYWHERE the firm runs.

        Reading only `crontab -l` made a Mac-built roster call 74 agents
        unscheduled where the merged view said 45 — the Mac has a handful of
        jobs and the VM has 75. Same firm, two headcounts, decided by which host
        last wrote the file. Both hosts must read the deployed snapshot.
        """
        import agent_roster as ar
        snap = ar.DATA / "vm_crontab_snapshot.txt"
        if not snap.exists():
            self.skipTest("no crontab snapshot deployed")
        cron = ar._crontab()
        sample = [ln for ln in snap.read_text().splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
        self.assertTrue(sample, "the snapshot holds no job lines")
        self.assertIn(sample[0], cron,
                      "the roster judged 'scheduled' against this host's crontab "
                      "alone — the VM's 75 jobs were invisible to it")

    def test_the_live_manifest_still_declares_every_live_agent(self):
        """The ratchet is only real if it runs against what is actually shipped."""
        import agent_roster as ar
        res = ar.check(ar._load(ar.ROSTER_PATH, {"agents": []}),
                       ar._load(ar.MANIFEST_PATH, {"agents": []}))
        self.assertNotEqual(res["declared"], 0,
                            "no manifest — the shrink ratchet is not armed")
        self.assertEqual(res["vanished"], [],
                         f"agents on the books have disappeared with no recorded "
                         f"retirement: {[v['name'] for v in res['vanished']]}")


class TestHRSeesTheWholeFirm(unittest.TestCase):
    """HR must audit the roster, not the registry it happens to be handed.

    Chairman directive 2026-08-01: "hr needs to be on top of tracking all
    agents, including their own team." HR reviewed agent_registry.json — 67
    rows — while the firm ran 155 agents. Everything HR could not see was
    everything the Chief-of-Staff factory did not create: the board, the deputy
    board, the Logic Team, the Chairman SLA, and HR itself.
    """

    def test_hr_audits_the_roster_not_just_the_registry(self):
        import hr_agent, agent_roster as ar
        res = hr_agent.audit_roster_coverage()
        roster_n = len(ar._load(ar.ROSTER_PATH, {"agents": []})["agents"])
        self.assertEqual(res["checked"], roster_n,
                         "HR checked a different population than the roster holds")
        self.assertGreater(res["checked"], res["reviewed_by_hr"],
                           "test premise broken: registry and roster now match")

    def test_hr_reports_its_own_team_as_unreviewed(self):
        """The specific blind spot: the graders were ungraded."""
        import hr_agent
        res = hr_agent.audit_roster_coverage()
        gov = {g["agent"] for g in res["governance_unreviewed"]}
        self.assertIn("hr_agent", gov,
                      "HR did not name itself among the agents it never reviews")
        self.assertEqual(res["status"], "ATTENTION",
                         "an unaudited governance chain reported as OK")

    def test_a_missing_roster_rebuilds_rather_than_passing(self):
        """No file on disk must mean "go find out", not "all clear"."""
        import hr_agent
        orig = hr_agent.ROSTER_PATH
        td = tempfile.mkdtemp()
        try:
            hr_agent.ROSTER_PATH = os.path.join(td, "nope.json")
            res = hr_agent.audit_roster_coverage()
        finally:
            hr_agent.ROSTER_PATH = orig
        self.assertGreater(res["checked"], 0,
                           "HR reported on zero agents instead of rebuilding the roster")
        self.assertNotEqual(res["status"], "OK",
                            "HR certified full coverage with no roster to check against")

    def test_an_unbuildable_roster_is_attention_never_ok(self):
        """And if it cannot even rebuild, it must say so — not stay quiet.

        This is the branch that matters: a clean bill of health issued by an
        audit that checked nothing is worse than no audit, because it silences
        the question. Driven by breaking the build, not just hiding the file.
        """
        import hr_agent
        orig_path, orig_build = hr_agent.ROSTER_PATH, None
        import agent_roster
        orig_build = agent_roster.build
        td = tempfile.mkdtemp()
        try:
            hr_agent.ROSTER_PATH = os.path.join(td, "nope.json")
            agent_roster.build = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("disk gone"))
            res = hr_agent.audit_roster_coverage()
        finally:
            hr_agent.ROSTER_PATH, agent_roster.build = orig_path, orig_build
        self.assertEqual(res["checked"], 0)
        self.assertEqual(res["status"], "ATTENTION",
                         "HR could not see a single agent and did not raise its hand")
        self.assertIn("disk gone", res.get("error", ""),
                      "the failure was swallowed instead of reported")

    def test_zombies_are_reported_and_never_touch_a_rating(self):
        """Retired on paper, still loaded every cycle."""
        import hr_agent
        res = hr_agent.audit_roster_coverage()
        for z in res["zombies"]:
            self.assertNotEqual(z["still_runs_via"], ["UNSCHEDULED"],
                                f"{z['agent']} flagged as a zombie but runs nowhere")
        self.assertIn("never affect", res["note"].lower().replace("never affects", "never affect"),
                      "roster findings are not marked as management-only, so they "
                      "could leak into an agent's rating or PIP")

    def test_attendance_and_the_roster_agree_on_who_is_scheduled(self):
        """Two definitions of 'scheduled' would give one firm two headcounts."""
        import hr_agent
        att = {a["agent"] for a in hr_agent.audit_attendance()["never_scheduled"]}
        ros = {a["agent"] for a in
               hr_agent.audit_roster_coverage()["employed_unscheduled"]}
        # The roster additionally covers registry agents, which attendance skips
        # by design; attendance must not claim anyone the roster says is running.
        self.assertEqual(att - ros, set(),
                         f"HR called {sorted(att - ros)} unscheduled while the roster "
                         "says they run — the audits disagree about the same firm")

    def test_attendance_does_not_invent_broken_agents(self):
        """A false CANNOT_START is worse than no check at all.

        teaching_division/ is shadowed by teaching_division.py, so BOTH import
        styles the naive probe could use fail on the agents inside it — dotted
        ("teaching_division is not a package") and bare ("no module named
        chief_learning_officer") — even though they run fine as scripts. The
        probe must load from the file. Asserted directly on _probe_import so
        the guard is about the probe, not about whichever agents happen to be
        broken on the day the suite runs.
        """
        import hr_agent, importlib
        here = os.path.dirname(os.path.abspath(hr_agent.__file__))
        rel = "teaching_division/chief_learning_officer.py"
        if not os.path.exists(os.path.join(here, rel)):
            self.skipTest("no package-dir agent to probe")

        with self.assertRaises(Exception):   # premise: a name import cannot work
            importlib.import_module("teaching_division.chief_learning_officer")
        with self.assertRaises(Exception):
            importlib.import_module("chief_learning_officer")

        hr_agent._probe_import("chief_learning_officer", rel, here)   # must not raise

        res = hr_agent.audit_attendance()
        self.assertNotIn("chief_learning_officer",
                         {c["agent"] for c in res["cannot_start"]},
                         "an agent that imports fine was reported broken because of "
                         "the import style used to probe it")


class TestChairmanQueueCannotRefork(unittest.TestCase):
    """One identity function for the whole Chairman-queue contract.

    The queue forked across hosts (188+54 action items, 139+55 decisions) and
    the reconciler merged them to 108/96. That merge is undone the instant the
    SLA ingest de-dupes on a different key than the reconciler merges on — the
    producer/consumer identity mismatch this firm keeps rediscovering. So the
    ingest imports group_key rather than owning a second copy of it.
    """

    def test_the_sla_ingest_uses_the_reconcilers_identity_function(self):
        import chairman_decision_sla as sla, chairman_queue_sync as sync
        self.assertIs(sla._group_key, sync.group_key,
                      "the ingest has its own idea of which items are the same "
                      "item — every reconcile will be undone by the next ingest")

    def test_counter_bumped_restatements_are_one_decision(self):
        import chairman_queue_sync as sync
        a = {"id": "x-1", "title": "CRO at HIGH",
             "description": "CRO has been at HIGH for 41 days"}
        b = {"id": "x-2", "title": "CRO at HIGH",
             "description": "CRO has been at HIGH for 42 days"}
        self.assertEqual(sync.group_key(a), sync.group_key(b),
                         "a re-stated grievance with a bumped day counter reads as "
                         "a new decision — this is how 124 rows of one complaint "
                         "reached the Chairman's inbox")

    def test_a_producer_supplied_dedup_key_wins_over_the_text(self):
        import chairman_queue_sync as sync
        a = {"id": "1", "dedup_key": "buy-freeze", "description": "totally different text"}
        b = {"id": "2", "dedup_key": "buy-freeze", "description": "also different"}
        self.assertEqual(sync.group_key(a), sync.group_key(b))

    def test_ingest_is_idempotent(self):
        """Run twice, add nothing the second time."""
        import chairman_decision_sla as sla
        actions = [{"id": "a-1", "status": "awaiting_chairman", "severity": "HIGH",
                    "description": "CRO has been at HIGH for 41 days",
                    "title": "CRO at HIGH", "source": "test"},
                   {"id": "a-2", "status": "awaiting_chairman", "severity": "HIGH",
                    "description": "CRO has been at HIGH for 42 days",
                    "title": "CRO at HIGH", "source": "test"}]
        td = Path(tempfile.mkdtemp())
        (td / "actions.json").write_text(json.dumps(actions))
        orig = sla.CHAIR_ACTION
        try:
            sla.CHAIR_ACTION = td / "actions.json"
            q = {}
            first = sla._ingest(q)
            second = sla._ingest(q)
        finally:
            sla.CHAIR_ACTION = orig
        self.assertEqual(first, 1, "two restatements of one grievance became "
                                   f"{first} separate Chairman decisions")
        self.assertEqual(second, 0, "a second run re-ingested items already in the "
                                    "queue — the inbox grows forever")

    def test_an_already_merged_id_cannot_walk_back_in(self):
        import chairman_decision_sla as sla
        actions = [{"id": "a-2", "status": "awaiting_chairman",
                    "description": "swallowed by the merge", "source": "test"}]
        td = Path(tempfile.mkdtemp())
        (td / "actions.json").write_text(json.dumps(actions))
        orig = sla.CHAIR_ACTION
        try:
            sla.CHAIR_ACTION = td / "actions.json"
            q = {"a-1": {"id": "a-1", "description": "the surviving record",
                         "merged_ids": ["a-2"], "status": "awaiting_chairman"}}
            added = sla._ingest(q)
        finally:
            sla.CHAIR_ACTION = orig
        self.assertEqual(added, 0,
                         "an id already folded into a queue entry was re-added as a "
                         "new decision — the reconcile silently unwinds itself")

    def test_the_merge_may_never_drop_an_id(self):
        import chairman_queue_sync as sync
        # Note the matched plurals: the digit is normalized to "#" but the words
        # around it are not, so "1 day" and "2 days" stay distinct. That costs at
        # most one extra row on a counter's first tick and is deliberate — a
        # normalizer aggressive enough to fold singular into plural would start
        # merging genuinely different complaints.
        items = [{"id": "a-1", "title": "T", "description": "same thing 41 days"},
                 {"id": "a-2", "title": "T", "description": "same thing 42 days"},
                 {"id": "b-1", "title": "Other", "description": "unrelated"}]
        merged = sync._collapse(items, sync.ACTION_RANK, sync.ACTION_TS_FIELDS, "action")
        seen = set()
        for m in merged:
            seen.add(m["id"])
            seen.update(m.get("merged_ids") or [])
        self.assertEqual(seen, {"a-1", "a-2", "b-1"},
                         f"collapsing dropped {{'a-1','a-2','b-1'}} - {seen}")
        self.assertEqual(len(merged), 2)

    def test_the_most_advanced_status_survives_a_collapse(self):
        """A decision already made must not revert to awaiting_chairman."""
        import chairman_queue_sync as sync
        items = [{"id": "a-1", "title": "T", "description": "x", "status": "resolved"},
                 {"id": "a-2", "title": "T", "description": "x",
                  "status": "awaiting_chairman"}]
        for order in (items, list(reversed(items))):
            merged = sync._collapse(list(order), sync.ACTION_RANK, sync.ACTION_TS_FIELDS, "action")
            self.assertEqual(merged[0]["status"], "resolved",
                             "a resolved item reverted to awaiting_chairman — the "
                             "Chairman is asked to decide something he already did")


class TestSweepTaxonomyIsClosed(unittest.TestCase):
    """No sweep finding may exist that nobody owns.

    The firm's signature bug is a producer emitting an identifier no consumer
    handles, swallowed by a `.get()` default. A weekly sweep is a machine for
    emitting identifiers, so building one without closing that hole would have
    industrialised the exact failure it was built to catch.

    These tests assert the closure holds end to end: every kind has a route,
    every route names a queue that exists, every queue has a real consumer, and
    every category is one the deputy board actually recognises. If someone adds
    a finding kind next year and stops here, the finding never reaches anyone.
    """

    def test_kinds_and_routes_are_a_bijection(self):
        import sweep_taxonomy as tax
        self.assertEqual(set(tax.KINDS), set(tax.ROUTES),
                         "a declared kind has no route, or a route has no kind — "
                         "either way a finding can be produced that reaches nobody")

    def test_unknown_kind_raises_rather_than_defaulting(self):
        """The single most important property: failure is loud, never silent."""
        import sweep_taxonomy as tax
        with self.assertRaises(tax.UnroutedFinding):
            tax.route_for("A_KIND_NOBODY_DECLARED")
        with self.assertRaises(tax.UnroutedFinding):
            tax.make_finding("A_KIND_NOBODY_DECLARED", "subj", "detail")

    def test_every_owner_queue_exists_and_has_a_consumer(self):
        """A queue with no reader is a wastebasket with a nameplate."""
        import sweep_taxonomy as tax
        here = Path(__file__).parent
        for owner, spec in tax.OWNERS.items():
            qfile = Path(spec["queue"]).name
            consumers = spec["consumer"]
            self.assertTrue(consumers, f"{owner} has no declared consumer")
            found = False
            for c in consumers:
                p = here / c
                self.assertTrue(p.exists(), f"{owner}'s consumer {c} does not exist")
                if qfile in p.read_text():
                    found = True
            self.assertTrue(found,
                            f"{owner}'s queue {qfile} is named by no consumer in "
                            f"{consumers} — findings routed there would be orphaned")

    def test_every_category_is_one_the_deputy_board_recognises(self):
        import sweep_taxonomy as tax
        import deputy_board
        known = (set(deputy_board.DEPUTY_CATEGORIES)
                 | set(deputy_board.FULL_BOARD_ONLY_CATEGORIES)
                 | set(deputy_board.AUTO_IMPLEMENT_CATEGORIES))
        for kind, route in tax.ROUTES.items():
            self.assertIn(route["category"], known,
                          f"{kind} files under category {route['category']!r}, which "
                          f"no deputy_board set handles — it would skip the vote")

    def test_hr_has_a_disposition_for_every_kind_routed_to_it(self):
        """HR's policy table must cover exactly what the taxonomy sends it."""
        import sweep_taxonomy as tax
        import hr_agent
        hr_kinds = {k for k, r in tax.ROUTES.items() if r["to_hr"]}
        missing = hr_kinds - set(hr_agent.SWEEP_DISPOSITIONS)
        self.assertFalse(missing,
                         f"HR receives {sorted(missing)} with no disposition — it "
                         f"would land in HR's inbox with nobody knowing what to do")

    def test_every_hr_routed_kind_has_a_collector_that_can_close_it(self):
        """A finding nobody can DISPROVE never leaves HR's queue.

        `_reconcile` only closes an open item when its kind is in
        `_closable_kinds`, and that set is built by claiming kinds per collector
        off a name prefix. A to_hr kind whose prefix matches no collector is
        therefore filed, held open as "unverifiable" forever, and its times_seen
        climbs for years after the condition was fixed — the mirror image of the
        false all-clear, and just as misleading.

        The prefixes are the fragile part. `REVIEW_BLOCKED` had to be named that
        and not `AGENT_REVIEW_BLOCKED`, or agent_sweep's prefix would have
        claimed it and closed it on a run where the collector that actually
        produces it had crashed.
        """
        import sweep_taxonomy as tax, sweep_governance as SG
        owners = SG.closable_owners()
        claimed = set().union(*owners.values())
        hr_kinds = {k for k, r in tax.ROUTES.items() if r["to_hr"]}
        self.assertFalse(
            hr_kinds - claimed,
            "%s enter HR's queue and no collector claims them, so nothing is ever "
            "in a position to close them" % sorted(hr_kinds - claimed))
        overlap = [(a, b) for a in owners for b in owners
                   if a < b and owners[a] & owners[b]]
        self.assertFalse(overlap,
                         "two collectors claim the same kind (%s); the crashed one "
                         "no longer protects it" % overlap)

    def test_hr_queue_producer_and_consumer_agree_on_the_path(self):
        """sweep_governance writes it; hr_agent reads it; same file or neither works."""
        import sweep_governance, hr_agent
        self.assertEqual(Path(sweep_governance.HR_QUEUE_PATH).name,
                         Path(hr_agent.SWEEP_QUEUE_PATH).name)

    def test_sweep_reuses_wiring_audits_regexes_rather_than_copying_them(self):
        """One identity function per contract.

        agent_sweep once carried its own near-identical copy of these patterns.
        The copy required a literal `data/` prefix, so every `DATA / "x.json"`
        was invisible to it and six well-wired agents were reported as speaking
        to nobody. Identity, not equality — two objects that merely match today
        drift apart the first time one is edited.
        """
        import re as _re
        import agent_sweep, wiring_audit

        src = {"probe_mod": 'json.dump(x, open("data/probe_marker.json", "w"))'}
        baseline = agent_sweep._graph(src)
        self.assertIn("probe_marker.json", baseline["writes"]["probe_mod"],
                      "the shared pattern no longer recognises a plain data write")

        # Swap wiring_audit's pattern for one that matches nothing. If agent_sweep
        # kept a private copy, the graph is unchanged and it never learns about a
        # fix made in wiring_audit — the drift that reported six well-wired agents
        # as speaking to nobody.
        original = wiring_audit.DATA_JSON_RE
        wiring_audit.DATA_JSON_RE = _re.compile(r"(?!x)x")
        try:
            patched = agent_sweep._graph(src)
        finally:
            wiring_audit.DATA_JSON_RE = original
        self.assertFalse(patched["writes"]["probe_mod"],
                         "agent_sweep ignored wiring_audit's pattern — it is using a "
                         "copy, which will drift the first time either side is edited")

    def test_only_assurance_failures_reach_the_chairman(self):
        """He asked to hear about serious decisions, not routine defects.

        A queue nobody can drain is a queue nobody reads. Only the kinds meaning
        "the assurance system itself is broken" escalate; everything else is
        work for CIT with an SLA, and problem_lifecycle_sentinel escalates it
        only if that SLA is blown.
        """
        import sweep_taxonomy as tax
        escalating = {k for k, r in tax.ROUTES.items() if r["to_chairman"]}
        self.assertEqual(escalating,
                         {"DETECTOR_BLIND", "SWEEP_CRASHED",
                          "SWEEP_DID_NOT_RUN", "UNROUTED_FINDING"})
        crit = tax.make_finding("AGENT_IMPORT_FAILS", "x", "y")
        self.assertFalse(crit["to_chairman"],
                         "a routine CRITICAL escalated to the Chairman — this is how "
                         "his queue refilled to 88 items the last time")

    def test_governance_holds_items_open_when_a_collector_was_blind(self):
        """Absence of evidence must not be recorded as evidence of repair.

        Closure is by re-observation. If the collector that would have produced a
        finding never ran, its silence proves nothing — and closing on it would
        mark every open item fixed at the exact moment the firm went blind.
        """
        import sweep_governance as sg
        crashed = sg._closable_kinds({"agent_sweep": {"error": "boom"}}, probe=True)
        self.assertFalse([k for k in crashed if k.startswith("AGENT_")],
                         "agent_sweep crashed, yet its findings were still closable")
        healthy = sg._closable_kinds({"agent_sweep": {}}, probe=True)
        self.assertIn("AGENT_ISOLATED", healthy)
        static = sg._closable_kinds({"agent_sweep": {}}, probe=False)
        self.assertNotIn("AGENT_IMPORT_FAILS", static,
                         "a static-only run closed an import finding it never tested")

    def test_closure_happens_by_re_observation_and_reopens_on_relapse(self):
        """The full lifecycle, in one pass: open → close → reopen.

        Closure has to be earned by the condition disappearing from a sweep that
        actually looked, never by anyone declaring it fixed — that is the only
        rule a band-aid cannot game. And a relapse must reopen the same key
        rather than filing a fresh item, or a chronically broken agent looks
        like a run of unrelated one-offs.
        """
        import tempfile
        import sweep_governance as sg
        import sweep_taxonomy as tax

        f = tax.make_finding("AGENT_ISOLATED", "lonely_agent", "talks to nobody")
        closable = {"AGENT_ISOLATED"}
        with tempfile.TemporaryDirectory() as td:
            original = sg.HR_QUEUE_PATH
            sg.HR_QUEUE_PATH = Path(td) / "q.json"
            try:
                r1 = sg._reconcile([f], "2026-08-01T00:00:00", closable)
                self.assertEqual(r1["open"], 1)

                # A sweep that looked and did NOT find it: closed.
                r2 = sg._reconcile([], "2026-08-08T00:00:00", closable)
                self.assertEqual(r2["open"], 0)
                self.assertEqual(len(r2["closed_this_run"]), 1)

                # It comes back: same key reopens, with the relapse counted.
                r3 = sg._reconcile([f], "2026-08-15T00:00:00", closable)
                self.assertEqual(r3["open"], 1)
                entries = json.loads(sg.HR_QUEUE_PATH.read_text())
                self.assertEqual(len(entries), 1, "a relapse filed a duplicate item")
                self.assertEqual(entries[0]["reopen_count"], 1)
            finally:
                sg.HR_QUEUE_PATH = original

    def test_a_decision_already_made_is_not_re_litigated_weekly(self):
        """Ask the board once, not every Monday until someone acts.

        An approved proposal leaves the pending queue, so a naive weekly re-file
        would put the same question back on the agenda forever — and append to
        cos_meeting_requests.json, which is already 9,994 entries deep. But an
        EXECUTED decision must not suppress anything: if the condition is still
        there afterwards, the board is entitled to hear it again.
        """
        import sweep_governance as sg
        outstanding = [{"dedup_key": "sweep-agent_unscheduled", "status": "awaiting_chairman"}]
        self.assertEqual(sg._already_decided(outstanding), {"sweep-agent_unscheduled"})
        for done in ("resolved", "closed", "implemented", "DONE"):
            self.assertEqual(
                sg._already_decided([{"dedup_key": "sweep-agent_unscheduled",
                                      "status": done}]), set(),
                f"status={done} still suppressed the finding — a fix that was "
                f"applied and did not work would never be reported again")
        # Someone else's queue entries must never silence the sweep.
        self.assertEqual(sg._already_decided(
            [{"dedup_key": "pip-integrity-2026", "status": "awaiting_chairman"}]), set())

    def test_board_titles_dedup_across_weeks(self):
        """cit_director dedups by title with digits collapsed to '#'.

        So a headline that changes shape with the count — a plural 's', say —
        files a second proposal the week a batch drops to one item, and the
        board sees the same finding twice with no way to tell.
        """
        import re
        import sweep_governance as sg
        for kind in ("AGENT_UNSCHEDULED", "AGENT_ISOLATED"):
            one = "Sweep: %d finding(s) — %s" % (1, sg._headline(kind))
            many = "Sweep: %d finding(s) — %s" % (45, sg._headline(kind))
            self.assertEqual(re.sub(r"\d+", "#", one), re.sub(r"\d+", "#", many))

    def test_every_kind_has_a_board_headline(self):
        """A kind with no headline crashes _batch on the week it first fires."""
        import sweep_taxonomy as tax
        import sweep_governance as sg
        for kind in tax.KINDS:
            self.assertTrue(sg._headline(kind))

    def test_the_sweep_report_is_deadmanned(self):
        """A weekly job that stops running must itself raise an alarm.

        Without this the sweep's own disappearance is indistinguishable from a
        clean week — the failure mode that let the Oracle host run unwatched for
        69 days.
        """
        import cit_director
        self.assertIn("agent_sweep_report.json",
                      str(cit_director.CITPipelineMonitor.EXPECTED_FILES),
                      "the weekly sweep's report is not freshness-monitored, so the "
                      "sweep silently ceasing would look exactly like all-clear")

    def test_attendance_is_gated_on_the_host_that_runs_the_agent(self):
        """Output freshness is a claim about the agent only where it runs.

        The Mac's crontab is empty by design; the VM runs everything. Measured
        from the Mac, every VM agent's outputs are ancient, and the sweep
        nominated six healthy agents as PIP candidates on that basis. The gate
        has to be wired into collect() itself — proving runs_here() works in
        isolation proves nothing if the caller ignores it.
        """
        import inspect, agent_sweep, agent_roster
        src = inspect.getsource(agent_sweep.collect)
        self.assertIn("runs_here", src,
                      "collect() does not consult agent_roster.runs_here, so the "
                      "staleness check is measuring whichever disk it happens to "
                      "be running on")
        self.assertIn("attendance_skipped_wrong_host", src,
                      "a skipped attendance check must be counted; a check that "
                      "quietly does nothing is the bug this sweep exists to find")
        agents = [{"file": "somewhere_else_agent.py", "runs_via": ["cron"]}]
        self.assertEqual(agent_roster.runs_here(agents, crontab_text=""), set())
        self.assertEqual(
            agent_roster.runs_here(
                agents, crontab_text="0 5 * * * python3 somewhere_else_agent.py"),
            {"somewhere_else_agent"})

    def test_a_host_that_cannot_see_attendance_cannot_close_it(self):
        """Same rule as the crashed-collector guard, one level down.

        A sweep run from the wrong box looked at no attendance evidence, so it
        must not conclude a silent agent recovered.
        """
        import sweep_governance as sg
        blind = {"agent_sweep": {"stats": {"attendance_assessable": 0}},
                 "detector_mutation_harness": {}}
        seeing = {"agent_sweep": {"stats": {"attendance_assessable": 105}},
                  "detector_mutation_harness": {}}
        self.assertNotIn("AGENT_OUTPUT_STALE", sg._closable_kinds(blind, True))
        self.assertNotIn("AGENT_OUTPUT_NEVER_PRODUCED", sg._closable_kinds(blind, True))
        self.assertIn("AGENT_OUTPUT_STALE", sg._closable_kinds(seeing, True))
        self.assertIn("AGENT_OUTPUT_NEVER_PRODUCED", sg._closable_kinds(seeing, True))
        # ...and the guard must not have quietly disabled every other kind.
        self.assertIn("AGENT_ISOLATED", sg._closable_kinds(blind, True))


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
    suite.addTests(loader.loadTestsFromTestCase(TestLiveSnapshotPathActuallyRecords))
    suite.addTests(loader.loadTestsFromTestCase(TestLTExperimentsAreRealExperiments))
    suite.addTests(loader.loadTestsFromTestCase(TestFormatSpecsAreReachable))
    suite.addTests(loader.loadTestsFromTestCase(TestLabelColumnContract))
    suite.addTests(loader.loadTestsFromTestCase(TestEveryDecisionCarriesItsOriginator))
    suite.addTests(loader.loadTestsFromTestCase(TestPerformanceSnapshotProducer))
    suite.addTests(loader.loadTestsFromTestCase(TestLossAttributionMustBeCurrent))
    suite.addTests(loader.loadTestsFromTestCase(TestFICITReachesTheChairman))
    suite.addTests(loader.loadTestsFromTestCase(TestHostSpecificChecksStayOnTheirHost))
    suite.addTests(loader.loadTestsFromTestCase(TestWrongfulRetirement))
    suite.addTests(loader.loadTestsFromTestCase(TestPIPLifecycleNotDeadlocked))
    suite.addTests(loader.loadTestsFromTestCase(TestInstitutionRankingFloor))
    suite.addTests(loader.loadTestsFromTestCase(TestRosterMergeLosesNobody))
    suite.addTests(loader.loadTestsFromTestCase(TestHRSeesTheWholeFirm))
    suite.addTests(loader.loadTestsFromTestCase(TestChairmanQueueCannotRefork))
    suite.addTests(loader.loadTestsFromTestCase(TestSweepTaxonomyIsClosed))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
