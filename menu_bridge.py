"""Small, user-local Cinnamon menu adapter for per-category manual sorting.

Never edit /usr/share/cinnamon. Rebuild from the installed Cinnamon applet
at installation time; refuse to overwrite any unrelated user xlet or any
file modified outside this adapter. No background services are installed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

UUID = "menu@cinnamon.org"
SYSTEM = Path("/usr/share/cinnamon/applets") / UUID
FILES = ("applet.js", "appUtils.js", "metadata.json",
         "settings-schema.json", "settings-override.json")
MARKER = ".mint-menu-studio-managed.json"


class BridgeError(RuntimeError):
    pass


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def substitute_once(value, old, new):
    if value.count(old) != 1:
        raise BridgeError("Версия Cinnamon несовместима с адаптером порядка меню.")
    return value.replace(old, new, 1)


def render(upstream):
    applet = upstream["applet.js"].decode("utf-8")
    utils = upstream["appUtils.js"].decode("utf-8")

    utils = substitute_once(utils,
'''const CMenu = imports.gi.CMenu;
''',
'''const CMenu = imports.gi.CMenu;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const ByteArray = imports.byteArray;

// The adapter fails open: missing/broken order settings retain Cinnamon's
// normal alphabetic ordering without affecting launchers.
function manualOrder() {
    try {
        const file = Gio.File.new_for_path(
            GLib.build_filenamev([GLib.get_user_config_dir(), "mint-menu-studio", "order.json"]));
        const [ok, bytes] = file.load_contents(null);
        if (!ok) return null;
        const parsed = JSON.parse(ByteArray.toString(bytes));
        if (parsed?.version !== 1 || !Array.isArray(parsed.categories) ||
            typeof parsed.apps !== "object" || parsed.apps === null) return null;
        return parsed;
    } catch (e) {
        return null;
    }
}

function sortBySavedOrder(items, ids, getId) {
    if (!Array.isArray(ids) || ids.length === 0) return items;
    const position = new Map(ids.map((id, index) => [id, index]));
    return items.sort((a, b) => {
        const pa = position.get(getId(a));
        const pb = position.get(getId(b));
        if (pa === undefined && pb === undefined) return 0;
        if (pa === undefined) return 1;
        if (pb === undefined) return -1;
        return pa - pb;
    });
}

function orderedButtonsForCategory(buttons, category) {
    const setting = manualOrder();
    const list = setting?.apps?.[category === "app" ? "@all" : category];
    if (!Array.isArray(list) || list.length === 0) return null;
    return sortBySavedOrder([...buttons], list, b => b.app.get_id());
}
''')
    utils = substitute_once(utils,
'''    dirs.sort(dirSort);
    let sortedApps = Array.from(apps.entries()).sort(appSort);''',
'''    dirs.sort(dirSort);
    const manual = manualOrder();
    if (manual) sortBySavedOrder(dirs, manual.categories, d => d.get_menu_id());
    let sortedApps = Array.from(apps.entries()).sort(appSort);''')

    applet = substitute_once(applet,
'''        this.orderDirty = false;

        this._session''',
'''        this.orderDirty = false;

        // Watching the DIRECTORY survives order.json's atomic replacement.
        this._menuStudioOrderMonitor = null;
        try {
            const folder = Gio.File.new_for_path(
                GLib.build_filenamev([GLib.get_user_config_dir(), "mint-menu-studio"]));
            this._menuStudioOrderMonitor = folder.monitor_directory(
                Gio.FileMonitorFlags.NONE, null);
            this._menuStudioOrderMonitor.connect("changed", (_monitor, file) => {
                if (file.get_basename() === "order.json")
                    this.queueRefresh(RefreshFlags.APP);
            });
        } catch (e) {
            global.logWarning("Menu Studio: menu-order watch disabled: " + e.message);
        }

        this._session''')
    applet = substitute_once(applet,
'''    on_applet_removed_from_panel () {
        Main.keybindingManager.removeXletHotKey(this, "overlay-key");
    }''',
'''    on_applet_removed_from_panel () {
        Main.keybindingManager.removeXletHotKey(this, "overlay-key");
        if (this._menuStudioOrderMonitor) {
            this._menuStudioOrderMonitor.cancel();
            this._menuStudioOrderMonitor = null;
        }
    }''')
    applet = substitute_once(applet,
'''            if (this.orderDirty) {
                this._resetSortOrder();
                this.orderDirty = false;
            }

            this.applicationsBox.get_children()''',
'''            // Each category can have its own order. Always restore the
            // alphabetical baseline before applying that category's order.
            this._resetSortOrder();
            this.orderDirty = false;

            this.applicationsBox.get_children()''')
    applet = substitute_once(applet,
'''                c.visible = b.type.includes(category) || b.type === 'app' && b.category.includes(category);
            });
        } else {
            this.orderDirty = true;''',
'''                c.visible = b.type.includes(category) || b.type === 'app' && b.category.includes(category);
            });
            if (category === "app" || (category !== "favorite" && category !== "recent")) {
                const visibleApps = this._applicationsButtons.filter(b => b.actor.visible);
                const reordered = AppUtils.orderedButtonsForCategory(visibleApps, category);
                if (reordered)
                    reordered.forEach((button, position) =>
                        this.applicationsBox.set_child_at_index(button.actor, position));
            }
        } else {
            this.orderDirty = true;''')

    return {**upstream,
            "applet.js": applet.encode("utf-8"),
            "appUtils.js": utils.encode("utf-8")}


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mms-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def dest_root():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "cinnamon" / "applets" / UUID


def check_owned(root):
    marker = root / MARKER
    if not root.exists():
        return None
    if root.is_symlink():
        raise BridgeError("Пользовательская папка апплета является символьной ссылкой.")
    if not marker.is_file() or marker.is_symlink():
        raise BridgeError("Уже имеется сторонняя версия апплета меню. Она не будет перезаписана.")
    try:
        info = json.loads(marker.read_bytes())
    except Exception as exc:
        raise BridgeError("Не удалось проверить существующую интеграцию.") from exc
    if info.get("version") != 1 or set(info.get("files", {})) != set(FILES):
        raise BridgeError("Неверная подпись установленной интеграции.")
    if {p.name for p in root.iterdir()} != set(FILES) | {MARKER}:
        raise BridgeError("Апплет содержит посторонние файлы, установка остановлена.")
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or checksum(path.read_bytes()) != info["files"][name]:
            raise BridgeError("Апплет изменён вне редактора: " + name)
    return info


def install_bridge():
    if not SYSTEM.is_dir():
        raise BridgeError("Стандартный апплет Cinnamon не найден.")
    upstream = {name: (SYSTEM / name).read_bytes() for name in FILES}
    assembled = render(upstream)
    root = dest_root()
    check_owned(root)
    # Adapter reads this directory; no settings are changed by installing it.
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "mint-menu-studio"
    if config.is_symlink():
        raise BridgeError("Папка порядка меню является символьной ссылкой.")
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.mkdir(parents=True, exist_ok=True)
    for name, data in assembled.items():
        _atomic(root / name, data)
    info = {"version": 1, "upstream": {key: checksum(val) for key, val in upstream.items()},
            "files": {key: checksum(val) for key, val in assembled.items()}}
    _atomic(root / MARKER, (json.dumps(info, indent=2) + "\n").encode())
    return root


def uninstall_bridge():
    root = dest_root()
    if not root.exists():
        return
    # Never touch an unrelated user-local applet. A marker is needed to claim
    # ownership; once marked, hashes must still match before deleting files.
    if not (root / MARKER).is_file():
        return
    check_owned(root)
    for name in FILES:
        (root / name).unlink()
    (root / MARKER).unlink()
    root.rmdir()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    options = parser.parse_args()
    print(uninstall_bridge() if options.uninstall else install_bridge())
