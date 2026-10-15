"""
Self-contained unit test for the meeting-cutoff logic in core/context.py
(apply_cutoff: unified last-cutoff-wins rule for SUMMARIZATION/MEETING signals).

It does NOT import the real `core` package (that pulls in the whole app
bootstrap). Instead it injects a lightweight stand-in `core` module into
sys.modules, loads core/context.py as a standalone module, and drives the
pure apply_cutoff() helper directly.

Run:  python tests/test_meeting_cutoff.py
"""
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTEXT_PATH = os.path.join(ROOT, "core", "context.py")

_core_mod = types.ModuleType("core")
_core_mod.log = lambda category, msg: None
sys.modules.setdefault("core", _core_mod)

_spec = importlib.util.spec_from_file_location("ol_core_context_under_test", CONTEXT_PATH)
ctx_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctx_mod)

apply_cutoff = ctx_mod.apply_cutoff


def msg(content, signal=None):
    m = {"role": "user", "content": content}
    if signal:
        m["_metadata"] = {"signal": signal}
    return m


def contents(messages):
    return [m.get("content") for m in messages]


class TestApplyCutoff(unittest.TestCase):
    def test_no_cutoff_returns_history_unchanged(self):
        history = [msg("a"), msg("b"), msg("c")]
        result = apply_cutoff(history)
        self.assertEqual(contents(result), ["a", "b", "c"])

    def test_empty_history(self):
        self.assertEqual(apply_cutoff([]), [])

    def test_meeting_cutoff_drops_earlier_messages_without_prompt(self):
        history = [
            msg("old meeting transcript"),
            msg("old notes"),
            msg("New meeting", "MEETING_CUTOFF"),
            msg("new transcript"),
        ]
        result = apply_cutoff(history)

        # everything before the boundary is gone, the boundary itself is gone
        self.assertEqual(contents(result), ["new transcript"])
        # no summarization prompt injected
        self.assertNotIn("Summarize our chat so far.", contents(result))

    def test_summarization_cutoff_keeps_existing_behavior(self):
        history = [
            msg("before"),
            msg("cutoff", "SUMMARIZATION_CUTOFF"),
            msg("after"),
        ]
        result = apply_cutoff(history)

        self.assertEqual(contents(result), ["Summarize our chat so far.", "after"])

    def test_later_meeting_cutoff_wins_over_earlier_summarization(self):
        history = [
            msg("ancient"),
            msg("cutoff", "SUMMARIZATION_CUTOFF"),
            msg("middle"),
            msg("New meeting", "MEETING_CUTOFF"),
            msg("latest"),
        ]
        result = apply_cutoff(history)

        # the meeting boundary is the last cutoff: it wins, no summarize prompt
        self.assertEqual(contents(result), ["latest"])

    def test_later_summarization_cutoff_wins_over_earlier_meeting(self):
        history = [
            msg("ancient"),
            msg("New meeting", "MEETING_CUTOFF"),
            msg("middle"),
            msg("cutoff", "SUMMARIZATION_CUTOFF"),
            msg("latest"),
        ]
        result = apply_cutoff(history)

        self.assertEqual(contents(result), ["Summarize our chat so far.", "latest"])

    def test_multiple_meeting_boundaries_last_one_wins(self):
        history = [
            msg("meeting 1 transcript"),
            msg("New meeting", "MEETING_CUTOFF"),
            msg("meeting 2 transcript"),
            msg("New meeting", "MEETING_CUTOFF"),
            msg("meeting 3 transcript"),
        ]
        result = apply_cutoff(history)

        self.assertEqual(contents(result), ["meeting 3 transcript"])

    def test_cutoff_at_end_yields_empty_history(self):
        history = [msg("a"), msg("New meeting", "MEETING_CUTOFF")]
        self.assertEqual(apply_cutoff(history), [])

    def test_other_signals_are_not_cutoffs(self):
        history = [msg("a"), msg("b", "SOME_OTHER_SIGNAL"), msg("c")]
        result = apply_cutoff(history)
        self.assertEqual(contents(result), ["a", "b", "c"])

    def test_messages_without_metadata_survive(self):
        history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        result = apply_cutoff(history)
        self.assertEqual(contents(result), ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
