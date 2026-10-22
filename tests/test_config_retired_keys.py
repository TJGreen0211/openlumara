"""
Self-contained test for retired core settings (core/config.py RETIRED_KEYS):
sync_config only adds missing keys, so settings removed from the schema (the
llama.cpp slot cache keys) used to linger in config.yml and per-user
config.json files and kept showing up in the webui settings. load() and
load_user_config() must now strip them, and leave everything else alone.

Uses the REAL core.config with get_schema/_get_registry_data patched so no
module/channel scanning (app bootstrap) happens, like test_config_save_once.

Run:  venv\\Scripts\\python tests\\test_config_retired_keys.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core  # noqa: E402
import core.config  # noqa: E402

SCHEMA = {
    "core": {"data_folder": "data"},
    "api": {"url": "http://API_URL_HERE/v1", "key": "k"},
    "modules": {"enabled": [], "disabled": [], "settings": {}},
}

RETIRED = {"slot_cache": True, "slot_id": 0, "slot_save_path": ""}


class RetiredKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_retired_")
        self.cfg = os.path.join(self.tmp, "config.yml")
        patcher_schema = mock.patch.object(core.config, "get_schema", return_value=copy.deepcopy(SCHEMA))
        patcher_registry = mock.patch.object(core.config, "_get_registry_data", return_value=[])
        patcher_schema.start()
        patcher_registry.start()
        self.addCleanup(patcher_schema.stop)
        self.addCleanup(patcher_registry.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_global_config_retired_keys_are_removed_and_saved(self):
        data = copy.deepcopy(SCHEMA)
        data["api"].update(RETIRED)
        data["api"]["url"] = "http://example:5002/v1"
        with open(self.cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)

        core.config.load(self.cfg)

        with open(self.cfg, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        for key in RETIRED:
            self.assertNotIn(key, saved["api"])
        # non-retired values survive untouched
        self.assertEqual(saved["api"]["url"], "http://example:5002/v1")
        self.assertEqual(saved["api"]["key"], "k")

    def test_per_user_config_retired_keys_are_hidden_then_persisted(self):
        user_dir = os.path.join(self.tmp, "alice")
        os.makedirs(user_dir)
        user_cfg = os.path.join(user_dir, "config.json")
        with open(user_cfg, "w", encoding="utf-8") as f:
            json.dump({"api": dict(RETIRED, url="http://example:5002/v1"), "model": {"name": "m"}}, f)

        def fake_data_path(subpath=None, user=None):
            base = os.path.join(self.tmp, user) if user else self.tmp
            return os.path.join(base, subpath) if subpath else base

        with mock.patch.object(core, "get_data_path", side_effect=fake_data_path):
            if hasattr(core.config.load_user_config, "_cache"):
                core.config.load_user_config._cache.clear()
            loaded = core.config.load_user_config("alice")
            for key in RETIRED:
                self.assertNotIn(key, loaded["api"])
            self.assertEqual(loaded["api"]["url"], "http://example:5002/v1")
            self.assertEqual(loaded["model"]["name"], "m")

            # the file is only rewritten by a normal save, which then drops them
            core.config.save_user_config("alice", loaded)

        with open(user_cfg, encoding="utf-8") as f:
            on_disk = json.load(f)
        for key in RETIRED:
            self.assertNotIn(key, on_disk["api"])

    def test_strip_ignores_missing_or_non_dict_sections(self):
        core.config.strip_retired_keys({})
        core.config.strip_retired_keys({"api": None})
        core.config.strip_retired_keys(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
