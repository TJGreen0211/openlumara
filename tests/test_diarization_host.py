"""
Backend end-to-end test: drives the openlumara proxy (core/diarization.py) against a
REAL diarization host running in MOCK mode (DIARIZE_MOCK=1, no GPU / no ffmpeg
needed - the mock pipeline fabricates a duration). This proves the proxy -> host
multipart contract, the non-blocking job flow, and the status/result shapes align over
actual HTTP.

It starts `uvicorn main:app` from diarization_host/ as a subprocess, waits for
/health, points the (fake) core.config base URL at it, and runs the proxy client.

Run:  python tests/test_diarization_host.py
"""
import asyncio
import httpx
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIAG_PATH = os.path.join(ROOT, "core", "diarization.py")
HOST_DIR = os.path.join(ROOT, "diarization_host")

# ---------------------------------------------------------------------------
# load core/diarization.py with a stand-in core (never pulls in the app)
# ---------------------------------------------------------------------------
_CFG = {"diarization_server_url": ""}


def _fake_config_get(*args, **kwargs):
    key = None
    for a in args:
        if isinstance(a, str):
            key = a
    if key in _CFG:
        return _CFG[key]
    return kwargs.get("default")


_core = types.ModuleType("core")
_core.log = lambda category, msg: None
_core_config = types.ModuleType("core.config")
_core_config.get = _fake_config_get
_core.config = _core_config
sys.modules["core"] = _core
sys.modules["core.config"] = _core_config

_spec = importlib.util.spec_from_file_location("ol_core_diarization_e2e", DIAG_PATH)
diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diag)


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HostE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = _free_port()
        env = os.environ.copy()
        env["DIARIZE_MOCK"] = "1"
        env["DIARIZE_MOCK_DURATION"] = "40"
        env["PYTHONIOENCODING"] = "utf-8"
        self._log_path = os.path.join(tempfile.gettempdir(), "ol_diag_host_e2e.log")
        self._log = open(self._log_path, "wb")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=HOST_DIR, env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )

        async def wait_ready():
            base = f"http://127.0.0.1:{self.port}"
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as c:
                for _ in range(150):
                    if self._proc.poll() is not None:
                        raise RuntimeError("host exited early:\n" + self._read_log())
                    try:
                        r = await c.get(base + "/health")
                        if r.status_code == 200 and r.json().get("model_ready"):
                            return
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)
            raise RuntimeError("mock host did not become ready\n" + self._read_log())

        await wait_ready()
        self.base = f"http://127.0.0.1:{self.port}"
        _CFG["diarization_server_url"] = self.base

    def _read_log(self):
        try:
            self._log.close()
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    async def asyncTearDown(self):
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        try:
            self._log.close()
        except Exception:
            pass
        try:
            os.remove(self._log_path)
        except Exception:
            pass

    async def test_full_round_trip(self):
        parts = [b"fake-opus-chunk-one" * 64, b"fake-opus-chunk-two" * 64]

        # submit
        job = await diag.diarize(parts, codec="audio/webm;codecs=opus", title="E2E")
        self.assertTrue(job, "a job id should be returned")

        # poll for completion (should flip processing -> done quickly in mock mode)
        state = "processing"
        for _ in range(100):
            state = (await diag.status(job))["state"]
            if state in ("done", "error"):
                break
            await asyncio.sleep(0.1)
        self.assertEqual(state, "done", "job should reach 'done' in mock mode")

        # result
        res = await diag.result(job)
        self.assertEqual(res["speakers"], 2)
        self.assertEqual(res["duration"], 40.0)
        self.assertEqual(res["labels"], [[0.0, 20.0, "SPEAKER_00"], [20.0, 40.0, "SPEAKER_01"]])
        self.assertEqual(res["speaker_names"], {"SPEAKER_00": None, "SPEAKER_01": None})

        # a bogus job -> 404 -> DiagError through the proxy
        with self.assertRaises(diag.DiagError):
            await diag.status("does-not-exist")
        with self.assertRaises(diag.DiagError):
            await diag.result("does-not-exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
