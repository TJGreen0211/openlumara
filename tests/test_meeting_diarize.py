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
    requests = []                 # [(method, path)]

    def do_POST(self):
        _Mock.requests.append(("POST", self.path))
        _Mock.received = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply()

    def do_DELETE(self):
        _Mock.requests.append(("DELETE", self.path))
        self._reply()

    def _reply(self):
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


class MeetingSessionTest(unittest.TestCase):
    """The streamed meeting session: start, append chunks, finish, discard."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CFG["stt_whisper_server_url"] = self.base + "/audio/transcriptions"
        _Mock.status = 200
        _Mock.requests = []

    def test_meetings_url_sits_next_to_the_transcription_endpoint(self):
        self.assertEqual(stt._meetings_url(), self.base + "/meetings")
        _CFG["stt_whisper_server_url"] = self.base + "/audio/transcriptions/"
        self.assertEqual(stt._meetings_url(), self.base + "/meetings")

    def test_whisper_cpp_style_url_has_no_sessions(self):
        _CFG["stt_whisper_server_url"] = "http://host:8080/inference"
        with self.assertRaises(stt.STTError):
            asyncio.run(stt.meeting_session_start())

    def test_start_append_finish(self):
        _Mock.body = {"id": "abc123"}
        self.assertEqual(asyncio.run(stt.meeting_session_start()), "abc123")

        _Mock.body = {"received_sec": 15}
        asyncio.run(stt.meeting_session_append("abc123", 2, "part2.webm", b"CHUNK", "audio/webm"))
        self.assertIn(b'name="part"\r\n\r\n2', _Mock.received)
        self.assertIn(b"CHUNK", _Mock.received)
        self.assertNotIn(b'name="seq"', _Mock.received)
        asyncio.run(stt.meeting_session_append("abc123", 2, "part2.webm", b"MORE", "audio/webm", 7))
        self.assertIn(b'name="seq"\r\n\r\n7', _Mock.received)

        _Mock.body = {"segments": [
            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_0", "text": "Hola."},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_1", "text": "<|nospeech|>"},
        ]}
        segs = asyncio.run(stt.meeting_session_finish("abc123"))
        self.assertEqual([s["text"] for s in segs], ["Hola."])
        self.assertEqual([r[1] for r in _Mock.requests],
                         ["/v1/meetings", "/v1/meetings/abc123/audio", "/v1/meetings/abc123/audio",
                          "/v1/meetings/abc123/finish"])

    def test_attendee_count_is_sent_as_max_speakers(self):
        _Mock.body = {"id": "s1"}
        asyncio.run(stt.meeting_session_start(4))
        self.assertIn(b"max_speakers=4", _Mock.received)   # url-encoded form (no file attached)
        asyncio.run(stt.meeting_session_start())
        self.assertNotIn(b"max_speakers", _Mock.received)

    def test_append_error_raises(self):
        _Mock.status, _Mock.body = 409, {"detail": "part 1 already ended"}
        with self.assertRaises(stt.STTError):
            asyncio.run(stt.meeting_session_append("abc", 1, "p.webm", b"x", "audio/webm"))

    def test_discard_is_best_effort(self):
        _Mock.status, _Mock.body = 404, {"detail": "unknown"}
        asyncio.run(stt.meeting_session_discard("gone"))   # must not raise
        self.assertEqual(_Mock.requests, [("DELETE", "/v1/meetings/gone")])


if __name__ == "__main__":
    unittest.main()
