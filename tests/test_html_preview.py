"""
Self-contained unit test for the HTML preview MVP:
  - modules/coder.py: the open_preview tool (success on existing file, failure on missing)
  - channels/webui.py: the /api/artifact route (503 without coder, 200 + no-store for
    existing files, 404 for missing files and path traversal)

It does NOT import the real `core` package (that pulls in the whole app bootstrap).
Instead it injects a lightweight stand-in `core` module into sys.modules, reusing the
REAL core/functions.py sandbox_path so path-safety is tested for real.

Run:  venv\\Scripts\\python tests\\test_html_preview.py
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

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None  # webui is the primary channel, so fastapi+httpx should be installed


class _FakeConfig:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value


class _ModuleBase:
    def __init__(self):
        self.name = "under_test"
        self.config = _FakeConfig({})

    def result(self, data, success=True):
        return {"success": success, "data": data}


def _command(*args, **kwargs):
    def decorator(fn):
        return fn
    return decorator


# ---------------------------------------------------------------------------
# fake core module (core/functions.py loads fine against it: it only uses
# core.debug / core.manager inside functions we never call here)
# ---------------------------------------------------------------------------
_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_path = lambda p: os.path.abspath(p)
sys.modules["core"] = _core_mod  # must exist before core/functions.py's `import core`

_spec = importlib.util.spec_from_file_location(
    "ol_core_functions_under_test", os.path.join(ROOT, "core", "functions.py"))
_functions = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _functions
_spec.loader.exec_module(_functions)
_core_mod.sandbox_path = _functions.sandbox_path

_channel_mod = types.ModuleType("core.channel")
_channel_mod.Channel = type("Channel", (), {})
_core_mod.channel = _channel_mod

_module_mod = types.ModuleType("core.module")
_module_mod.Module = _ModuleBase
_module_mod.command = _command
_core_mod.module = _module_mod

_spec = importlib.util.spec_from_file_location(
    "ol_coder_under_test", os.path.join(ROOT, "modules", "coder.py"))
coder_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coder_mod)

_spec = importlib.util.spec_from_file_location(
    "ol_webui_under_test", os.path.join(ROOT, "channels", "webui.py"))
webui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webui_mod)


# ---------------------------------------------------------------------------
# fakes for the webui channel object (create_fastapi only touches
# channel.config.get("login_lifetime") and channel.assets_path at def time)
# ---------------------------------------------------------------------------
class _FakeManager:
    def __init__(self, modules):
        self.modules = modules


class _FakeChannel:
    def __init__(self, tmp_dir, modules):
        self.name = "webui"
        self.config = _FakeConfig({"require_login": False, "login_lifetime": 30})
        self.assets_path = tmp_dir
        self.path = tmp_dir
        self.manager = _FakeManager(modules)

    def log(self, *args, **kwargs):
        pass


def _make_coder(tmp_dir):
    coder = coder_mod.Coder()
    coder.config = _FakeConfig({
        "sandbox_paths": [tmp_dir],
        "enable_builtin_templates": False,
    })
    return coder


class CoderOpenPreviewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_preview_test_")
        self.sandbox_name = os.path.basename(self.tmp.rstrip("/\\"))
        self.coder = _make_coder(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_open_preview_existing_file(self):
        with open(os.path.join(self.tmp, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html><body>hi</body></html>")

        res = await self.coder.open_preview(self.sandbox_name, "index.html")

        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["preview"], {"sandbox": self.sandbox_name, "path": "index.html"})

    async def test_open_preview_missing_file(self):
        res = await self.coder.open_preview(self.sandbox_name, "nope.html")
        self.assertFalse(res["success"])

    async def test_open_preview_nested_file(self):
        site_dir = os.path.join(self.tmp, "site")
        os.makedirs(site_dir)
        with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>")

        res = await self.coder.open_preview(self.sandbox_name, "site/index.html")
        self.assertTrue(res["success"])


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ArtifactRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_preview_route_")
        self.sandbox_name = os.path.basename(self.tmp.rstrip("/\\"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def asyncSetUp(self):
        self.app = await webui_mod.create_fastapi(_FakeChannel(self.tmp, {}))
        self.client = TestClient(self.app)

    def _write_site(self):
        site_dir = os.path.join(self.tmp, "site")
        os.makedirs(site_dir, exist_ok=True)
        with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html><head><link rel='stylesheet' href='styles.css'></head><body>hat shop</body></html>")
        with open(os.path.join(site_dir, "styles.css"), "w", encoding="utf-8") as f:
            f.write("body { background: #fff; }")

    def test_503_when_coder_module_absent(self):
        self._write_site()
        resp = self.client.get(f"/api/artifact/{self.sandbox_name}/site/index.html")
        self.assertEqual(resp.status_code, 503)

    async def test_route_with_coder_present(self):
        self._write_site()
        app = await webui_mod.create_fastapi(_FakeChannel(self.tmp, {"coder": _make_coder(self.tmp)}))
        client = TestClient(app)

        # html entry point
        resp = client.get(f"/api/artifact/{self.sandbox_name}/site/index.html")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("no-store", resp.headers["cache-control"])
        self.assertIn("hat shop", resp.text)

        # relative asset (multi-file support)
        resp = client.get(f"/api/artifact/{self.sandbox_name}/site/styles.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/css", resp.headers["content-type"])
        self.assertIn("no-store", resp.headers["cache-control"])

        # missing file -> 404
        resp = client.get(f"/api/artifact/{self.sandbox_name}/site/missing.html")
        self.assertEqual(resp.status_code, 404)

        # traversal attempt -> 404 (core.sandbox_path rejects '..')
        resp = client.get(f"/api/artifact/{self.sandbox_name}/%2e%2e/%2e%2e/test")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
