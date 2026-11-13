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

    def test_corrupt_heartbeat_is_stale_not_crash(self):
        (Path(self.tmp) / "f.json").write_text('{"ts": "garbage", "n_done": 1}')
        self.assertEqual(self._st(), "STALE")


if __name__ == "__main__":
    unittest.main()
