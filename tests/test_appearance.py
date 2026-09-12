# -*- coding: utf-8 -*-
"""Appearance settings defaults, migration and validation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import ConfigStore  # noqa: E402


class AppearanceConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        return ConfigStore(self.path)

    def test_fresh_install_defaults(self):
        store = self._store()
        self.assertEqual(store.data["appearance"], {"theme": "system", "skin": "violet"})

    def test_partial_and_invalid_legacy_values_are_completed(self):
        store = self._store({"appearance": {"theme": "DARK", "skin": "unknown"}})
        self.assertEqual(store.data["appearance"], {"theme": "dark", "skin": "violet"})

        store = self._store({"appearance": {"skin": "ocean"}})
        self.assertEqual(store.data["appearance"], {"theme": "system", "skin": "ocean"})

    def test_update_merges_and_persists_valid_values(self):
        store = self._store()
        result = store.update_settings({"appearance": {"theme": "dark", "skin": "rose"}})
        self.assertEqual(result["appearance"], {"theme": "dark", "skin": "rose"})
        reloaded = ConfigStore(self.path)
        self.assertEqual(reloaded.data["appearance"], {"theme": "dark", "skin": "rose"})

        # Updating one field preserves the other.
        store.update_settings({"appearance": {"theme": "light"}})
        self.assertEqual(store.data["appearance"], {"theme": "light", "skin": "rose"})

    def test_update_rejects_invalid_shape_and_enum(self):
        store = self._store()
        for payload in (None, "dark", [], 1):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": payload})
        for payload in ({"theme": "blue"}, {"skin": "purple"}, {"theme": "dark", "extra": True}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    store.update_settings({"appearance": payload})


if __name__ == "__main__":
    unittest.main()
