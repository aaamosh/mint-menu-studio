"""Ordering is per-category, reversible, persistent and does not modify installed programs."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from menu_backend import Backend, ConflictError, CMenu


@unittest.skipUnless(CMenu is not None, "Cinnamon GI CMenu bindings unavailable")
class OrderBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mms-order-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CURRENT_DESKTOP": "X-Cinnamon",
            "GSETTINGS_BACKEND": "memory"
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.backend = Backend()

    def test_category_drag_and_persistent_undo_redo(self):
        backend = self.backend
        original = [item.key for item in backend.ordered_categories()]
        if len(original) < 3:
            self.skipTest("Fewer than 3 categories available")
        backend.reorder_category(original[0], original[2], False)
        expected = original[1:3] + original[:1] + original[3:]
        self.assertEqual([x.key for x in backend.ordered_categories()], expected)
        self.assertEqual([x.key for x in Backend().ordered_categories()], expected)
        self.assertFalse(backend.menu_path.exists(), "Reordering must not change XDG menu XML")
        backend.undo()
        self.assertEqual([x.key for x in backend.ordered_categories()], original)
        backend.redo()
        self.assertEqual([x.key for x in backend.ordered_categories()], expected)

    def test_app_drag_preserves_commands_and_membership(self):
        b = self.backend
        category = next((x for x in b.categories if len(b.ordered_apps(x.key)) > 3), None)
        if category is None:
            self.skipTest("Need a real nonempty Cinnamon category")
        before = b.ordered_apps(category.key)
        ids = [x.id for x in before]
        cmds = {x.id: x.command for x in before}
        membership = {x.id: x.categories[:] for x in before}
        b.reorder_apps(category.key, ids[:2], ids[3], False)
        expected = ids[2:4] + ids[:2] + ids[4:]
        self.assertEqual([x.id for x in b.ordered_apps(category.key)], expected)
        self.assertEqual({x.id: x.command for x in b.ordered_apps(category.key)}, cmds)
        self.assertEqual({x.id: x.categories for x in b.ordered_apps(category.key)}, membership)
        b.undo()
        self.assertEqual([x.id for x in b.ordered_apps(category.key)], ids)
        b.redo()
        self.assertEqual([x.id for x in b.ordered_apps(category.key)], expected)
        self.assertFalse(b.menu_path.exists())

    def test_undo_guard_rejects_external_changes(self):
        b = self.backend
        keys = [c.key for c in b.ordered_categories()]
        if len(keys) < 2:
            self.skipTest("Need two categories")
        b.reorder_category(keys[0], keys[1], False)
        b.order_path.write_bytes(b.order_path.read_bytes() + b" ")
        with self.assertRaises(ConflictError):
            b.undo()
        self.assertTrue(b.order_path.read_bytes().endswith(b" "))

    def test_noop_drag_does_not_create_history(self):
        b = self.backend
        cats = [c.key for c in b.ordered_categories()]
        if not cats:
            self.skipTest("No categories")
        b.reorder_category(cats[0], cats[0], True)
        self.assertFalse(b.can_undo)


if __name__ == "__main__":
    unittest.main()
