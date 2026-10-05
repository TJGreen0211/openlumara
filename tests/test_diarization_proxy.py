"""
Self-contained unit test for core/diarization.py (the speaker-diarization proxy).

It does NOT import the real `core` package (that pulls in the whole app bootstrap and
network). Instead it injects two lightweight stand-in modules for `core` and
`core.config` into sys.modules, loads core/diarization.py as a standalone module, and
drives it against a local mock diarization host (a threaded http.server).

Run:  python tests/test_diarization_proxy.py
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
ROOT = os.path.dirname(HERE)
DIAG_PATH = os.path.join(ROOT, "core", "diarization.py")

# ---------------------------------------------------------------------------
# fake core / core.config so we can load core/diarization.py without the app
# ---------------------------------------------------------------------------
_CFG_STATE = {"diarization_server_url": ""}


def _fake_config_get(*args, **kwargs):
    key = None
    for a in args:
        if isinstance(a, str):
            key = a
    if key in _CFG_STATE:
        return _CFG_STATE[key]
    return kwargs.get("default")


_core_mod = types.ModuleType("core")
_core_mod.log = lambda category, msg: None
_core_config_mod = types.ModuleType("core.config")
_core_config_mod.get = _fake_config_get
_core_mod.config = _core_config_mod
sys.modules.setdefault("core", _core_mod)
sys.modules.setdefault("core.config", _core_config_mod)

_spec = importlib.util.spec_from_file_location("ol_core_diarization_under_test", DIAG_PATH)
diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diag)


# ---------------------------------------------------------------------------
# mock diarization host
# ---------------------------------------------------------------------------
class _MockDiag:
    received = {}
    jobs = {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/diarize":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        _MockDiag.received["/diarize"] = {
            "bytes": len(raw),
            "audio_fields": raw.count(b'name="audio"'),
            "codec": b'codec=' in raw or b'"codec"' in raw,
        }
        job = "mockjob123"
        _MockDiag.jobs[job] = {
            "state": "done",
            "result": {
                "speakers": 2,
                "speaker_names": {"SPEAKER_00": None, "SPEAKER_01": None},
                "labels": [[0.0, 3.0, "SPEAKER_00"], [3.0, 7.0, "SPEAKER_01"]],
                "duration": 7.0,
            },
        }
        self._send(202, {"job": job})

    def do_GET(self):
        if self.path.startswith("/diarize/status/"):
            job = self.path.rsplit("/", 1)[-1]
            j = _MockDiag.jobs.get(job)
            if not j:
                self._send(404, {"error": "not found"})
                return
            self._send(200, {"state": j["state"]})
        elif self.path.startswith("/diarize/result/"):
            job = self.path.rsplit("/", 1)[-1]
            j = _MockDiag.jobs.get(job)
            if not j or j["state"] != "done":
                self._send(404, {"error": "not ready"})
                return
            self._send(200, j["result"])
        else:
            self._send(404, {"error": "not found"})


class DiagProxyTest(unittest.IsolatedAsyncioTestCase):
    # one loop for the whole suite, because diag._diag_sem is a module-level
    # semaphore that binds to whichever loop first awaits it
    async def asyncSetUp(self):
        _MockDiag.received = {}
        _MockDiag.jobs = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    async def asyncTearDown(self):
        self._server.shutdown()
        self._server.server_close()

    def _base(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    async def test_proxy(self):
        # 1. no url configured -> DiagError with the exact message
        _CFG_STATE["diarization_server_url"] = ""
        with self.assertRaises(diag.DiagError) as ctx:
            await diag.diarize([b"abc"])
        self.assertIn("No diarization server configured", str(ctx.exception))
        with self.assertRaises(diag.DiagError):
            await diag.status("job")
        with self.assertRaises(diag.DiagError):
            await diag.result("job")

        # 2. configured but host unreachable (port 1) -> DiagError
        _CFG_STATE["diarization_server_url"] = "http://127.0.0.1:1"
        with self.assertRaises(diag.DiagError) as ctx2:
            await diag.status("job")
        self.assertIn("Could not reach the diarization host", str(ctx2.exception))

        # 3. happy path against the local mock host
        _CFG_STATE["diarization_server_url"] = self._base()
        parts = [b"\x01\x02\x03" * 10, b"\x04\x05\x06" * 10]
        job = await diag.diarize(parts, codec="audio/webm;codecs=opus", title="T")
        self.assertEqual(job, "mockjob123")

        got = _MockDiag.received["/diarize"]
        self.assertGreater(got["bytes"], 0)
        self.assertEqual(got["audio_fields"], 2)  # one file per part under "audio"

        state = await diag.status(job)
        self.assertEqual(state["state"], "done")

        result = await diag.result(job)
        self.assertEqual(result["speakers"], 2)
        self.assertEqual(result["labels"][0], [0.0, 3.0, "SPEAKER_00"])
        self.assertIsNone(result["speaker_names"].get("SPEAKER_01"))

        # 4. unknown job -> 404 -> DiagError
        with self.assertRaises(diag.DiagError) as ctx3:
            await diag.status("nope")
        self.assertIn("not found", str(ctx3.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
