"""Regression test for vLLM chunks that contain both reasoning and content."""
import asyncio
import importlib.util
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_core = types.ModuleType("core")
_core.config = types.SimpleNamespace(
    get=lambda key, default=None: {"use_tools": False} if key == "model" else default
)
sys.modules["core"] = _core

_spec = importlib.util.spec_from_file_location(
    "ol_api_under_test", os.path.join(ROOT, "core", "api.py")
)
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


class _Response:
    def __init__(self, chunk):
        self.chunk = chunk

    def __aiter__(self):
        async def chunks():
            yield self.chunk

        return chunks()


class ApiStreamMixedDeltaTest(unittest.TestCase):
    def test_preserves_content_when_delta_also_has_reasoning(self):
        client = api.APIClient(types.SimpleNamespace())
        client.cancel_request = False
        chunk = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    delta=types.SimpleNamespace(
                        content="\n\nThe",
                        reasoning=" to the user.\n",
                        tool_calls=None,
                    )
                )
            ]
        )

        async def collect():
            return [event async for event in client._recv_stream(_Response(chunk))]

        events = asyncio.run(collect())

        self.assertEqual(
            [(event["type"], event["content"]) for event in events],
            [
                ("reasoning", " to the user.\n"),
                ("content", "\n\nThe"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
