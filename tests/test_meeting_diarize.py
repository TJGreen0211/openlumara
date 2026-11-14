"""
Self-contained unit test for core.stt.transcribe_meeting (post-meeting diarization).

Loads core/stt.py with stand-in `core` / `core.config` modules (the app is never
bootstrapped) and drives it against a local mock speech server.

Run:  python tests/test_meeting_diarize.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STT_PATH = os.path.join(os.path.dirname(HERE), "core", "stt.py")

_CFG = {"stt_whisper_server_url": "", "key": ""}


def _fake_get(*args, default=None):
    return _CFG.get(args[-1], default)


_core = types.ModuleType("core")
_core.log = lambda category, msg: None
_core_config = types.ModuleType("core.config")
_core_config.get = _fake_get
_core.config = _core_config
sys.modules.setdefault("core", _core)
sys.modules.setdefault("core.config", _core_config)

_spec = importlib.util.spec_from_file_location("ol_core_stt_under_test", STT_PATH)
stt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt)


class _Mock(BaseHTTPRequestHandler):
    status = 200
    body = {}
    received = b""

    def do_POST(self):
        _Mock.received = self.rfile.read(int(self.headers["Content-Length"]))
        out = json.dumps(_Mock.body).encode()
        self.send_response(_Mock.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class TranscribeMeetingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1/audio/transcriptions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CFG["stt_whisper_server_url"] = self.url
        _Mock.status = 200

    def run_parts(self, parts):
        return asyncio.run(stt.transcribe_meeting(parts))

    def test_sends_every_part_with_diarize_and_returns_segments(self):
        _Mock.body = {"segments": [
            {"start": 0.5, "end": 2.0, "speaker": "SPEAKER_0", "text": "Hola a todos."},
            {"start": 2.4, "end": 3.0, "speaker": "SPEAKER_1", "text": "[BLANK_AUDIO]"},
            {"start": 3.1, "end": 5.0, "speaker": "SPEAKER_1", "text": "Good morning."},
        ]}
        segs = self.run_parts([("part1.webm", b"AAA", "audio/webm"), ("part2.webm", b"BBB", "audio/webm")])
        self.assertEqual([s["text"] for s in segs], ["Hola a todos.", "Good morning."])
        self.assertEqual(segs[1]["speaker"], "SPEAKER_1")
        body = _Mock.received
        self.assertEqual(body.count(b'name="file"'), 2)
        self.assertLess(body.index(b"AAA"), body.index(b"BBB"))
        self.assertIn(b'name="diarize"\r\n\r\ntrue', body)
        self.assertIn(b'name="response_format"\r\n\r\nverbose_json', body)

    def test_server_error_raises(self):
        _Mock.status, _Mock.body = 500, {"detail": "boom"}
        with self.assertRaises(stt.STTError):
            self.run_parts([("part1.webm", b"AAA", "audio/webm")])

    def test_server_without_diarization_raises(self):
        _Mock.body = {"text": "a whisper server ignores diarize"}
        with self.assertRaises(stt.STTError):
            self.run_parts([("part1.webm", b"AAA", "audio/webm")])

    def test_no_url_raises(self):
        _CFG["stt_whisper_server_url"] = ""
        with self.assertRaises(stt.STTError):
            self.run_parts([("part1.webm", b"AAA", "audio/webm")])


if __name__ == "__main__":
    unittest.main()
