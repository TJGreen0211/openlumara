"""
Self-contained unit test for the /api/settings/save and /api/settings/load routes
in channels/webui.py:
  - module settings apply to the whole server: an admin's save lands in the global
    config, a regular user's save never touches it
  - a save merges into the global config, so whatever the payload leaves out stays
    (it used to replace it, which wiped modules.settings from config.yml and left
    every module without settings until the next restart)
  - stale per-user module settings in data/{username}/config.json are ignored

Like test_html_preview.py, it injects a lightweight stand-in `core` module into
sys.modules, so the app is never bootstrapped. _merge_user_config_over and
_deep_merge are lifted out of the REAL core/config.py so the merge rules under test
are the real ones.

Run:  venv\\Scripts\\python tests\\test_settings_save.py
"""
import ast
import contextvars
import copy
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


class _FakeConfig:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakeGlobalConfig(dict):
    """load(data) replaces everything, like core.storage.StorageDict"""
    def load(self, data=None):
        if data is not None:
            self.clear()
            self.update(data)
        return True

    def save(self):
        pass


def _real_config_functions():
    """pulls functions out of core/config.py without importing it (that needs the whole app)"""
    with open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    wanted = {"_deep_merge", "_merge_user_config_over"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {"copy": copy}
    exec(compile(ast.Module(nodes, []), "core/config.py", "exec"), namespace)
    return namespace


_real = _real_config_functions()

_user_configs = {}

_config_mod = types.ModuleType("core.config")
_config_mod.config = _FakeGlobalConfig()
_config_mod.default_config = {}
_config_mod.PER_USER_KEYS = {"api", "model", "appearance", "audio"}
_config_mod.PER_USER_CORE_KEYS = {"auto_resume_chats", "cmd_prefix", "tool_timeout"}
_config_mod.load_user_config = lambda username: _user_configs.get(username, {})
_config_mod.save_user_config = lambda username, data: _user_configs.__setitem__(username, data)

_real["default_config"] = _config_mod.default_config
_real["load_user_config"] = _config_mod.load_user_config
_real["PER_USER_KEYS"] = _config_mod.PER_USER_KEYS
_real["PER_USER_CORE_KEYS"] = _config_mod.PER_USER_CORE_KEYS
_config_mod._deep_merge = _real["_deep_merge"]
_config_mod._merge_user_config_over = _real["_merge_user_config_over"]

_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.detail_error = lambda e: str(e)
_core_mod.get_path = lambda p: os.path.abspath(p)
_core_mod.current_user = contextvars.ContextVar("current_user", default=None)
_core_mod.config = _config_mod
sys.modules["core"] = _core_mod

_channel_mod = types.ModuleType("core.channel")
_channel_mod.Channel = type("Channel", (), {})
_core_mod.channel = _channel_mod

_spec = importlib.util.spec_from_file_location(
    "ol_webui_settings_test", os.path.join(ROOT, "channels", "webui.py"))
webui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webui_mod)


class _FakeManager:
    def __init__(self):
        self.modules = {}
        self.reloaded = []

    async def reload_module(self, name):
        self.reloaded.append(name)


class _FakeUsers:
    roles = {"root": "admin", "bob": "user"}

    def authenticate(self, username, password):
        return {"username": username, "role": self.roles[username]}

    def get_user(self, username):
        return {"role": self.roles[username]} if username in self.roles else None


class _FakeChannel:
    def __init__(self, tmp_dir, require_login=True):
        self.name = "webui"
        self.config = _FakeConfig({"require_login": require_login, "login_lifetime": 30})
        self.assets_path = tmp_dir
        self.path = tmp_dir
        self.manager = _FakeManager()
        self.user_manager = _FakeUsers()
        self.login_attempts = {}

    def log(self, *args, **kwargs):
        pass

    def _get_session_secret(self):
        return "test-session-secret"


def _global_config():
    return {
        "core": {"data_folder": "data"},
        "channels": {"enabled": ["webui"], "settings": {"webui": {"port": 3000}}},
        "modules": {
            "enabled": ["coder"],
            "disabled": [],
            "disabled_prompts": ["calendar"],
            "settings": {
                "coder": {"sandbox_paths": ["coder"], "global_line_limit": 1000},
                "memory": {"top_k": 5},
            },
        },
    }


