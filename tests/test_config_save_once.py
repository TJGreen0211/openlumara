"""
Self-contained test for core/config.py load() save-on-change behavior (audit 2.6):
load() used to save unconditionally, rewriting config.yml on every startup and
stripping user comments. It must now only save when the sync changed something.

Uses the REAL core.config.load() with get_schema/_get_registry_data patched so
no module/channel scanning (app bootstrap) happens, like the other tests avoid
bootstrapping.

Run:  venv\\Scripts\\python tests\\test_config_save_once.py
"""
import copy
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
    "modules": {"enabled": [], "disabled": [], "settings": {}},
}


class ConfigSaveOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openlumara_cfgsave_")
        self.cfg = os.path.join(self.tmp, "config.yml")
        patcher_schema = mock.patch.object(core.config, "get_schema", return_value=copy.deepcopy(SCHEMA))
        patcher_registry = mock.patch.object(core.config, "_get_registry_data", return_value=[])
        patcher_schema.start()
        patcher_registry.start()
        self.addCleanup(patcher_schema.stop)
        self.addCleanup(patcher_registry.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data, comment="# my precious comment\n"):
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(comment)
            yaml.safe_dump(data, f)

    def test_new_config_is_created(self):
        core.config.load(self.cfg)
        self.assertTrue(os.path.exists(self.cfg))

    def test_unchanged_config_is_not_rewritten(self):
        self._write(copy.deepcopy(SCHEMA))
        before = open(self.cfg, "rb").read()

        core.config.load(self.cfg)

        self.assertEqual(open(self.cfg, "rb").read(), before)
        self.assertIn(b"# my precious comment", open(self.cfg, "rb").read())

    def test_changed_config_is_saved(self):
        data = copy.deepcopy(SCHEMA)
        data["core"]["data_folder"] = "other"
        self._write(data)

        core.config.load(self.cfg)

        saved = yaml.safe_load(open(self.cfg, encoding="utf-8"))
        self.assertEqual(saved["core"]["data_folder"], "other")
        self.assertEqual(core.config.get("core", "data_folder"), "other")

    def test_missing_schema_keys_are_added_and_saved(self):
        self._write({"core": {"data_folder": "data"}})

        core.config.load(self.cfg)

        saved = yaml.safe_load(open(self.cfg, encoding="utf-8"))
        self.assertIn("modules", saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
