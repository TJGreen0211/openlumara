"""
Self-contained unit test for the WebUI session secret (audit 1.2):
  - ConfigManager.get("key", "literal string") treats the string as another
    key, not a default, and returns None - the bug that made session cookies
    forgeable (starlette signs with str(None) == "None").
  - Webui._get_session_secret generates a random secret on first run, persists
    it in the GLOBAL data dir (user=""), and returns the same secret on
    subsequent calls.

It does NOT import the real `core` package. Instead it injects lightweight
stand-ins into sys.modules, like tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_session_secret.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------------------
# fake core + stub packages so core/config.py can be loaded standalone
# ---------------------------------------------------------------------------
DATA_DIR = {"path": None, "user": "unset"}


def _fake_get_data_path(subpath=None, user=None):
    DATA_DIR["user"] = user
    return DATA_DIR["path"]


_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_path = lambda p: os.path.abspath(p)
_core_mod.get_data_path = _fake_get_data_path
sys.modules["core"] = _core_mod

for _name in ("modules", "user_modules", "channels", "user_channels"):
    _stub = types.ModuleType(_name)
    _stub.__path__ = []
    sys.modules[_name] = _stub

_channel_mod = types.ModuleType("core.channel")
_channel_mod.Channel = type("Channel", (), {})
_core_mod.channel = _channel_mod

_spec = importlib.util.spec_from_file_location(
    "ol_config_under_test", os.path.join(ROOT, "core", "config.py"))
config_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config_mod)

_spec = importlib.util.spec_from_file_location(
    "ol_webui_secret_under_test", os.path.join(ROOT, "channels", "webui.py"))
webui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webui_mod)


class _FakeRoot(dict):
    def load(self):
        pass


class ConfigGetPitfallTest(unittest.TestCase):
    def test_string_second_arg_is_a_key_not_a_default(self):
        cfg = config_mod.ConfigManager(_FakeRoot({"session_secret": None}), [])
        # the old webui call: "openlumara-..." is treated as a nested key,
        # so this returns None instead of the intended default
        self.assertIsNone(cfg.get("session_secret", "openlumara-default-session-secret-change-me"))

    def test_default_kwarg_works(self):
        cfg = config_mod.ConfigManager(_FakeRoot({}), [])
        self.assertEqual(cfg.get("session_secret", default="fallback"), "fallback")


class SessionSecretTest(unittest.TestCase):
    def setUp(self):
        DATA_DIR["path"] = tempfile.mkdtemp(prefix="openlumara_secret_")
        DATA_DIR["user"] = "unset"
        self.channel = webui_mod.Webui.__new__(webui_mod.Webui)
        self.channel.log = lambda *args: None

    def tearDown(self):
        shutil.rmtree(DATA_DIR["path"], ignore_errors=True)

    def _make(self):
        return self.channel._get_session_secret()

    def test_generates_random_secret_and_persists(self):
        secret = self._make()
        self.assertTrue(secret)
        self.assertNotIn(secret, ("None", "openlumara-default-session-secret-change-me"))
        self.assertEqual(len(secret), 64)
        secret_file = os.path.join(DATA_DIR["path"], "session_secret")
        self.assertTrue(os.path.exists(secret_file))
        with open(secret_file, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), secret)

    def test_secret_is_stable_across_calls(self):
        self.assertEqual(self._make(), self._make())

    def test_secret_is_global_not_per_user(self):
        self._make()
        self.assertEqual(DATA_DIR["user"], "")

    def test_corrupt_empty_file_regenerates(self):
        secret_file = os.path.join(DATA_DIR["path"], "session_secret")
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write("   ")
        secret = self._make()
        self.assertEqual(len(secret), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
