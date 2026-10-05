"""
Self-contained unit test for modules/time.py timezone resolution.

It does NOT import the real `core` package (that pulls in the whole app bootstrap).
Instead it injects lightweight stand-in `core` / `core.config` / `core.module`
modules into sys.modules, loads modules/time.py as a standalone module, and drives
Time._get_current_time() to verify the per-user timezone logic:

  * timezone == "local" + a detected zone  -> uses the detected zone
  * timezone == <explicit zone>            -> uses that zone (override wins)
  * timezone == "local" + no detection     -> falls back to the server's local zone

Run:  venv\\Scripts\\python tests/test_time_timezone.py
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TIME_PATH = os.path.join(ROOT, "modules", "time.py")

# ---------------------------------------------------------------------------
# fake core so we can load modules/time.py without the app bootstrap
# ---------------------------------------------------------------------------
_state = {
    "timezone": "local",        # what core.config.get() returns for the tz key
    "detected_file": None,      # path that core.get_data_path() returns
}


def _fake_config_get(*args, **kwargs):
    # args are ("modules", "settings", "time", "timezone", default=...);
    # this test only ever reads the single timezone key.
    return _state["timezone"]


def _fake_get_data_path(subpath=None, user=None):
    return _state["detected_file"]


class _FakeModule:
    def __init__(self, *args, **kwargs):
        pass

    def log(self, category, message):
        pass


_core = types.ModuleType("core")
_core.get_data_path = _fake_get_data_path
_core.detail_error = lambda e: str(e)

_core_config = types.ModuleType("core.config")
_core_config.get = _fake_config_get

_core_module = types.ModuleType("core.module")
_core_module.Module = _FakeModule

_core.config = _core_config
_core.module = _core_module

sys.modules["core"] = _core
sys.modules["core.config"] = _core_config
sys.modules["core.module"] = _core_module

_spec = importlib.util.spec_from_file_location("ol_time_under_test", TIME_PATH)
time_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(time_mod)


class TimeTimezoneTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.detected_file = os.path.join(self._tmpdir.name, "detected_timezone")
        self.addCleanup(self._tmpdir.cleanup)
        _state["detected_file"] = self.detected_file

    def _make_time(self):
        t = time_mod.Time()
        t.name = "time"
        return t

    def _write_detected(self, zone):
        with open(self.detected_file, "w", encoding="utf-8") as f:
            f.write(zone)

    def test_local_uses_detected_zone(self):
        _state["timezone"] = "local"
        self._write_detected("America/New_York")

        now = self._make_time()._get_current_time()

        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.tzinfo.key, "America/New_York")

    def test_explicit_zone_overrides_detection(self):
        _state["timezone"] = "Europe/Paris"
        self._write_detected("America/New_York")  # should be ignored

        now = self._make_time()._get_current_time()

        self.assertEqual(now.tzinfo.key, "Europe/Paris")

    def test_local_without_detection_falls_back_to_server_local(self):
        _state["timezone"] = "local"
        # detected file intentionally absent

        now = self._make_time()._get_current_time()

        # must be an aware datetime (server local zone fallback)
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())

    def test_read_detected_timezone_empty_file_returns_none(self):
        _state["timezone"] = "local"
        self._write_detected("   ")  # whitespace only

        self.assertIsNone(self._make_time()._read_detected_timezone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