@unittest.skipIf(TestClient is None, "fastapi test client not installed")
class _SettingsTest(unittest.IsolatedAsyncioTestCase):
    require_login = True

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_settings_test_")
        _config_mod.config.load(_global_config())
        _user_configs.clear()
        self.channel = _FakeChannel(self.tmp, require_login=self.require_login)
        self.client = TestClient(await webui_mod.create_fastapi(self.channel), follow_redirects=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, username):
        self.assertEqual(self.client.post("/login", data={"username": username, "password": "x"}).status_code, 303)

    def _save(self, payload):
        resp = self.client.post("/api/settings/save", json=payload)
        self.assertTrue(resp.json()["success"], resp.text)


class AdminSaveTest(_SettingsTest):
    async def test_payload_without_module_settings_keeps_them(self):
        # the bug: this emptied modules.settings for the whole server
        self._login("root")
        self._save({"modules": {"enabled": ["coder"], "disabled": []}})
        settings = _config_mod.config["modules"]["settings"]
        self.assertEqual(settings["coder"]["sandbox_paths"], ["coder"])
        self.assertEqual(settings["memory"], {"top_k": 5})

    async def test_module_settings_apply_to_the_whole_server(self):
        self._login("root")
        self._save({"modules": {"settings": {"coder": {"global_line_limit": 50}}}, "changed_modules": ["coder"]})
        coder = _config_mod.config["modules"]["settings"]["coder"]
        self.assertEqual(coder["global_line_limit"], 50)
        self.assertEqual(coder["sandbox_paths"], ["coder"], "keys left out of the payload must stay")
        self.assertNotIn("modules", _user_configs.get("root", {}))
        self.assertEqual(self.channel.manager.reloaded, ["coder"])

    async def test_keys_the_frontend_never_sends_survive(self):
        self._login("root")
        self._save({"modules": {"enabled": ["coder"], "disabled": [], "settings": {}}})
        self.assertEqual(_config_mod.config["modules"]["disabled_prompts"], ["calendar"])

    async def test_per_user_keys_stay_per_user(self):
        self._login("root")
        self._save({"api": {"url": "http://mine"}, "core": {"cmd_prefix": "!", "data_folder": "elsewhere"}})
        self.assertEqual(_user_configs["root"]["api"], {"url": "http://mine"})
        self.assertEqual(_user_configs["root"]["core"], {"cmd_prefix": "!"})
        self.assertNotIn("api", _config_mod.config)
        self.assertEqual(_config_mod.config["core"]["data_folder"], "elsewhere")


class RegularUserSaveTest(_SettingsTest):
    async def test_cannot_change_module_settings_or_reload_modules(self):
        self._login("bob")
        self._save({"modules": {"settings": {"coder": {"sandbox_paths": ["/etc"]}}}, "changed_modules": ["coder"]})
        self.assertEqual(_config_mod.config, _global_config())
        self.assertNotIn("modules", _user_configs.get("bob", {}))
        self.assertEqual(self.channel.manager.reloaded, [])

    async def test_can_still_save_their_own_settings(self):
        self._login("bob")
        self._save({"appearance": {"theme": "dark"}})
        self.assertEqual(_user_configs["bob"]["appearance"], {"theme": "dark"})
        self.assertEqual(_config_mod.config, _global_config())


class SingleUserSaveTest(_SettingsTest):
    require_login = False

    async def test_merges_into_the_global_config(self):
        self._save({"modules": {"settings": {"coder": {"global_line_limit": 50}}}})
        modules = _config_mod.config["modules"]
        self.assertEqual(modules["settings"]["coder"]["global_line_limit"], 50)
        self.assertEqual(modules["settings"]["memory"], {"top_k": 5})
        self.assertEqual(modules["disabled_prompts"], ["calendar"])


class SettingsLoadTest(_SettingsTest):
    async def test_stale_per_user_module_settings_are_ignored(self):
        # older versions saved a full copy of the module settings per user
        _user_configs["root"] = {"modules": {"settings": {"coder": {"sandbox_paths": ["~/coder"]}}}}
        self._login("root")
        data = self.client.get("/api/settings/load").json()["data"]
        self.assertEqual(data["modules"]["settings"]["coder"]["sandbox_paths"], ["coder"])

    async def test_per_user_keys_still_merge_over_the_global_config(self):
        _user_configs["root"] = {"api": {"url": "http://mine"}}
        self._login("root")
        self.assertEqual(self.client.get("/api/settings/load").json()["data"]["api"], {"url": "http://mine"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
