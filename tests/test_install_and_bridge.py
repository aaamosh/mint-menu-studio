"""User-local installation, opt-in adapter, and conflict protection."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from menu_bridge import BridgeError, FILES, MARKER, SYSTEM, check_owned, render


@unittest.skipUnless((SYSTEM / "applet.js").exists(), "Cinnamon system menu applet unavailable")
class BridgeUnitTests(unittest.TestCase):
    def test_render_is_deterministic_without_system_modification(self):
        original = {name: (SYSTEM / name).read_bytes() for name in FILES}
        copy = render(original)
        self.assertEqual(copy, render(original))
        self.assertEqual(original["metadata.json"], copy["metadata.json"])
        self.assertNotEqual(original["applet.js"], copy["applet.js"])
        self.assertIn(b"orderedButtonsForCategory", copy["appUtils.js"])
        for filename in FILES:
            self.assertEqual(original[filename], (SYSTEM / filename).read_bytes())

    @unittest.skipIf(os.geteuid() == 0, "Install script refuses root")
    def test_opt_in_out_is_reversible_and_data_survives(self):
        with tempfile.TemporaryDirectory(prefix="mms-install-test-") as folder:
            root = Path(folder)
            env = os.environ.copy()
            env.update({
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "GSETTINGS_BACKEND": "memory",
                "XDG_CURRENT_DESKTOP": "X-Cinnamon",
                "PYTHONDONTWRITEBYTECODE": "1"
            })
            def installer(*args):
                return subprocess.run(
                    [sys.executable, str(ROOT / "install.py"), *args],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=20
                )
            local_applet = root / "data/cinnamon/applets/menu@cinnamon.org"
            desktop = root / "data/applications/ru.amosh.MenuStudio.desktop"
            result = installer()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(desktop.exists())
            self.assertFalse(local_applet.exists(), "Default install must not touch the applet")
            result = installer("--with-order-applet")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(check_owned(local_applet))
            self.assertTrue((local_applet / MARKER).exists())
            saved = root / "config/mint-menu-studio/order.json"
            saved.write_text('{"version":1,"categories":[],"apps":{}}\n')
            result = installer("--remove-order-applet")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(local_applet.exists())
            self.assertTrue(saved.exists(), "Removing adapter must not remove order")
            self.assertTrue(desktop.exists(), "Removing adapter must not uninstall editor")
            result = installer("--uninstall")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(desktop.exists())
            self.assertFalse((root / "data/mint-menu-studio").exists())
            self.assertTrue(saved.exists())

    @unittest.skipIf(os.geteuid() == 0, "Install script refuses root")
    def test_foreign_applet_protected(self):
        with tempfile.TemporaryDirectory(prefix="mms-foreign-test-") as folder:
            root = Path(folder)
            local_applet = root / "data/cinnamon/applets/menu@cinnamon.org"
            local_applet.mkdir(parents=True)
            foreign = local_applet / "user-owned.txt"
            foreign.write_text("do not touch me")
            env = os.environ.copy()
            env.update({"XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_DATA_HOME": str(root / "data"),
                        "XDG_STATE_HOME": str(root / "state"),
                        "GSETTINGS_BACKEND": "memory",
                        "PYTHONDONTWRITEBYTECODE": "1"})
            def invoke(flag):
                return subprocess.run([sys.executable, str(ROOT / "install.py"), flag],
                                      capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(invoke("--with-order-applet").returncode, 1)
            self.assertEqual(foreign.read_text(), "do not touch me")
            self.assertEqual(invoke("--remove-order-applet").returncode, 0)
            self.assertEqual(foreign.read_text(), "do not touch me")


if __name__ == "__main__":
    unittest.main()
