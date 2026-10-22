"""
Regression test for the shared stop flag in core/api.py: send()/send_stream()
used to reset APIClient.cancel_request on every new request, so any request
that started while another stream was being stopped (another user's message, a
background memory extraction) silently swallowed the /stop. A leftover flag is
now only cleared when no stream is running.

Run:  venv\\Scripts\\python tests\\test_api_cancel_flag.py
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_core = types.ModuleType("core")
_core.config = types.SimpleNamespace(get=lambda key, default=None: default)
sys.modules["core"] = _core

_spec = importlib.util.spec_from_file_location("ol_api_cancel_under_test", os.path.join(ROOT, "core", "api.py"))
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def make_client():
    client = api.APIClient(types.SimpleNamespace())

    async def not_connected():
        # stop right after the flag handling, without any network
        return api.APIError("not connected")

    client.attempt_connect = not_connected
    return client


async def drain(gen):
    return [item async for item in gen]


class CancelFlagTest(unittest.TestCase):
    def test_send_keeps_a_stop_meant_for_a_running_stream(self):
        client = make_client()
        client.is_streaming, client.cancel_request = True, True
        asyncio.run(client.send([{"role": "user", "content": "x"}]))
        self.assertTrue(client.cancel_request)

    def test_send_stream_keeps_a_stop_meant_for_a_running_stream(self):
        client = make_client()
        client.is_streaming, client.cancel_request = True, True
        asyncio.run(drain(client.send_stream([{"role": "user", "content": "x"}])))
        self.assertTrue(client.cancel_request)

    def test_a_leftover_flag_is_cleared_when_nothing_is_streaming(self):
        for call in ("send", "send_stream"):
            client = make_client()
            client.is_streaming, client.cancel_request = False, True
            result = getattr(client, call)([{"role": "user", "content": "x"}])
            asyncio.run(drain(result) if call == "send_stream" else result)
            self.assertFalse(client.cancel_request, call)

    def test_stop_completes_while_another_request_starts(self):
        """the user-visible bug: /stop during a stream while another request begins"""
        client = make_client()
        client.is_streaming = True

        async def scenario():
            stop = asyncio.create_task(client.cancel())
            await asyncio.sleep(0.01)
            await client.send([{"role": "user", "content": "background extraction"}])
            saw_stop = client.cancel_request  # what the running stream's loop checks
            client.cancel_request = False  # the stream honours it and resets, as _recv_stream does
            client.is_streaming = False
            return saw_stop, await asyncio.wait_for(stop, timeout=2)

        saw_stop, cancelled = asyncio.run(scenario())
        self.assertTrue(saw_stop)
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
