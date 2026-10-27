# Mint Menu Studio

A local GTK 3 menu editor for **Linux Mint Cinnamon**. Organize launchers and categories with drag and drop, create shortcuts, edit launch commands, manage favorites, hide and restore entries, and undo changes across editor restarts.

**No account, backend service, network access, or administrator privileges required.**

[Русская документация](README.md) · [Architecture](docs/ARCHITECTURE.md) · [Security](docs/SECURITY.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

## Features

- Move applications between menu categories; drag one or several selected applications (Ctrl / Shift).
- Rearrange categories in the left pane and entries within a category in the right pane.
- Define a different ordering for each category, plus a separate All Applications order.
- Create custom categories and .desktop launchers; edit name, icon, description, command and terminal setting.
- Hide entries without uninstalling the software; restore them later.
- Manage Cinnamon favorites and their order.
- Search by name, description, and launch command.
- Undo and redo, including after closing the editor; conflict detection protects changes made by other tools.

## Standard installation

Tested on **Linux Mint 22.3 / Cinnamon 6.6.9** with Python 3.12, GTK 3 and CMenu 3. System GI/CMenu packages are usually already installed with Cinnamon. Compatibility with other releases is not guaranteed.

Run as your desktop user, **not as root**:

~~~bash
git clone https://github.com/aaamosh/mint-menu-studio.git
cd mint-menu-studio
/usr/bin/python3 install.py
~~~

The program appears in the applications menu as **Mint Menu Studio** (or **Редактор меню** in Russian). Launch it directly with:

~~~bash
/usr/bin/python3 ~/.local/share/mint-menu-studio/app.py
~~~

**The default installation does not replace or modify the Cinnamon menu applet.** Category membership, launchers, and favorites work as expected without an add-on. The editor can save manual ordering, but Cinnamon's built-in applet still uses its own alphabetical sorting until the optional adapter below is enabled.

## Optional custom-order applet adapter

If you want manual category and application order to appear **in Cinnamon's actual menu**, explicitly opt in:

~~~bash
/usr/bin/python3 install.py --with-order-applet
cinnamon-dbus-command ReloadXlet menu@cinnamon.org APPLET
~~~

This generates a **user-local copy of the standard Cinnamon menu applet** from the version installed on your computer, with a narrowly scoped ordering adapter. It reads **~/.config/mint-menu-studio/order.json** and reloads when that file changes. No system files under **/usr/share/cinnamon** are modified.

The adapter is **experimental and version-sensitive**. Updating Cinnamon can make an old user-local applet incompatible. Re-run the opt-in installer after an update to rebuild the copy from the updated system files. It fails closed if the expected upstream source structure has changed or an unrelated local menu applet is already installed.

Disable the adapter at any time without deleting your saved ordering:

~~~bash
/usr/bin/python3 install.py --remove-order-applet
cinnamon-dbus-command ReloadXlet menu@cinnamon.org APPLET
~~~

Without the adapter, Cinnamon returns to its built-in sorting. A plain editor update does not enable, disable, or replace an existing adapter.

## Basic usage

1. Drag categories up or down in the left panel.
2. In a category, drag applications above or below other entries to reorder them, or drop them onto a category to move them there.
3. Use Ctrl / Shift to select and move multiple launchers.
4. Create a **Category** or **Application** with the toolbar. Double-click an entry to edit it.
5. The **Hide** action hides the menu entry, not the underlying program. Restore it from **Hidden Applications**.
6. Drag entries into **Favorites**, then reorder them by dragging or using arrow buttons.
7. Undo with **Ctrl+Z**, redo with **Ctrl+Shift+Z** or **Ctrl+Y**.

Special All Applications / Favorites / Hidden Applications rows in the editor remain fixed. Search results are not manually sortable. Empty custom categories appear in the editor immediately and in Cinnamon after at least one application is assigned.

## Files and data

- User menu XML: **~/.config/menus**.
- User .desktop / .directory overrides: **~/.local/share/applications** and **~/.local/share/desktop-directories**.
- Manual ordering: **~/.config/mint-menu-studio/order.json**.
- Undo/recovery state: **~/.local/state/mint-menu-studio**.
- Installed editor: **~/.local/share/mint-menu-studio**.
- Optional applet adapter: **~/.local/share/cinnamon/applets/menu@cinnamon.org**.

Source-controlled project files do **not** include private user menu configuration, browser data or credentials.

## Uninstall

~~~bash
/usr/bin/python3 ~/.local/share/mint-menu-studio/install.py --uninstall
~~~

The editor and its own managed optional adapter are removed. Personal menu customizations, manual ordering, and undo history remain on disk.

## Project documentation

[Architecture](docs/ARCHITECTURE.md) · [Testing](docs/TESTING.md) · [Security](docs/SECURITY.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

The editor and adapter generator are licensed under **MIT**. Original Cinnamon sources are **not shipped** in this repository. Locally generated modified Cinnamon applet files retain the relevant upstream Cinnamon license obligations.

Version **1.1.1**.
