#!/usr/bin/python3
"""Install/update this application in the current user's XDG directories."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from menu_bridge import install_bridge, uninstall_bridge, BridgeError

APP = "mint-menu-studio"
DESKTOP_ID = "ru.amosh.MenuStudio.desktop"


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def desktop_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def main():
    parser = argparse.ArgumentParser(description="Install Mint Menu Studio for the current user")
    parser.add_argument("--uninstall", action="store_true", help="Remove editor and its managed menu adapter")
    addon = parser.add_mutually_exclusive_group()
    addon.add_argument("--with-order-applet", action="store_true",
                       help="Opt in to a user-local Cinnamon applet adapter for custom ordering")
    addon.add_argument("--remove-order-applet", action="store_true",
                       help="Remove the optional adapter without deleting saved order preferences")
    options = parser.parse_args()
    if options.uninstall and (options.with_order_applet or options.remove_order_applet):
        parser.error("--uninstall cannot be combined with adapter options")
    if os.geteuid() == 0:
        raise SystemExit("Run this installer as the desktop user, without sudo.")
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    installed = data / APP
    desktop = data / "applications" / DESKTOP_ID
    if options.uninstall:
        try:
            uninstall_bridge()
        except BridgeError as exc:
            raise SystemExit(str(exc)) from exc
        if desktop.exists():
            contents = desktop.read_text()
            if "X-Mint-Menu-Studio=true" not in contents:
                raise SystemExit("The launcher was changed outside this installer; leaving it untouched.")
            desktop.unlink()
        # Menu customizations and undo history intentionally remain user data.
        for filename in ["app.py", "menu_backend.py", "menu_bridge.py", "install.py", "icon.svg", "README.md", "LICENSE"]:
            target = installed / filename
            if target.is_file() and not target.is_symlink():
                target.unlink()
        cache = installed / "__pycache__"
        if cache.is_dir() and not cache.is_symlink():
            for target in cache.glob("*.pyc"):
                target.unlink()
            try:
                cache.rmdir()
            except OSError:
                pass
        try:
            installed.rmdir()
        except OSError:
            pass
        print("Editor removed. Your menu customizations and history were preserved.")
        return
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("CMenu", "3.0")
    from gi.repository import CMenu, Gtk  # noqa: F401
    source = Path(__file__).resolve().parent
    for filename in ["app.py", "menu_backend.py", "menu_bridge.py", "install.py", "icon.svg", "README.md", "LICENSE"]:
        original = source / filename
        if not original.is_file():
            raise SystemExit("Missing package file: " + filename)
    # The applet adapter is explicitly opt-in: the editor also works without
    # it, but Cinnamon itself then retains its built-in alphabetical sorting.
    # A plain upgrade preserves an adapter that the user enabled previously.
    try:
        if options.with_order_applet:
            install_bridge()
        elif options.remove_order_applet:
            uninstall_bridge()
    except BridgeError as exc:
        raise SystemExit("Установка остановлена без перезаписи чужого апплета: " + str(exc)) from exc
    installed.mkdir(parents=True, exist_ok=True)
    for filename in ["app.py", "menu_backend.py", "menu_bridge.py", "install.py", "icon.svg", "README.md", "LICENSE"]:
        atomic_write(installed / filename, (source / filename).read_bytes())
    launcher = "\n".join([
        "[Desktop Entry]", "Type=Application", "Version=1.0",
        "Name=Mint Menu Studio", "Name[en]=Mint Menu Studio", "Name[ru]=Редактор меню",
        "Comment=Разделы, приложения, избранное и отмена изменений",
        "Comment[en]=Organize your Cinnamon menu with drag and drop and undo",
        "Exec=/usr/bin/python3 " + desktop_quote(installed / "app.py"),
        "Icon=" + str(installed / "icon.svg"),
        "Terminal=false", "Categories=Settings;DesktopSettings;",
        "Keywords=menu;editor;launcher;меню;редактор;ярлык;раздел;избранное;",
        "StartupNotify=true", "StartupWMClass=MintMenuStudio",
        "OnlyShowIn=X-Cinnamon;", "X-Mint-Menu-Studio=true", "",
    ])
    atomic_write(desktop, launcher.encode())
    validator = shutil.which("desktop-file-validate")
    if validator:
        subprocess.run([validator, str(desktop)], check=True)
    update = shutil.which("update-desktop-database")
    if update:
        subprocess.run([update, str(desktop.parent)], check=True, capture_output=True)
    print("Installed: " + str(installed))
    print("Launcher: " + str(desktop))
    if options.with_order_applet:
        print("Optional Cinnamon applet adapter installed; reload the menu applet to activate.")
    elif options.remove_order_applet:
        print("Optional Cinnamon applet adapter removed; reload the menu applet.")
    else:
        print("Editor installed; optional Cinnamon applet adapter was not changed.")


if __name__ == "__main__":
    main()
