"""Tests for liveness.py -- includes the poison pill (planted silence must alarm)."""
import os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


class TestLiveness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["APEX_HEARTBEAT_DIR"] = self.tmp
        import liveness
        self.L = liveness
        self.now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)  # a Wednesday
        self.reg = {"features": {"f": {"max_silence_hours": 26, "min_n_done": 1, "instrumented": True}}}

    def tearDown(self):
        os.environ.pop("APEX_HEARTBEAT_DIR", None)

    def _st(self):
        return self.L.check(self.reg, now=self.now)["results"]["f"]["status"]

    def test_fresh_beat_is_ok(self):
        self.L.beat("f", 4, 4, now=self.now - timedelta(hours=1))
        self.assertEqual(self._st(), "OK")

    def test_poison_pill_silence_alarms(self):
        self.L.beat("f", 4, 4, now=self.now - timedelta(hours=80))
        rep = self.L.check(self.reg, now=self.now)
        self.assertEqual(rep["results"]["f"]["status"], "STALE")
        self.assertEqual(len(rep["alarms"]), 1)

    def test_poison_pill_alive_but_did_nothing_alarms(self):
        self.L.beat("f", 0, 4, now=self.now - timedelta(hours=1))
        self.assertEqual(self._st(), "NO_OUTCOME")

    def test_idle_with_nothing_pending_is_not_an_alarm(self):
        self.L.beat("f", 0, 0, now=self.now - timedelta(hours=1))
        self.assertEqual(self._st(), "OK")

    def test_instrumented_but_no_file_alarms(self):
        self.assertEqual(self._st(), "MISSING")

    def test_just_instrumented_no_beat_yet_waits_does_not_page(self):
        # Deployed 3h ago; a 26h job has had no chance to run. Paging now is a false alarm.
        self.reg["features"]["f"]["instrumented_since"] = (self.now - timedelta(hours=3)).isoformat()
        rep = self.L.check(self.reg, now=self.now)
        self.assertEqual(rep["results"]["f"]["status"], "AWAITING_FIRST_BEAT")
        self.assertEqual(rep["alarms"], [])

    def test_poison_pill_grace_expired_with_no_beat_alarms(self):
        self.reg["features"]["f"]["instrumented_since"] = (self.now - timedelta(hours=30)).isoformat()
        rep = self.L.check(self.reg, now=self.now)
        self.assertEqual(rep["results"]["f"]["status"], "MISSING")
        self.assertEqual(len(rep["alarms"]), 1)

    def test_unparseable_instrumented_since_fails_closed_to_missing(self):
        self.reg["features"]["f"]["instrumented_since"] = "not a date"
        self.assertEqual(self._st(), "MISSING")

    def test_uninstrumented_is_counted_not_healthy_not_paged(self):
        self.reg["features"]["f"]["instrumented"] = False
        rep = self.L.check(self.reg, now=self.now)
        self.assertEqual(rep["results"]["f"]["status"], "UNINSTRUMENTED")
        self.assertEqual(rep["n_uninstrumented"], 1)
        self.assertEqual(rep["alarms"], [])

    def test_registry_file_is_valid_and_nonempty(self):
        rep = self.L.check(now=self.now)
        self.assertGreater(rep["n_examined"], 0)
        for n, spec in self.L.json.loads(self.L.REGISTRY.read_text())["features"].items():
            self.assertIn("max_silence_hours", spec, n)

    def test_every_registry_row_has_a_parseable_instrumented_since(self):
        import json
        reg = json.loads(self.L.REGISTRY.read_text())
        for name, spec in reg["features"].items():
            if spec.get("instrumented"):
                ts = datetime.fromisoformat(spec["instrumented_since"].replace("Z", "+00:00"))
                self.assertIsNotNone(ts.tzinfo, name)

    def test_weekdays_only_friday_beat_is_not_stale_on_monday_morning(self):
        mon = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        fri = datetime(2026, 11, 13, 13, 30, tzinfo=timezone.utc)   # 70.5 wall hours
        self.reg["features"]["f"]["weekdays_only"] = True
        self.L.beat("f", 1, 1, now=fri)
        self.assertEqual(self.L.check(self.reg, now=mon)["results"]["f"]["status"], "OK")

    def test_weekdays_only_poison_pill_two_weekdays_silent_still_alarms(self):
        wed = datetime(2026, 11, 18, 14, tzinfo=timezone.utc)
        mon = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)   # 50 weekday hours
        self.reg["features"]["f"]["weekdays_only"] = True
        self.L.beat("f", 1, 1, now=mon)
        self.assertEqual(self.L.check(self.reg, now=wed)["results"]["f"]["status"], "STALE")

    def test_wall_clock_feature_friday_beat_is_stale_monday(self):
        mon = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.L.beat("f", 1, 1, now=datetime(2026, 11, 13, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(self.L.check(self.reg, now=mon)["results"]["f"]["status"], "STALE")

    def test_corrupt_heartbeat_is_stale_not_crash(self):
        (Path(self.tmp) / "f.json").write_text('{"ts": "garbage", "n_done": 1}')
        self.assertEqual(self._st(), "STALE")



class TestEveryRegisteredFeatureIsWired(unittest.TestCase):
    """Wiring contract: a registry row is a promise that some producer beats.

    Until 2026-11-19 all 16 rows said instrumented:false, so the watcher's
    whole verdict was "16 UNINSTRUMENTED, 0 alarms" -- a monitor that could
    never fire. Two halves, both AST-resolved (never grepped: comments name
    every feature):
      1. every registered feature is instrumented, and
      2. every instrumented feature has a literal liveness.beat("<name>", ...)
         call in some production module (tests excluded).
    """
    HERE = Path(__file__).resolve().parent

    def _beat_sites(self):
        import ast
        sites = {}
        for f in self.HERE.glob("*.py"):
            if f.name.startswith("test_") or f.name == "liveness.py":
                continue
            try:
                tree = ast.parse(f.read_text(), filename=str(f))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "beat"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in ("liveness", "_liveness")
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    sites.setdefault(node.args[0].value, []).append(f.name)
        return sites

    def _registry(self):
        import json
        return json.loads((self.HERE / "config" / "liveness.json").read_text())["features"]

    def test_every_registered_feature_is_instrumented(self):
        off = [n for n, s in self._registry().items() if not s.get("instrumented")]
        self.assertEqual(off, [], "registered but not instrumented (watcher cannot fire): %s" % off)

    def test_every_instrumented_feature_has_a_beat_call(self):
        sites = self._beat_sites()
        missing = [n for n, s in self._registry().items()
                   if s.get("instrumented") and n not in sites]
        self.assertEqual(missing, [], "instrumented with no liveness.beat() producer: %s" % missing)

    def test_no_beat_names_an_unregistered_feature(self):
        reg = self._registry()
        stray = sorted(set(self._beat_sites()) - set(reg) - {"liveness_watcher"})
        self.assertEqual(stray, [], "beat() for a feature the watcher never reads: %s" % stray)


class TestATestCannotRefreshAProductionHeartbeat(unittest.TestCase):
    def test_beat_under_unittest_without_env_never_touches_data(self):
        import liveness
        saved = os.environ.pop("APEX_HEARTBEAT_DIR", None)
        try:
            d = liveness._hb_dir()
            self.assertNotEqual(d, liveness.DATA / "heartbeats")
            self.assertNotIn(str(liveness.DATA), str(d))
        finally:
            if saved is not None:
                os.environ["APEX_HEARTBEAT_DIR"] = saved

    def test_a_library_importing_unittest_is_not_a_test(self):
        # sklearn/xgboost/lightgbm/matplotlib import unittest; a production
        # process that loads one must still beat into data/heartbeats.
        import subprocess
        here = Path(__file__).resolve().parent
        env = {k: v for k, v in os.environ.items() if k != "APEX_HEARTBEAT_DIR"}
        out = subprocess.run(
            [sys.executable, "-c", "import unittest, liveness; print(liveness._hb_dir())"],
            cwd=str(here), env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), str(here / "data" / "heartbeats"))

    def test_no_production_module_imports_unittest(self):
        # The guard above keys on sys.modules; a production import of unittest
        # would silently divert that job's live heartbeats to /tmp.
        import ast
        here = Path(__file__).resolve().parent
        bad = []
        for f in here.glob("*.py"):
            if f.name.startswith("test_") or f.name in ("conftest.py",):
                continue
            try:
                tree = ast.parse(f.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                         else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                if any(x == "unittest" or x.startswith("unittest.") for x in names):
                    bad.append(f.name)
        self.assertEqual(sorted(set(bad)), [])


class TestManagerBeats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["APEX_HEARTBEAT_DIR"] = self.tmp
        import manager_agent
        self.M = manager_agent

    def tearDown(self):
        os.environ.pop("APEX_HEARTBEAT_DIR", None)

    def _hb(self, f):
        import json
        p = Path(self.tmp) / (f + ".json")
        return json.loads(p.read_text()) if p.exists() else None

    def test_failed_cover_is_counted_as_not_done(self):
        dec = [{"ticker": "XYZ", "side": "short", "manager_decision": "APPROVED",
                "manager_action": "SELL"}]
        ex = [{"ticker": "XYZ", "executed": False, "error": "qty<=0"}]
        out = self.M._liveness_beats(dec, ex, dry_run=False, cro_level="HIGH")
        self.assertEqual(out["short_covers"], (0, 1))
        self.assertEqual(out["pod_orders"], (0, 1))
        self.assertNotIn("cro_buy_block", out)       # HIGH = buys blocked: no beat
        self.assertEqual(self._hb("short_covers")["n_examined"], 1)

    def test_dry_run_beats_nothing(self):
        out = self.M._liveness_beats([], [{"ticker": "A", "executed": True}],
                                     dry_run=True, cro_level="LOW")
        self.assertEqual(out, {})
        self.assertIsNone(self._hb("pod_orders"))

    def test_no_orders_does_not_refresh_pod_orders(self):
        out = self.M._liveness_beats([], [], dry_run=False, cro_level="ELEVATED")
        self.assertNotIn("pod_orders", out)
        self.assertEqual(out["short_covers"], (0, 0))
        self.assertEqual(out["cro_buy_block"], (1, 1))

if __name__ == "__main__":
    unittest.main()
