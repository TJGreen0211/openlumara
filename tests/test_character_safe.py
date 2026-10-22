"""
Self-contained tests for Module.character_safe (core/manager.py): while a
character is active only the characters module's prompts are used, unless a
module opts in with character_safe (memory does, so characters still know the
user). Calls the REAL Manager.get_system_prompt / get_end_prompt on a fake
manager instance with a fake `core`, so the app is never bootstrapped.

Run:  venv\\Scripts\\python tests\\test_character_safe.py
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CONFIG = {"model": {"use_tools": True}, "modules": {"disabled_prompts": []}}

_core_mod = types.ModuleType("core")
_core_mod.detail_error = lambda e: str(e)
_core_mod.config = types.SimpleNamespace(get=lambda key, *a, **k: CONFIG.get(key, {}))
_core_mod.modules = types.SimpleNamespace(nonagentic=("characters", "writing_style", "time"))
sys.modules["core"] = _core_mod
sys.modules.setdefault("modules", types.ModuleType("modules"))

_spec = importlib.util.spec_from_file_location(
    "ol_manager_charsafe_under_test", os.path.join(ROOT, "core", "manager.py"))
manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manager_mod)
Manager = manager_mod.Manager


class FakeModule:
    def __init__(self, name, character_safe=False, disable_agent_prompts=True):
        self.name = name
        self.character_safe = character_safe
        self.config = types.SimpleNamespace(get=lambda key, *a, **k: {
            "disable_agent_prompts_when_character_active": disable_agent_prompts,
            "use_writing_style": False,
        }.get(key))

    async def on_system_prompt(self):
        return f"{self.name} system"

    async def on_end_prompt(self):
        return f"{self.name} end"


class FakeChat:
    def __init__(self, character):
        self.current = 0
        self._metadata = {"character": character} if character else {}

    def get(self, key):
        return self._metadata if key == "metadata" else None


def make_manager(character, **char_kwargs):
    mgr = types.SimpleNamespace()
    mgr.pure_mode = False
    mgr.broken_modules = []
    mgr.log = lambda *a: None
    mgr.channel = types.SimpleNamespace(context=types.SimpleNamespace(chat=FakeChat(character)))
    mgr.modules = {
        "characters": FakeModule("characters", **char_kwargs),
        "memory": FakeModule("memory", character_safe=True),
        "scheduler": FakeModule("scheduler"),
    }
    return mgr


def run(coro):
    return asyncio.run(coro)


class CharacterSafeTest(unittest.TestCase):
    def test_without_a_character_everything_is_included(self):
        mgr = make_manager(None)
        system = run(Manager.get_system_prompt(mgr))
        end = run(Manager.get_end_prompt(mgr))
        for name in ("memory", "scheduler"):
            self.assertIn(f"{name} system", system)
            self.assertIn(f"{name} end", end)

    def test_character_keeps_only_characters_and_character_safe_modules(self):
        mgr = make_manager("pirate")
        system = run(Manager.get_system_prompt(mgr))
        end = run(Manager.get_end_prompt(mgr))
        self.assertIn("characters system", system)
        self.assertIn("memory system", system)
        self.assertNotIn("scheduler system", system)
        self.assertIn("characters end", end)
        self.assertIn("memory end", end)
        self.assertNotIn("scheduler end", end)

    def test_character_safe_can_be_a_property(self):
        mgr = make_manager("pirate")

        class Toggle(FakeModule):
            enabled = False

            @property
            def character_safe(self):
                return Toggle.enabled

            @character_safe.setter
            def character_safe(self, value):
                pass

        mgr.modules["memory"] = Toggle("memory")
        self.assertNotIn("memory end", run(Manager.get_end_prompt(mgr)))
        Toggle.enabled = True
        self.assertIn("memory end", run(Manager.get_end_prompt(mgr)))

    def test_modules_without_the_attribute_are_not_character_safe(self):
        mgr = make_manager("pirate")
        legacy = FakeModule("legacy")
        del legacy.character_safe
        mgr.modules["legacy"] = legacy
        self.assertNotIn("legacy end", run(Manager.get_end_prompt(mgr)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
