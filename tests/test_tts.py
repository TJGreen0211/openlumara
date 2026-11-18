"""
Self-contained unit test for core.tts (reading replies aloud).

Loads core/tts.py with stand-in `core` / `core.config` modules (the app is never
bootstrapped) and drives synthesize() against a local mock TTS server.

Run:  python tests/test_tts.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TTS_PATH = os.path.join(os.path.dirname(HERE), "core", "tts.py")

_CFG = {"tts_server_url": "", "voice_output_voice": "default", "voice_language": "en", "voice_output_speed": 1.0}


def _fake_get(*args, default=None):
    return _CFG.get(args[-1], default)


_core = types.ModuleType("core")
_core.log = lambda category, msg: None
_core_config = types.ModuleType("core.config")
_core_config.get = _fake_get
_core.config = _core_config
sys.modules.setdefault("core", _core)
sys.modules.setdefault("core.config", _core_config)

_spec = importlib.util.spec_from_file_location("ol_core_tts_under_test", TTS_PATH)
tts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tts)


class CleanForSpeech(unittest.TestCase):
    def test_markdown_symbols_are_not_read_out(self):
        md = "## Plan\n\n**Bold** and *italic* and `code` and ~~gone~~.\n\n- first item\n- second item\n1. numbered"
        out = tts.clean_for_speech(md)
        for symbol in ("#", "*", "`", "~~", "- "):
            self.assertNotIn(symbol, out)
        self.assertIn("Bold and italic and code and gone.", out)
        self.assertIn("first item", out)

    def test_code_blocks_and_tables_are_announced_not_read(self):
        md = "Run this:\n\n```python\nprint('hi')\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nDone."
        out = tts.clean_for_speech(md)
        self.assertNotIn("print", out)
        self.assertNotIn("|", out)
        self.assertIn("Code omitted.", out)
        self.assertIn("Table omitted.", out)
        self.assertEqual(out.count("Table omitted."), 1)

    def test_spanish_reply_announces_omissions_in_spanish(self):
        out = tts.clean_for_speech("```\nx\n```", "es")
        self.assertIn("Código omitido.", out)

    def test_links_urls_emoji_and_reasoning(self):
        md = "<think>private reasoning</think>See [the docs](https://x.y/z) or https://a.b/c 🎉 now."
        out = tts.clean_for_speech(md)
        self.assertEqual(out, "See the docs or now.")

    def test_snake_case_survives(self):
        self.assertIn("max_speakers", tts.clean_for_speech("Set max_speakers to 4."))


class Language(unittest.TestCase):
    def test_english_and_spanish(self):
        self.assertEqual(tts.detect_language("The release is ready and we will ship it on Friday.", "en"), "en")
        self.assertEqual(tts.detect_language("La versión está lista y la publicamos el viernes.", "en"), "es")

    def test_unclear_text_uses_the_fallback(self):
        self.assertEqual(tts.detect_language("OK.", "es"), "es")
        self.assertEqual(tts.detect_language("OK.", "en"), "en")

    def test_other_configured_languages_pass_through(self):
        self.assertEqual(tts.detect_language("The release is ready and we ship it.", "fr"), "fr")


class Plan(unittest.TestCase):
    def test_one_piece_per_run_of_one_language(self):
        md = ("First paragraph in English, about the release.\n\nSecond paragraph, also in English.\n\n"
              "Luis dijo que la reunión con finanzas es el lunes.\n\nY que enviará un resumen del presupuesto.\n\n"
              "Back to English for the wrap-up of the meeting.")
        pieces = tts.plan(md, "en")
        self.assertEqual([p["language"] for p in pieces], ["en", "es", "en"])
        self.assertEqual(pieces[0]["text"], "First paragraph in English, about the release.\n\n"
                                            "Second paragraph, also in English.")

    def test_long_replies_are_bounded_between_sentences(self):
        sentences = " ".join(f"This is sentence number {i} of a very long reply about the sprint." for i in range(200))
        pieces = tts.plan(sentences, "en")
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p["text"]) <= tts.MAX_PIECE_CHARS for p in pieces))
        self.assertTrue(all(p["text"].endswith(".") for p in pieces))
        self.assertEqual(" ".join(p["text"] for p in pieces), sentences)

    def test_empty_and_symbol_only_replies(self):
        self.assertEqual(tts.plan("", "en"), [])
        self.assertEqual(tts.plan("---\n\n***", "en"), [])


class Voices(unittest.TestCase):
    def tearDown(self):
        _CFG.pop("tts_voices", None)
        _CFG["voice_output_voice"] = "default"

    def test_list_entries_parse_to_names_and_descriptions(self):
        _CFG["tts_voices"] = ["default: Built-in", " ana : Warm Spanish voice ", "max", "", "ana: duplicate"]
        self.assertEqual(tts.voices(), {"default": "Built-in", "ana": "Warm Spanish voice", "max": ""})

    def test_pick_from_the_list_is_used(self):
        _CFG["tts_voices"] = ["default", "ana"]
        _CFG["voice_output_voice"] = "ana"
        self.assertEqual(tts.voice(), "ana")

    def test_pick_not_on_the_list_falls_back_to_the_first(self):
        _CFG["tts_voices"] = ["ana: Spanish", "default"]
        _CFG["voice_output_voice"] = "removed_voice"
        self.assertEqual(tts.voice(), "ana")

    def test_without_a_list_any_name_goes(self):
        _CFG["tts_voices"] = []
        _CFG["voice_output_voice"] = "my_clone"
        self.assertEqual(tts.voices(), {})
        self.assertEqual(tts.voice(), "my_clone")


class _Mock(BaseHTTPRequestHandler):
    status = 200
    chunks = [b"\x01\x00" * 100, b"\x02\x00" * 100, b"\x03\x00" * 50]
    content_type = "audio/pcm"
    received = None
    finished = None

    def do_POST(self):
        _Mock.received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(_Mock.status)
        self.send_header("Content-Type", _Mock.content_type)
        self.send_header("X-Sample-Rate", "24000")
        if _Mock.status != 200:
            body = b'{"detail": "bad language"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for c in _Mock.chunks:
                self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            _Mock.finished = True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            _Mock.finished = False

    def log_message(self, *a):
        pass


class Streaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1/audio/speech"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CFG["tts_server_url"] = self.url
        _Mock.status, _Mock.content_type, _Mock.finished = 200, "audio/pcm", None

    @staticmethod
    def collect(text, language):
        async def run():
            rate, chunks = await tts.open_speech(text, language)
            return rate, [c async for c in chunks]
        return asyncio.run(run())

    def test_streams_pcm_with_text_language_and_voice(self):
        _CFG["voice_output_voice"] = "ana"
        rate, chunks = self.collect("Hola a todos.", "es")
        _CFG["voice_output_voice"] = "default"
        self.assertEqual(rate, 24000)
        self.assertEqual(b"".join(chunks), b"".join(_Mock.chunks))
        self.assertGreater(len(chunks), 1, "audio should arrive in pieces, not all at once")
        self.assertEqual(_Mock.received, {"input": "Hola a todos.", "language": "es", "voice": "ana",
                                          "response_format": "pcm"})

    def test_speed_is_sent_only_when_changed(self):
        _CFG["voice_output_speed"] = 1.5
        try:
            self.collect("Hello.", "en")
            self.assertEqual(_Mock.received["speed"], 1.5)
            _CFG["voice_output_speed"] = 9
            self.collect("Hello.", "en")
            self.assertEqual(_Mock.received["speed"], 4.0)
            _CFG["voice_output_speed"] = "fast"
            self.collect("Hello.", "en")
            self.assertNotIn("speed", _Mock.received)
        finally:
            _CFG["voice_output_speed"] = 1.0

    def test_unsupported_language_falls_back(self):
        self.collect("Hello.", "xx")
        self.assertEqual(_Mock.received["language"], "en")

    def test_stopping_early_closes_the_connection(self):
        async def run():
            _rate, chunks = await tts.open_speech("Hello.", "en")
            async for _ in chunks:
                break
            await chunks.aclose()
        asyncio.run(run())
        time.sleep(0.4)
        self.assertNotEqual(_Mock.finished, True)

    def test_errors_raise_before_any_audio(self):
        _CFG["tts_server_url"] = ""
        with self.assertRaises(tts.TTSError):
            self.collect("Hello.", "en")
        _CFG["tts_server_url"] = self.url
        _Mock.status = 400
        with self.assertRaises(tts.TTSError):
            self.collect("Hello.", "en")
        _Mock.status, _Mock.content_type = 200, "application/json"
        with self.assertRaises(tts.TTSError):
            self.collect("Hello.", "en")
        _CFG["tts_server_url"] = "http://127.0.0.1:9/v1/audio/speech"
        with self.assertRaises(tts.TTSError):
            self.collect("Hello.", "en")


if __name__ == "__main__":
    unittest.main()
