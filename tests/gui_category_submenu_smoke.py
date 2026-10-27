"""Real GTK/CMenu integration smoke. Run under xvfb-run + dbus-run-session."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="mms-gtk-submenu-") as td:
        os.environ.update({
            "XDG_CONFIG_HOME": td + "/config",
            "XDG_DATA_HOME": td + "/data",
            "XDG_STATE_HOME": td + "/state",
            "XDG_CACHE_HOME": td + "/cache",
            "GSETTINGS_BACKEND": "memory",
            "XDG_CURRENT_DESKTOP": "X-Cinnamon",
        })
        from app import Gtk, MenuApplication, EditorWindow
        application = MenuApplication()
        assert application.register(None)
        window = EditorWindow(application)
        try:
            app = next((a for a in window.backend.apps if a.categories and not a.hidden), None)
            assert app, "Need at least one Cinnamon application"
            target = next((c for c in window.backend.categories
                           if not c.hidden and c.key not in app.categories), None)
            assert target, "Need a second visible category"

            submenu = window.app_categories_submenu(app.id)
            widgets = [x for x in submenu.get_children() if isinstance(x, Gtk.CheckMenuItem)]
            assert len(widgets) == len(window.backend.categories), (len(widgets), len(window.backend.categories))
            labels = [x.get_label() for x in widgets]
            assert target.name in labels
            expected_checked = {c.name for c in window.backend.categories if c.key in app.categories}
            actual_checked = {x.get_label() for x in widgets if x.get_active()}
            assert expected_checked <= actual_checked, (expected_checked, actual_checked)

            check = next(x for x in widgets if x.get_label() == target.name)
            assert not check.get_active()
            assert check.get_sensitive()
            before = set(app.categories)
            check.set_active(True)
            refreshed = window.app_map()[app.id]
            assert before | {target.key} <= set(refreshed.categories)
            assert check.get_active()
            assert window.backend.can_undo

            check.set_active(False)
            refreshed = window.app_map()[app.id]
            assert set(refreshed.categories) == before
            window.backend.undo()
            assert target.key in window.app_map()[app.id].categories
            window.backend.redo()
            assert target.key not in window.app_map()[app.id].categories
            print(f"GTK CATEGORY SUBMENU OK: {len(widgets)} checkboxes; add/remove/undo/redo.")
        finally:
            window.destroy()


if __name__ == "__main__":
    main()
