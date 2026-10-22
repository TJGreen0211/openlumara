"""
Self-contained unit test for per-user sandboxes and site media:
  - modules/coder.py: per-user sandbox scoping, saving/listing/deleting site media,
    placeholders (+ replacing them with reference rewriting), the URL import ssrf guard
  - channels/webui.py: save_attachments_to_site() and the /api/site-media routes

Like test_html_preview.py, it injects a lightweight stand-in `core` module into
sys.modules (reusing the REAL core/functions.py sandbox_path) so the app is never
bootstrapped.

Run:  venv\\Scripts\\python tests\\test_site_media.py
"""
import contextvars
import importlib.util
import json
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

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'


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


_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.detail_error = lambda e: str(e)
_core_mod.get_path = lambda p: os.path.abspath(p)
_core_mod.current_user = contextvars.ContextVar("current_user", default=None)
_core_mod.data_root = tempfile.gettempdir()   # pointed at a temp dir by the data-folder tests


def _get_data_path(subpath=None, user=None):
    """mirrors core.get_data_path(): user=None reads the context, "" forces the global folder"""
    if user is None:
        user = _core_mod.current_user.get()
    path = os.path.join(_core_mod.data_root, user) if user else _core_mod.data_root
    os.makedirs(path, exist_ok=True)
    return _core_mod.sandbox_path(path, subpath) if subpath else path


_core_mod.get_data_path = _get_data_path


def _load_user_config(username):
    path = _get_data_path("config.json", user=username)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_user_config(username, config_dict):
    with open(_get_data_path("config.json", user=username), "w", encoding="utf-8") as f:
        json.dump(config_dict, f)


_config_mod = types.ModuleType("core.config")
_config_mod.load_user_config = _load_user_config
_config_mod.save_user_config = _save_user_config
_core_mod.config = _config_mod
sys.modules["core"] = _core_mod

_spec = importlib.util.spec_from_file_location(
    "ol_core_functions_media_test", os.path.join(ROOT, "core", "functions.py"))
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
    "ol_coder_media_test", os.path.join(ROOT, "modules", "coder.py"))
coder_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coder_mod)

_spec = importlib.util.spec_from_file_location(
    "ol_webui_media_test", os.path.join(ROOT, "channels", "webui.py"))
webui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webui_mod)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _make_coder(tmp_dir, **extra):
    coder = coder_mod.Coder()
    coder.config = _FakeConfig({
        "sandbox_paths": [tmp_dir],
        "enable_builtin_templates": False,
        "per_user_sandboxes": True,
        "max_media_size_mb": 1,
        "folder_blacklist": ["venv", "__pycache__"],
        **extra,
    })
    return coder


class _TmpSandbox(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_media_test_")
        self.sb = os.path.basename(self.tmp.rstrip("/\\"))
        self.coder = _make_coder(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _as(self, user):
        token = _core_mod.current_user.set(user)
        self.addCleanup(_core_mod.current_user.reset, token)


class PerUserSandboxTest(_TmpSandbox):
    async def test_users_get_private_folders(self):
        self._as("alice")
        res = await self.coder.file_create(self.sb, "site/index.html", "<p>alice</p>")
        self.assertTrue(res["success"], res)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "users", "alice", "site", "index.html")))

        self._as("bob")
        res = await self.coder.file_read(self.sb, "site/index.html")
        self.assertFalse(res.get("success"), "bob must not see alice's file")

        # and traversal out of bob's folder into alice's is refused
        with self.assertRaises(Exception):
            await self.coder._get_sandbox_subpath(self.sb, "../alice/site/index.html")

    async def test_single_user_mode_uses_the_sandbox_root(self):
        await self.coder.file_create(self.sb, "index.html", "<p>hi</p>")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "index.html")))

    async def test_setting_off_shares_the_sandbox(self):
        self.coder.config.set("per_user_sandboxes", False)
        self._as("alice")
        await self.coder.file_create(self.sb, "index.html", "<p>hi</p>")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "index.html")))


