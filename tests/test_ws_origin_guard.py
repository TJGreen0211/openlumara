"""
Self-contained unit test for the WebSocket cross-origin guard in channels/webui.py:
  - Webui.websocket_origin_allowed: missing origin (non-browser) allowed, same-origin
    allowed, cross-origin rejected, localhost origins allowed, X-Forwarded-Host and
    configured custom host allowed, malformed origin rejected.
  - end-to-end via TestClient: a cross-origin /ws handshake is refused before accept,
    a same-origin one is accepted and receives the ready message.

It does NOT import the real `core` package. Instead it injects a lightweight
stand-in `core` module into sys.modules, like tests/test_html_preview.py.

Run:  venv\\Scripts\\python tests\\test_ws_origin_guard.py
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
except ImportError:
    TestClient = None
    WebSocketDisconnect = None


class _FakeConfig:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value


# ---------------------------------------------------------------------------
# fake core module, then load the real webui module against it
# ---------------------------------------------------------------------------
_core_mod = types.ModuleType("core")
_core_mod.debug = False
_core_mod.log = lambda category, msg: None
_core_mod.get_path = lambda p: os.path.abspath(p)
sys.modules["core"] = _core_mod

_channel_mod = types.ModuleType("core.channel")
_channel_mod.Channel = type("Channel", (), {})
_core_mod.channel = _channel_mod

_spec = importlib.util.spec_from_file_location(
    "ol_webui_origin_under_test", os.path.join(ROOT, "channels", "webui.py"))
webui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webui_mod)


class _FakeWebSocket:
    def __init__(self, headers):
        self.headers = headers


def _allowed(host, headers):
    channel = webui_mod.Webui.__new__(webui_mod.Webui)
    channel.host = host
    return channel.websocket_origin_allowed(_FakeWebSocket(headers))


class OriginAllowedTest(unittest.TestCase):
    def test_missing_origin_allowed(self):
        self.assertTrue(_allowed("127.0.0.1", {}))

    def test_same_origin_allowed(self):
        self.assertTrue(_allowed("127.0.0.1", {
            "origin": "http://localhost:3000", "host": "localhost:3000"}))
        self.assertTrue(_allowed("0.0.0.0", {
            "origin": "http://192.168.1.5:3000", "host": "192.168.1.5:3000"}))

    def test_cross_origin_rejected(self):
        self.assertFalse(_allowed("127.0.0.1", {
            "origin": "http://evil.example.com", "host": "localhost:3000"}))
        self.assertFalse(_allowed("127.0.0.1", {
            "origin": "null", "host": "localhost:3000"}))

    def test_localhost_origin_always_allowed(self):
        self.assertTrue(_allowed("0.0.0.0", {
            "origin": "http://127.0.0.1:3000", "host": "192.168.1.5:3000"}))
        self.assertTrue(_allowed("0.0.0.0", {
            "origin": "http://[::1]:3000", "host": "192.168.1.5:3000"}))

    def test_forwarded_host_allowed(self):
        self.assertTrue(_allowed("127.0.0.1", {
            "origin": "https://lumara.example.org", "host": "127.0.0.1:3000",
            "x-forwarded-host": "lumara.example.org"}))

    def test_custom_host_allowed(self):
        self.assertTrue(_allowed("lumara.example.org", {
            "origin": "https://lumara.example.org", "host": "lumara.example.org"}))

    def test_malformed_origin_rejected(self):
        self.assertFalse(_allowed("127.0.0.1", {
            "origin": "not a url ]", "host": "localhost:3000"}))


class _FakeChannel:
    def __init__(self, tmp_dir, require_login=False):
        self.name = "webui"
        self.config = _FakeConfig({"require_login": require_login, "login_lifetime": 30})
        self.login_attempts = {}
        self.assets_path = tmp_dir
        self.path = tmp_dir
        self.host = "127.0.0.1"
        self.websocket_manager = webui_mod.WebSocketManager(self)

    def log(self, *args, **kwargs):
        pass

    def _get_session_secret(self):
        return "test-session-secret"

    def websocket_origin_allowed(self, websocket):
        return webui_mod.Webui.websocket_origin_allowed(self, websocket)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class WsHandshakeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_ws_origin_")
        self.app = await webui_mod.create_fastapi(_FakeChannel(self.tmp))
        self.client = TestClient(self.app)

    def test_cross_origin_handshake_refused(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws", headers={
                    "origin": "http://evil.example.com"}) as ws:
                ws.receive_json()

    def test_same_origin_handshake_accepted(self):
        with self.client.websocket_connect("/ws", headers={
                "origin": "http://localhost:3000", "host": "localhost:3000"}) as ws:
            self.assertEqual(ws.receive_json().get("type"), "ready")

    def test_non_browser_handshake_accepted(self):
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json().get("type"), "ready")


def _signed_session_cookie(data, secret="test-session-secret"):
    """build a session cookie exactly like starlette's SessionMiddleware does"""
    import base64
    import json
    import itsdangerous
    payload = base64.b64encode(json.dumps(data).encode("utf-8"))
    return itsdangerous.TimestampSigner(secret).sign(payload).decode("utf-8")


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class WsRequireLoginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_ws_auth_")
        self.app = await webui_mod.create_fastapi(_FakeChannel(self.tmp, require_login=True))
        self.client = TestClient(self.app)

    def _assert_refused(self, cookies=None):
        if cookies:
            self.client.cookies.update(cookies)
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect("/ws") as ws:
                ws.receive_json()
        self.assertEqual(cm.exception.code, 4001)

    def test_no_cookie_refused(self):
        self._assert_refused()

    def test_forged_cookie_refused(self):
        # a garbage cookie fails signature verification, leaving an empty session
        self._assert_refused({"session": "x"})

    def test_wrong_secret_cookie_refused(self):
        self._assert_refused({"session": _signed_session_cookie(
            {"authenticated": True, "username": "admin"}, secret="not-the-secret")})

    def test_signed_but_unauthenticated_session_refused(self):
        # e.g. a session that only carries a csrf token from the login page
        self._assert_refused({"session": _signed_session_cookie({"_csrf": "abc"})})

    def test_valid_login_session_accepted(self):
        self.client.cookies.update({"session": _signed_session_cookie(
            {"authenticated": True, "username": "admin", "role": "admin"})})
        with self.client.websocket_connect("/ws") as ws:
            self.assertEqual(ws.receive_json().get("type"), "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
