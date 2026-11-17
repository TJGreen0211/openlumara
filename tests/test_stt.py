"""
Self-contained unit test for core.stt.transcribe (voice input): it goes to the configured
speech server, or to the main API's /audio/transcriptions when none is set, and its
output is cleaned up.

Loads core/stt.py with stand-in `core` / `core.config` modules (the app is never
bootstrapped) and drives it against a local mock server.

Run:  venv\\Scripts\\python tests\\test_stt.py
"""
import asyncio
import importlib.util
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STT_PATH = os.path.join(os.path.dirname(HERE), "core", "stt.py")

_CFG = {}


def _fake_get(*args, default=None):
    return _CFG.get(args[-1], default)


_core = types.ModuleType("core")
_core.log = lambda category, msg: None
_core_config = types.ModuleType("core.config")
_core_config.get = _fake_get
_core.config = _core_config
sys.modules.setdefault("core", _core)
sys.modules.setdefault("core.config", _core_config)

_spec = importlib.util.spec_from_file_location("ol_core_stt_routing_under_test", STT_PATH)
stt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt)


class _Handler(BaseHTTPRequestHandler):
    requests = []
    reply = "hello"

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Handler.requests.append((self.path, body))
        if self.path.endswith("/audio/transcriptions") and self.path.startswith("/api"):
            payload, ctype = ('{"text": "%s"}' % _Handler.reply).encode(), "application/json"
        else:
            payload, ctype = _Handler.reply.encode(), "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class Transcribe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CFG.clear()
        _Handler.requests.clear()
        _Handler.reply = "hello"

    def run_transcribe(self):
        return asyncio.run(stt.transcribe(b"RIFF....WAVE", language="en"))

    def test_speech_server_wins(self):
        _CFG.update({"stt_whisper_server_url": self.base + "/v1/audio/transcriptions", "url": self.base + "/api"})
        self.assertEqual(self.run_transcribe(), "hello")
        self.assertEqual([p for p, _ in _Handler.requests], ["/v1/audio/transcriptions"])
        self.assertIn(b'name="language"', _Handler.requests[0][1])

    def test_per_user_voice_url(self):
        _CFG.update({"voice_url": self.base + "/inference"})
        self.assertEqual(self.run_transcribe(), "hello")
        self.assertEqual([p for p, _ in _Handler.requests], ["/inference"])

    def test_main_api_when_no_speech_server(self):
        _CFG.update({"url": self.base + "/api"})
        self.assertEqual(self.run_transcribe(), "hello")
        self.assertEqual([p for p, _ in _Handler.requests], ["/api/audio/transcriptions"])

    def test_nothing_configured(self):
        with self.assertRaises(stt.STTError):
            self.run_transcribe()
        self.assertEqual(_Handler.requests, [])

    def test_output_is_cleaned(self):
        _CFG.update({"stt_whisper_server_url": self.base + "/v1/audio/transcriptions"})
        _Handler.reply = "[BLANK_AUDIO] Incluye las tareas pendientes.¿Quién es responsable?"
        self.assertEqual(self.run_transcribe(), "Incluye las tareas pendientes. ¿Quién es responsable?")

    def test_no_local_engine_left(self):
        for name in ("transcribe_local", "_local_ready", "LOCAL_MODELS"):
            self.assertFalse(hasattr(stt, name), name)


class Clean(unittest.TestCase):
    def test_inverted_marks(self):
        self.assertEqual(stt.clean_transcription("Bien.¡Vamos!"), "Bien. ¡Vamos!")

    def test_leaves_normal_text_alone(self):
        for text in ("Ya está. ¿Y tú?", "It costs 3.50 dollars.", "¿Qué?"):
            self.assertEqual(stt.clean_transcription(text), text)


if __name__ == "__main__":
    unittest.main()
