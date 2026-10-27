"""Cinnamon category/tag checkbox semantics, real menu round trips and undo."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import subprocess
from functools import wraps
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from menu_backend import Backend, BackendError, ConflictError, CMenu


def isolated_cmenu_test(test):
    """GLib caches XDG directories process-wide: execute each case fresh."""
    @wraps(test)
    def run(self):
        if os.environ.get("MMS_CMENU_TEST_CHILD") == "1":
            return test(self)
        env = os.environ.copy()
        env["MMS_CMENU_TEST_CHILD"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             f"test_app_categories.AppCategoryCheckboxTests.{test.__name__}"],
            cwd=str(ROOT / "tests"), env=env, capture_output=True,
            text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0,
                         "Fresh CMenu worker failed:\n" + result.stdout + result.stderr)
    return run


@unittest.skipUnless(CMenu is not None, "CMenu for Cinnamon is unavailable")
class AppCategoryCheckboxTests(unittest.TestCase):
    def setUp(self):
        if os.environ.get("MMS_CMENU_TEST_CHILD") != "1":
            return
        self.temp = tempfile.TemporaryDirectory(prefix="mms-checkboxes-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "GSETTINGS_BACKEND": "memory",
            "XDG_CURRENT_DESKTOP": "X-Cinnamon",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend = Backend()
        self.app = next((a for a in self.backend.apps if a.categories and not a.hidden), None)
        if self.app is None:
            self.skipTest("No suitable installed app in Cinnamon test environment")
        self.target = next(
            (c for c in self.backend.categories
             if c.key not in self.app.categories and not c.hidden),
            None)
        if self.target is None:
            self.skipTest("No second visible category available")

    def app_categories(self):
        b = self.backend
        return set(next(a for a in b.apps if a.id == self.app.id).categories)

    @isolated_cmenu_test
    def test_check_adds_without_removing_other_tags(self):
        b = self.backend
        existing = self.app_categories()
        original_menu = b.menu_path.read_bytes() if b.menu_path.exists() else None
        file_before = Path(self.app.path).read_bytes()
        b.set_app_category(self.app.id, self.target.key, True)
        assert self.target.key in self.app_categories()
        self.assertTrue(existing <= self.app_categories())
        self.assertEqual(Path(self.app.path).read_bytes(), file_before)
        self.assertTrue(b.can_undo)
        b.undo()
        self.assertEqual(self.app_categories(), existing)
        self.assertEqual(b.menu_path.read_bytes() if b.menu_path.exists() else None, original_menu)
        b.redo()
        self.assertTrue(existing <= self.app_categories())
        self.assertIn(self.target.key, self.app_categories())

    @isolated_cmenu_test
    def test_uncheck_removes_only_target_membership(self):
        b = self.backend
        previous = self.app_categories()
        b.set_app_category(self.app.id, self.target.key, True)
        b.set_app_category(self.app.id, self.target.key, False)
        self.assertEqual(self.app_categories(), previous)
        b.undo()
        self.assertIn(self.target.key, self.app_categories())
        b.redo()
        self.assertEqual(self.app_categories(), previous)

    @isolated_cmenu_test
    def test_check_and_uncheck_same_state_are_noops(self):
        b = self.backend
        originally = self.app_categories()
        b.set_app_category(self.app.id, next(iter(originally)), True)
        b.set_app_category(self.app.id, self.target.key, False)
        self.assertFalse(b.can_undo)
        self.assertFalse(b.menu_path.exists())

    @isolated_cmenu_test
    def test_two_categories_independent_and_undoable(self):
        b = self.backend
        existing = self.app_categories()
        targets = [c for c in b.categories if not c.hidden and c.key not in existing]
        if len(targets) < 2:
            self.skipTest("Fewer than two other visible categories")
        b.set_app_category(self.app.id, targets[0].key, True)
        b.set_app_category(self.app.id, targets[1].key, True)
        self.assertTrue(existing | {targets[0].key, targets[1].key} <= self.app_categories())
        b.undo()
        self.assertNotIn(targets[1].key, self.app_categories())
        self.assertIn(targets[0].key, self.app_categories())
        b.undo()
        self.assertEqual(self.app_categories(), existing)

    @isolated_cmenu_test
    def test_invalid_unknown_and_hidden_targets_rejected(self):
        b = self.backend
        initial = b._observed[b.menu_path]
        with self.assertRaises(BackendError):
            b.set_app_category(self.app.id, "../missing", True)
        with self.assertRaises(BackendError):
            b.set_app_category(self.app.id, self.target.key, "true")
        hidden = next((c for c in b.categories if c.hidden and c.key not in self.app.categories), None)
        if hidden is not None:
            with self.assertRaises(BackendError):
                b.set_app_category(self.app.id, hidden.key, True)
        self.assertEqual(b._observed[b.menu_path], initial)
        self.assertFalse(b.can_undo)

    @isolated_cmenu_test
    def test_external_file_conflict_blocks_toggle(self):
        b = self.backend
        b.set_app_category(self.app.id, self.target.key, True)
        after = b.menu_path.read_bytes()
        b.menu_path.write_bytes(after + b" ")
        with self.assertRaises(ConflictError):
            b.set_app_category(self.app.id, self.target.key, False)
        self.assertEqual(b.menu_path.read_bytes(), after + b" ")
        with self.assertRaises(ConflictError):
            b.undo()

    @isolated_cmenu_test
    def test_last_check_can_be_removed_and_undone(self):
        b = self.backend
        app = next((a for a in b.apps if len(a.categories) == 1 and not a.hidden), None)
        if app is None:
            self.skipTest("No application with exactly one category")
        file_before = Path(app.path).read_bytes()
        original_category = app.categories[0]
        b.set_app_category(app.id, original_category, False)
        self.assertEqual(Path(app.path).read_bytes(), file_before)
        b.undo()
        restored = next(a for a in b.apps if a.id == app.id)
        self.assertIn(original_category, restored.categories)

    @isolated_cmenu_test
    def test_reopen_preserves_multiple_categories(self):
        b = self.backend
        existing = self.app_categories()
        b.set_app_category(self.app.id, self.target.key, True)
        loaded = Backend()
        app = next(a for a in loaded.apps if a.id == self.app.id)
        self.assertTrue(existing | {self.target.key} <= set(app.categories))


if __name__ == "__main__":
    unittest.main()