class DataFolderSandboxTest(unittest.IsolatedAsyncioTestCase):
    """sandboxes given as a plain name live in the (per-user) data folder"""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_datasandbox_test_")
        self.data = os.path.join(self.tmp, "data")
        self.legacy = os.path.join(self.tmp, "home", "coder")
        self._old_root = _core_mod.data_root
        _core_mod.data_root = self.data
        self.coder = _make_coder(self.tmp, sandbox_paths=["coder"])
        self.coder.legacy_sandbox_path = self.legacy

    def tearDown(self):
        _core_mod.data_root = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _as(self, user):
        token = _core_mod.current_user.set(user)
        self.addCleanup(_core_mod.current_user.reset, token)

    def _write(self, path, text="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    async def test_per_user_folders_in_the_data_folder(self):
        self._as("alice")
        res = await self.coder.file_create("coder", "site/index.html", "<p>alice</p>")
        self.assertTrue(res["success"], res)
        self.assertTrue(os.path.isfile(os.path.join(self.data, "alice", "coder", "coder", "site", "index.html")))

        self._as("bob")
        res = await self.coder.file_read("coder", "site/index.html")
        self.assertFalse(res.get("success"), "bob must not see alice's file")
        with self.assertRaises(Exception):
            await self.coder._get_sandbox_subpath("coder", "../../../alice/coder/coder/site/index.html")

    async def test_single_user_and_shared_mode_use_the_global_data_folder(self):
        await self.coder.file_create("coder", "a.html", "<p>a</p>")
        self.assertTrue(os.path.isfile(os.path.join(self.data, "coder", "coder", "a.html")))

        self.coder.config.set("per_user_sandboxes", False)
        self._as("alice")
        await self.coder.file_create("coder", "b.html", "<p>b</p>")
        self.assertTrue(os.path.isfile(os.path.join(self.data, "coder", "coder", "b.html")))
        self.assertFalse(os.path.exists(os.path.join(self.data, "alice", "coder")))

    async def test_migrates_each_users_legacy_folder_on_first_use(self):
        self._write(os.path.join(self.legacy, "users", "alice", "site", "index.html"), "alice")
        self._write(os.path.join(self.legacy, "users", "bob", "index.html"), "bob")

        self._as("alice")
        await self.coder._get_full_sandbox_path("coder")
        self.assertEqual(_read(os.path.join(self.data, "alice", "coder", "coder", "site", "index.html")), "alice")
        self.assertFalse(os.path.exists(os.path.join(self.legacy, "users", "alice")))
        # bob's files wait for bob
        self.assertTrue(os.path.isfile(os.path.join(self.legacy, "users", "bob", "index.html")))

        self._as("bob")
        await self.coder._get_full_sandbox_path("coder")
        self.assertEqual(_read(os.path.join(self.data, "bob", "coder", "coder", "index.html")), "bob")
        # the emptied legacy folders are removed
        self.assertFalse(os.path.exists(self.legacy))

    async def test_migrates_single_user_files_but_not_other_users(self):
        self._write(os.path.join(self.legacy, "index.html"), "root")
        self._write(os.path.join(self.legacy, "users", "alice", "index.html"), "alice")

        await self.coder._get_full_sandbox_path("coder")
        self.assertEqual(_read(os.path.join(self.data, "coder", "coder", "index.html")), "root")
        self.assertFalse(os.path.exists(os.path.join(self.data, "coder", "coder", "users")))
        self.assertTrue(os.path.isfile(os.path.join(self.legacy, "users", "alice", "index.html")))

    async def test_migration_never_overwrites(self):
        self._write(os.path.join(self.legacy, "users", "alice", "index.html"), "old")
        self._write(os.path.join(self.legacy, "users", "alice", "other.html"), "other")
        self._write(os.path.join(self.data, "alice", "coder", "coder", "index.html"), "new")

        self._as("alice")
        await self.coder._get_full_sandbox_path("coder")
        root = os.path.join(self.data, "alice", "coder", "coder")
        self.assertEqual(_read(os.path.join(root, "index.html")), "new")
        self.assertEqual(_read(os.path.join(root, "other.html")), "other")
        self.assertEqual(_read(os.path.join(self.legacy, "users", "alice", "index.html")), "old")

    async def test_absolute_sandboxes_are_not_migrated(self):
        self._write(os.path.join(self.legacy, "users", "alice", "index.html"), "alice")
        elsewhere = os.path.join(self.tmp, "projects", "coder")
        self.coder.config.set("sandbox_paths", [elsewhere])

        self._as("alice")
        root = await self.coder._get_full_sandbox_path("coder")
        self.assertEqual(root, os.path.join(elsewhere, "users", "alice"))
        self.assertTrue(os.path.isfile(os.path.join(self.legacy, "users", "alice", "index.html")))
        self.assertFalse(os.path.exists(os.path.join(self.data, "alice", "coder")))

    async def test_on_ready_replaces_the_old_default_path(self):
        self.coder.name = "coder"
        self.coder.legacy_sandbox_path = "~/coder"
        self.coder.config.set("sandbox_paths", ["~/coder/", "/srv/projects"])
        # a user's config.json can override module settings
        _save_user_config("alice", {"api": {"url": "x"},
                                    "modules": {"settings": {"coder": {"sandbox_paths": ["~/coder"]}}}})
        _save_user_config("bob", {"api": {"url": "y"}})

        await self.coder.on_ready()
        self.assertEqual(self.coder.config.get("sandbox_paths"), ["coder", "/srv/projects"])
        self.assertEqual(_load_user_config("alice"),
                         {"api": {"url": "x"}, "modules": {"settings": {"coder": {"sandbox_paths": ["coder"]}}}})
        self.assertEqual(_load_user_config("bob"), {"api": {"url": "y"}})

    async def test_old_default_path_resolves_to_the_data_folder(self):
        # eg. a per-user override that still says ~/coder: it's migrated, not used as-is
        self._write(os.path.join(self.legacy, "users", "alice", "index.html"), "alice")
        self.coder.legacy_sandbox_path = self.legacy
        self.coder.config.set("sandbox_paths", [self.legacy])

        self._as("alice")
        root = await self.coder._get_full_sandbox_path("coder")
        self.assertEqual(root, os.path.join(self.data, "alice", "coder", "coder"))
        self.assertEqual(_read(os.path.join(root, "index.html")), "alice")


class SiteMediaTest(_TmpSandbox):
    async def test_save_sniffs_type_and_never_overwrites(self):
        first = await self.coder._save_site_media(self.sb, "bakery", "My Logo!.PNG", PNG)
        self.assertEqual(first, {"name": "my-logo.png", "path": "media/my-logo.png"})

        # same name again -> unique name; extension follows the real type, not the filename
        second = await self.coder._save_site_media(self.sb, "bakery", "my logo.gif", PNG)
        self.assertEqual(second["name"], "my-logo-2.png")

        svg = await self.coder._save_site_media(self.sb, "", "icon.svg", SVG)
        self.assertEqual(svg["path"], "media/icon.svg")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "media", "icon.svg")))

    async def test_save_refuses_non_media_and_oversized(self):
        with self.assertRaises(ValueError):
            await self.coder._save_site_media(self.sb, "bakery", "evil.png", b"<html><script>x</script></html>")
        with self.assertRaises(ValueError):
            await self.coder._save_site_media(self.sb, "bakery", "huge.png", PNG + b"\x00" * (1024 * 1024))

    async def test_list_and_delete(self):
        await self.coder._save_site_media(self.sb, "bakery", "hero.jpg", JPEG)
        await self.coder.media_placeholder(self.sb, "bakery", "team 1", 400, 300)

        items = await self.coder._list_site_media(self.sb, "bakery")
        self.assertEqual([(i["name"], i["placeholder"]) for i in items],
                         [("hero.jpg", False), ("placeholder-team-1.svg", True)])

        await self.coder._delete_site_media(self.sb, "bakery", "hero.jpg")
        self.assertEqual(len(await self.coder._list_site_media(self.sb, "bakery")), 1)

        for bad in ("../index.html", "sub/hero.jpg", ".hidden", ""):
            with self.assertRaises(Exception):
                await self.coder._delete_site_media(self.sb, "bakery", bad)

    async def test_placeholder_replace_rewrites_references(self):
        res = await self.coder.media_placeholder(self.sb, "bakery", "Hero", 1200, 600, label="Hero <photo>")
        self.assertEqual(res["data"]["path"], "media/placeholder-hero.svg")
        svg = _read(os.path.join(self.tmp, "bakery", "media", "placeholder-hero.svg"))
        self.assertIn("Hero &lt;photo&gt;", svg)

        site = os.path.join(self.tmp, "bakery")
        os.makedirs(os.path.join(site, "blog"))
        with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
            f.write('<img src="media/placeholder-hero.svg">')
        with open(os.path.join(site, "blog", "post.html"), "w", encoding="utf-8") as f:
            f.write('<img src="../media/placeholder-hero.svg">')
        with open(os.path.join(site, "style.css"), "w", encoding="utf-8") as f:
            f.write("body { color: red; }")

        result = await self.coder._replace_site_placeholder(self.sb, "bakery", "placeholder-hero.svg",
                                                            "beach.jpg", JPEG)
        self.assertEqual(result["path"], "media/beach.jpg")
        self.assertEqual(sorted(result["files_updated"]), ["blog/post.html", "index.html"])
        self.assertIn('src="media/beach.jpg"', _read(os.path.join(site, "index.html")))
        self.assertIn('src="../media/beach.jpg"', _read(os.path.join(site, "blog", "post.html")))
        self.assertFalse(os.path.exists(os.path.join(site, "media", "placeholder-hero.svg")))

    async def test_url_import_refuses_private_addresses(self):
        for url in ("http://127.0.0.1/a.png", "http://localhost:3000/api/artifact/x", "http://10.0.0.5/a.png",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/a.png", "file:///etc/passwd",
                    "ftp://example.com/a.png"):
            res = await self.coder.media_import_url(self.sb, "bakery", url)
            self.assertFalse(res["success"], url)


class SaveAttachmentsTest(_TmpSandbox):
    async def test_media_attachments_land_in_the_site(self):
        note = await webui_mod.save_attachments_to_site(
            self.coder, {"sandbox": self.sb, "folder": "bakery"},
            [("logo.png", PNG), ("notes.pdf", b"%PDF-1.4 ..."), ("team.jpg", JPEG)])
        self.assertIn("logo.png → media/logo.png", note)
        self.assertIn("team.jpg → media/team.jpg", note)
        self.assertNotIn("notes.pdf", note)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "bakery", "media", "logo.png")))

    async def test_no_site_or_no_media_means_no_note(self):
        self.assertIsNone(await webui_mod.save_attachments_to_site(self.coder, None, [("a.png", PNG)]))
        self.assertIsNone(await webui_mod.save_attachments_to_site(
            self.coder, {"sandbox": self.sb, "folder": "x"}, [("a.txt", b"hello")]))
        self.assertIsNone(await webui_mod.save_attachments_to_site(
            None, {"sandbox": self.sb, "folder": "x"}, [("a.png", PNG)]))


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

    def _get_session_secret(self):
        return "test-session-secret"


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class SiteMediaRouteTest(_TmpSandbox):
    async def asyncSetUp(self):
        app = await webui_mod.create_fastapi(_FakeChannel(self.tmp, {"coder": self.coder}))
        self.client = TestClient(app)

    def test_upload_list_serve_delete(self):
        resp = self.client.post("/api/site-media/upload", data={"sandbox": self.sb, "folder": "bakery"},
                                files=[("files", ("hero.png", PNG, "image/png")),
                                       ("files", ("page.html", b"<html></html>", "text/html"))])
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual([s["name"] for s in body["data"]["saved"]], ["hero.png"])
        self.assertEqual(len(body["data"]["errors"]), 1)

        items = self.client.get("/api/site-media", params={"sandbox": self.sb, "folder": "bakery"}).json()["data"]
        self.assertEqual([i["name"] for i in items], ["hero.png"])

        # the preview iframe/thumbnails load it through the artifact route
        token = self.client.get("/api/artifact-token",
                                params={"sandbox": self.sb, "folder": "bakery"}).json()["data"]["token"]
        self.assertEqual(self.client.get(f"/api/artifact/{token}/{self.sb}/bakery/media/hero.png").status_code, 200)

        resp = self.client.post("/api/site-media/delete",
                                json={"sandbox": self.sb, "folder": "bakery", "name": "hero.png"})
        self.assertTrue(resp.json()["success"])

    def test_import_blocks_localhost(self):
        resp = self.client.post("/api/site-media/import",
                                json={"sandbox": self.sb, "folder": "bakery", "url": "http://127.0.0.1:3000/x.png"})
        self.assertFalse(resp.json()["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
