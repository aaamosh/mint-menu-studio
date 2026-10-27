"""User-local Cinnamon menu editing, with guarded, persistent undo/redo.

The menu library is the source of truth for membership.  Application identities
and launch commands are never changed by a move.  No launcher is ever executed
by this module.  All writes are limited to the user's XDG directories.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional
import uuid
from xml.dom import Node, minidom

try:
    import gi
    gi.require_version("CMenu", "3.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import CMenu, Gio, GLib
except (ImportError, ValueError):
    CMenu = Gio = GLib = None


class BackendError(RuntimeError):
    """An operation could not safely be completed."""


class ConflictError(BackendError):
    """A file or setting changed outside this editor."""


@dataclass
class Category:
    key: str
    name: str
    icon: str
    hidden: bool = False


@dataclass
class App:
    id: str
    name: str
    comment: str
    command: str
    icon: str
    categories: list[str]
    hidden: bool
    path: str
    terminal: bool = False


_GROUP = "Desktop Entry"
_MAX_FILE = 4 * 1024 * 1024
_MAX_HISTORY = 24 * 1024 * 1024
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9-]+(?:\[[^\]\r\n]+\])?)(\s*=\s*)(.*)$")


def _languages() -> list[str]:
    if GLib is not None:
        values = GLib.get_language_names()
    else:
        values = [os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
                  or os.environ.get("LANG", "C")]
    result = []
    for value in values:
        value = value.split(".", 1)[0]
        if value and value not in ("C", "POSIX") and value not in result:
            result.append(value)
            if "_" in value and value.split("_", 1)[0] not in result:
                result.append(value.split("_", 1)[0])
    return result


def _unescape(value: str) -> str:
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    return re.sub(r"\\([sntr\\])", lambda match: escapes[match.group(1)], value)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


class DesktopDocument:
    """Edit only requested key values; retain all other bytes and translations."""
    def __init__(self, raw: bytes):
        try:
            self.text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackendError("Ярлык не является корректным UTF-8 файлом.") from exc
        if "\x00" in self.text:
            raise BackendError("В ярлыке обнаружен недопустимый нулевой символ.")
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        self.lines = self.text.splitlines(keepends=True)
        self._index()

    def _index(self):
        self.keys = {}
        self.values = {}
        self.groups = {}
        group = None
        for index, line in enumerate(self.lines):
            body = line.rstrip("\r\n")
            if body.startswith("[") and body.endswith("]"):
                group = body[1:-1]
                self.groups[group] = index
            elif group is not None:
                match = _KEY_RE.match(body)
                if match:
                    key = (group, match.group(2))
                    self.keys[key] = index
                    self.values[key] = match.group(4)

    def get(self, key: str, default: str = "", *, group: str = _GROUP) -> str:
        return _unescape(self.values.get((group, key), default))

    def localized(self, key: str, default: str = "") -> str:
        for language in _languages():
            if (_GROUP, f"{key}[{language}]") in self.values:
                return self.get(f"{key}[{language}]")
        return self.get(key, default)

    def boolean(self, key: str) -> bool:
        return self.get(key).strip().lower() == "true"

    def set(self, key: str, value: str | bool, *, localized: bool = False,
            group: str = _GROUP):
        raw_value = ("true" if value else "false") if isinstance(value, bool) else _escape(value)
        full_key = (group, key)
        if full_key in self.keys:
            index = self.keys[full_key]
            old = self.lines[index]
            match = _KEY_RE.match(old.rstrip("\r\n"))
            ending = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
            self.lines[index] = match.group(1) + key + match.group(3) + raw_value + ending
        else:
            if group not in self.groups:
                if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                    self.lines[-1] += self.newline
                self.lines.append(f"[{group}]" + self.newline)
                self._index()
            index = self.groups[group] + 1
            while index < len(self.lines) and not self.lines[index].startswith("["):
                index += 1
            if index and not self.lines[index - 1].endswith(("\n", "\r")):
                self.lines[index - 1] += self.newline
            self.lines.insert(index, key + "=" + raw_value + self.newline)
        self._index()
        if localized and not isinstance(value, bool) and _languages():
            self.set(f"{key}[{_languages()[0]}]", value, group=group)

    def to_bytes(self) -> bytes:
        return "".join(self.lines).encode("utf-8")


def _read(path: Path) -> Optional[bytes]:
    try:
        if path.stat().st_size > _MAX_FILE:
            raise BackendError(f"Файл слишком велик для безопасного редактирования: {path}")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackendError(f"Не удалось прочитать {path}: {exc}") from exc


def _digest(value: Optional[bytes]) -> str:
    return "absent" if value is None else hashlib.sha256(value).hexdigest()


def _pack(value: Optional[bytes]):
    return None if value is None else base64.b64encode(value).decode("ascii")


def _unpack(value):
    if value is None:
        return None
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise BackendError("Повреждён журнал отмены. Никакие файлы меню не изменены.") from exc
    if len(result) > _MAX_FILE:
        raise BackendError("Недопустимый размер файла в журнале отмены.")
    return result


def _atomic(path: Path, contents: Optional[bytes], mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BackendError(f"Редактирование символической ссылки запрещено: {path}")
    if contents is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        fd, temporary = tempfile.mkstemp(prefix=".mint-menu-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _xml(raw: bytes):
    if len(raw) > _MAX_FILE or re.search(br"<!\s*ENTITY\b|<!DOCTYPE[^>]*\[", raw, re.I):
        raise BackendError("Небезопасный или слишком большой XML меню; исходный файл сохранён.")
    try:
        document = minidom.parseString(raw)
    except Exception as exc:
        raise BackendError(f"XML меню повреждён. Исправьте файл или восстановите копию: {exc}") from exc
    root = document.documentElement
    names = _children(root, "Name")
    if root.tagName != "Menu" or len(names) != 1 or not _node_text(names[0]):
        raise BackendError("В XML меню отсутствует корректное корневое имя Menu/Name.")
    return document


def _children(node, tag):
    return [child for child in node.childNodes if child.nodeType == Node.ELEMENT_NODE and child.tagName == tag]


def _node_text(node):
    return "".join(child.data for child in node.childNodes if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE))


def _text_node(document, parent, tag, text):
    node = document.createElement(tag)
    node.appendChild(document.createTextNode(text))
    parent.appendChild(node)
    return node


def _ensure_text(document, parent, tag, text):
    for child in _children(parent, tag):
        if _node_text(child) == text:
            return child
    return _text_node(document, parent, tag, text)


def _directory_source(document, key, native_source, managed_target):
    """Prefer our selected override when CMenu still reports an older path."""
    selected = None
    if document is not None:
        # Duplicate Menu fragments are merged in document order by CMenu. A
        # stale override merely existing on disk is insufficient: the current
        # user XML must actually select its filename as the last Directory.
        for menu in _children(document.documentElement, "Menu"):
            names = _children(menu, "Name")
            if names and _node_text(names[0]) == key:
                for reference in _children(menu, "Directory"):
                    selected = _node_text(reference)
    if selected == managed_target.name:
        managed_raw = _read(managed_target)
        if managed_raw is not None:
            return managed_target, managed_raw
    return native_source, _read(native_source) if native_source else None


def _menu(document, path):
    parent = document.documentElement
    for name in path:
        match = None
        for candidate in _children(parent, "Menu"):
            names = _children(candidate, "Name")
            if names and _node_text(names[0]) == name:
                match = candidate
                break
        if match is None:
            match = document.createElement("Menu")
            parent.appendChild(match)
            _text_node(document, match, "Name", name)
        parent = match
    return parent


def _filename_rule(document, parent, app_id, include):
    # Remove only simple direct Filename conditions, preserving arbitrary And/
    # Or/Not rules and every unrelated entry.  The new rule is appended last.
    for tag in ("Include", "Exclude"):
        for rule in list(_children(parent, tag)):
            for filename in list(_children(rule, "Filename")):
                if _node_text(filename) == app_id:
                    rule.removeChild(filename)
            if not any(child.nodeType == Node.ELEMENT_NODE for child in rule.childNodes):
                parent.removeChild(rule)
    rule = document.createElement("Include" if include else "Exclude")
    _text_node(document, rule, "Filename", app_id)
    parent.appendChild(rule)


def _label(value, noun):
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise BackendError(f"Введите корректное {noun}.")
    if len(value) > 512:
        raise BackendError(f"Слишком длинное {noun}.")
    return value


def _command(value):
    value = value.strip()
    if not value or any(char in value for char in "\x00\r\n"):
        raise BackendError("Введите команду запуска в одной строке.")
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise BackendError(f"Ошибка в кавычках команды: {exc}") from exc
    if not parts:
        raise BackendError("Команда запуска пуста.")
    return value


class Backend:
    def __init__(self, menu_name: str = "cinnamon-applications.menu", history_limit: int = 40):
        if CMenu is None:
            raise BackendError("Нужны python3-gi и gir1.2-cmenu-3.0 (компоненты Cinnamon).")
        if Path(menu_name).name != menu_name or not menu_name.endswith(".menu"):
            raise BackendError("Недопустимое имя файла меню.")
        self.menu_name = menu_name
        self.history_limit = max(1, min(100, history_limit))
        self.config_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser().resolve()
        self.data_dir = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser().resolve()
        self.state_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser().resolve() / "mint-menu-studio"
        self.menu_path = self.config_dir / "menus" / menu_name
        self.order_path = self.config_dir / "mint-menu-studio" / "order.json"
        self.order_categories: list[str] = []
        self.order_apps: dict[str, list[str]] = {}
        self.app_dir = self.data_dir / "applications"
        self.directory_dir = self.data_dir / "desktop-directories"
        self.history_path = self.state_dir / "history.json"
        self.pending_path = self.state_dir / "pending.json"
        self.categories: list[Category] = []
        self.apps: list[App] = []
        self.favorites: list[str] = []
        self.favorites_available = False
        self.warnings: list[str] = []
        self._settings = None
        self._history = {"version": 1, "cursor": 0, "operations": []}
        self._history_bytes = None
        try:
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup("org.cinnamon", True) if source else None
            if schema and schema.has_key("favorite-apps"):
                self._settings = Gio.Settings.new_full(schema, None, None)
                self.favorites_available = self._settings.is_writable("favorite-apps")
        except Exception:
            self._settings = None
        with self._lock():
            self._recover()
        self.refresh()

    @contextlib.contextmanager
    def _lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state_dir / "lock"
        if path.is_symlink():
            raise BackendError("Файл блокировки журнала является символической ссылкой.")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _valid_target(self, path: Path):
        path = Path(path)
        valid = path in (self.menu_path, self.order_path)
        for directory, extension in ((self.app_dir, ".desktop"), (self.directory_dir, ".directory")):
            if path.is_relative_to(directory) and path.suffix == extension:
                valid = True
        if not valid or not path.is_absolute() or ".." in path.parts:
            raise BackendError(f"Запись за пределами пользовательского меню запрещена: {path}")
        if path.is_symlink() or path.resolve() != path:
            raise BackendError(f"Нельзя безопасно записать файл через символическую ссылку: {path}")
        if path.exists() and not path.is_file():
            raise BackendError(f"Путь не является обычным файлом: {path}")

    def _load_history(self):
        try:
            raw = self.history_path.read_bytes() if self.history_path.exists() else None
            if raw and len(raw) > _MAX_HISTORY:
                raise ValueError("слишком большой журнал")
            history = json.loads(raw) if raw else {"version": 1, "cursor": 0, "operations": []}
            if history.get("version") != 1 or not isinstance(history.get("operations"), list):
                raise ValueError("неизвестный формат")
            if not 0 <= history.get("cursor", -1) <= len(history["operations"]):
                raise ValueError("некорректная позиция")
            for operation in history["operations"]:
                self._validate_record(operation)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise BackendError(f"Не удалось прочитать журнал отмены: {exc}. Файлы меню сохранены.") from exc
        self._history_bytes = raw
        self._history = history

    def _validate_record(self, record):
        if not isinstance(record.get("files"), dict) or not isinstance(record.get("label"), str):
            raise BackendError("Повреждена операция в журнале отмены.")
        for filename, change in record["files"].items():
            self._valid_target(Path(filename))
            for side in ("before", "after"):
                _unpack(change[side])
        for side in ("favorite_before", "favorite_after"):
            value = record.get(side)
            if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
                raise BackendError("Повреждены избранные приложения в журнале отмены.")

    def refresh(self):
        """Re-read external changes; never silently replace invalid user XML."""
        with self._lock():
            self._recover()
            self._load_history()
        self.warnings = []
        raw_menu = _read(self.menu_path)
        user_document = _xml(raw_menu) if raw_menu is not None else None
        flags = (CMenu.TreeFlags.SHOW_EMPTY | CMenu.TreeFlags.INCLUDE_NODISPLAY |
                 CMenu.TreeFlags.INCLUDE_EXCLUDED | CMenu.TreeFlags.SORT_DISPLAY_NAME)
        tree = CMenu.Tree.new(self.menu_name, flags)
        try:
            if not tree.load_sync():
                raise BackendError("Cinnamon не смог прочитать дерево меню.")
        except Exception as exc:
            raise BackendError(f"Не удалось прочитать меню Cinnamon: {exc}") from exc
        root = tree.get_root_directory()
        if root is None:
            raise BackendError("Cinnamon вернул пустое дерево меню.")
        self._tree = tree
        self._root_name = root.get_menu_id()
        self._canonical_path = tree.get_canonical_menu_path()
        self._observed = {self.menu_path: raw_menu}
        order_raw = _read(self.order_path)
        self._observed[self.order_path] = order_raw
        order = self._parse_order(order_raw)
        self.order_categories = order["categories"]
        self.order_apps = order["apps"]
        self._sources = {}
        self._documents = {}
        self._alternatives = {}
        self._category_sources = {}
        self._memberships = {}
        self._excluded = {}
        self._subpaths = {}
        self._tree_ids = set()
        self._scan_desktops()
        categories = []
        for kind, directory in self._items(root):
            if kind != "directory":
                continue
            key = directory.get_menu_id()
            if any(category.key == key for category in categories):
                continue
            source_path = directory.get_desktop_file_path()
            source = Path(source_path) if source_path else None
            # A shared system .directory may describe multiple categories. Give
            # this category its own override so hiding/renaming it stays local.
            file_id = (key + ".directory") if re.fullmatch(r"mint-menu-studio-[a-f0-9]{32}", key) else f"mint-menu-studio-{hashlib.sha256(key.encode()).hexdigest()[:16]}.directory"
            target = self.directory_dir / file_id
            source, source_raw = _directory_source(user_document, key, source, target)
            categories.append(self._category_metadata(key, directory, source_raw))
            self._category_sources[key] = (source, source_raw, target)
            self._observed[target] = _read(target)
            self._walk_directory(directory, (key,), key)
        categories.sort(key=lambda item: (item.key.lower() in ("development", "other", "preferences", "administration"), locale.strxfrm(item.name.lower())))
        apps = []
        for app_id, path in self._sources.items():
            if app_id not in self._tree_ids and not path.is_relative_to(self.app_dir):
                continue
            document = self._documents.get(app_id)
            if document is None:
                continue
            if document.get("Type", "Application") != "Application":
                continue
            # Non-application helper desktop files are intentionally not offered.
            if not document.get("Name") and not document.boolean("Hidden"):
                continue
            fallback = self._fallback_document(app_id)
            name = document.localized("Name") or (fallback.localized("Name") if fallback else app_id)
            command = document.get("Exec") or (fallback.get("Exec") if fallback else "")
            memberships = list(self._memberships.get(app_id, {}))
            apps.append(App(app_id, name, document.localized("Comment"), command,
                            document.localized("Icon") or "application-x-executable", memberships,
                            document.boolean("NoDisplay") or document.boolean("Hidden"), str(path), document.boolean("Terminal")))
        apps.sort(key=lambda item: (locale.strxfrm(item.name.lower()), item.id))
        self.categories = categories
        self.apps = apps
        self._apps_by_id = {app.id: app for app in apps}
        self._categories_by_key = {category.key: category for category in categories}
        self.favorites = list(self._settings.get_strv("favorite-apps")) if self._settings else []

    @staticmethod
    def _parse_order(raw):
        if raw is None:
            return {"version": 1, "categories": [], "apps": {}}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("неизвестная версия")
            categories = data.get("categories", [])
            apps = data.get("apps", {})
            if (not isinstance(categories, list) or not isinstance(apps, dict)
                    or len(categories) > 500 or len(apps) > 500):
                raise ValueError("неверная структура")
            def valid_list(items):
                return (isinstance(items, list) and len(items) <= 10000
                        and all(isinstance(x, str) and 0 < len(x) < 512 for x in items)
                        and len(set(items)) == len(items))
            if not valid_list(categories) or any(
                not isinstance(key, str) or len(key) > 512 or not valid_list(value)
                for key, value in apps.items()
            ):
                raise ValueError("недопустимый список")
            return {"version": 1, "categories": categories, "apps": apps}
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise BackendError("Файл порядка меню повреждён. Ничего не изменено: " + str(exc)) from exc

    def _order_bytes(self, *, categories=None, apps=None):
        doc = {"version": 1,
               "categories": list(self.order_categories if categories is None else categories),
               "apps": self.order_apps if apps is None else apps}
        raw = (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._parse_order(raw)
        return raw

    @staticmethod
    def _apply_order(entries, preference, key):
        positions = {value: index for index, value in enumerate(preference)}
        # Unlisted items retain their natural initial order at the end.
        return sorted(entries, key=lambda entry: (0, positions[key(entry)]) if key(entry) in positions else (1, 0))

    def ordered_categories(self):
        return self._apply_order(self.categories, self.order_categories, lambda category: category.key)

    def ordered_apps(self, section):
        if section == "@favorites":
            return [self._apps_by_id[x] for x in self.favorites if x in self._apps_by_id]
        if section == "@all":
            items = [app for app in self.apps if not app.hidden]
        else:
            self._require_category(section)
            items = [app for app in self.apps if not app.hidden and section in app.categories]
        return self._apply_order(items, self.order_apps.get(section, []), lambda app: app.id)

    @staticmethod
    def _reposition(existing, selected, anchor, before):
        if anchor in selected:
            return existing
        moved = [item for item in existing if item in selected]
        remaining = [item for item in existing if item not in selected]
        index = remaining.index(anchor)
        remaining[index if before else index + 1:index if before else index + 1] = moved
        return remaining

    def reorder_category(self, key, target_key, before):
        self._require_category(key)
        self._require_category(target_key)
        current = [category.key for category in self.ordered_categories()]
        result = self._reposition(current, [key], target_key, before)
        if result == current:
            return
        # Preserve obsolete IDs from past configurations: a new installation
        # of the same menu item will recover its previous position.
        stored = result + [item for item in self.order_categories if item not in result]
        self._perform("Порядок разделов", {self.order_path: self._order_bytes(categories=stored)})

    def reorder_apps(self, section, selected_ids, target_id, before):
        if section == "@favorites":
            if not self.favorites_available:
                raise BackendError("Настройка избранного недоступна.")
            current = list(self.favorites)
            if target_id not in current or any(x not in current for x in selected_ids):
                raise BackendError("Выбранное избранное изменилось.")
            result = self._reposition(current, selected_ids, target_id, before)
            if result != current:
                self.set_favorites(result)
            return
        if section != "@all":
            self._require_category(section)
        current = [app.id for app in self.ordered_apps(section)]
        if target_id not in current or not selected_ids or any(x not in current for x in selected_ids):
            raise BackendError("Список приложений изменился. Нажмите «Обновить».")
        result = self._reposition(current, selected_ids, target_id, before)
        if result == current:
            return
        apps = {key: list(value) for key, value in self.order_apps.items()}
        apps[section] = result + [item for item in apps.get(section, []) if item not in result]
        self._perform("Порядок приложений", {self.order_path: self._order_bytes(apps=apps)})

    @staticmethod
    def _icon_string(icon, fallback):
        if icon is None:
            return fallback
        if hasattr(icon, "get_names"):
            names = icon.get_names()
            return names[0] if names else fallback
        if hasattr(icon, "get_file"):
            return icon.get_file().get_path() or fallback
        return icon.to_string() or fallback

    @classmethod
    def _category_metadata(cls, key, directory, raw):
        # CMenu caches directory metadata between trees until its GLib file
        # monitor runs. Read the selected .directory directly, just as we do
        # for .desktop metadata, so hide -> show also works synchronously.
        if raw is not None:
            document = DesktopDocument(raw)
            return Category(key, document.localized("Name") or directory.get_name() or key,
                            document.localized("Icon") or cls._icon_string(directory.get_icon(), "folder"),
                            document.boolean("NoDisplay") or document.boolean("Hidden"))
        return Category(key, directory.get_name() or key,
                        cls._icon_string(directory.get_icon(), "folder"), bool(directory.get_is_nodisplay()))

    @staticmethod
    def _items(directory):
        iterator = directory.iter()
        while True:
            kind = iterator.next()
            if kind == CMenu.TreeItemType.INVALID:
                break
            if kind == CMenu.TreeItemType.DIRECTORY:
                yield "directory", iterator.get_directory()
            elif kind == CMenu.TreeItemType.ENTRY:
                yield "entry", iterator.get_entry()
            elif kind == CMenu.TreeItemType.ALIAS:
                alias = iterator.get_alias()
                try:
                    entry = alias.get_aliased_entry()
                    if entry:
                        yield "entry", entry
                except Exception:
                    pass

    def _scan_desktops(self):
        directories = [self.app_dir] + [Path(path) / "applications" for path in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if path]
        for directory in directories:
            if not directory.is_dir():
                continue
            for parent, subdirs, names in os.walk(directory, followlinks=False):
                subdirs.sort()
                for name in sorted(names):
                    if not name.endswith(".desktop"):
                        continue
                    path = Path(parent) / name
                    app_id = str(path.relative_to(directory)).replace(os.sep, "-")
                    try:
                        raw = _read(path)
                        if raw is None:
                            continue
                        document = DesktopDocument(raw)
                    except BackendError as exc:
                        self.warnings.append(str(exc))
                        continue
                    self._alternatives.setdefault(app_id, []).append((path, raw))
                    if directory == self.app_dir:
                        self._observed[path] = raw
                    if app_id not in self._sources:
                        self._sources[app_id] = path
                        self._documents[app_id] = document

    def _walk_directory(self, directory, path, category):
        self._subpaths.setdefault(category, set()).add(path)
        for kind, item in self._items(directory):
            if kind == "directory":
                self._walk_directory(item, path + (item.get_menu_id(),), category)
            else:
                app_id = item.get_desktop_file_id()
                self._tree_ids.add(app_id)
                source = Path(item.get_desktop_file_path())
                raw = _read(source)
                if raw is None:
                    continue
                try:
                    document = DesktopDocument(raw)
                except BackendError:
                    continue
                # CMenu resolves nonstandard AppDirs and same-directory ID ties.
                self._sources[app_id] = source
                self._documents[app_id] = document
                self._alternatives.setdefault(app_id, []).append((source, raw))
                if source.is_relative_to(self.app_dir):
                    self._observed[source] = raw
                container = self._excluded if item.get_is_excluded() else self._memberships
                container.setdefault(app_id, {}).setdefault(category, set()).add(path)

    def _fallback_document(self, app_id):
        for path, raw in self._alternatives.get(app_id, []):
            document = DesktopDocument(raw)
            if document.get("Name") and (document.get("Exec") or document.boolean("DBusActivatable")):
                return document
        return None

    def _document(self):
        raw = self._observed[self.menu_path]
        if raw is not None:
            return _xml(raw)
        document = minidom.Document()
        root = document.createElement("Menu")
        document.appendChild(root)
        _text_node(document, root, "Name", self._root_name)
        merge = _text_node(document, root, "MergeFile", self._canonical_path)
        merge.setAttribute("type", "parent")
        return document

    def _menu_bytes(self, document):
        raw = document.toxml(encoding="UTF-8")
        _xml(raw)
        return raw

    def _require_category(self, key):
        if key not in self._categories_by_key:
            raise BackendError("Раздел больше не существует. Обновите список.")
        return self._categories_by_key[key]

    def _require_apps(self, app_ids):
        ids = list(dict.fromkeys(app_ids))
        if not ids:
            raise BackendError("Сначала выберите приложение.")
        if any(app_id not in self._apps_by_id for app_id in ids):
            raise BackendError("Один из ярлыков больше не существует. Обновите список.")
        return ids

    def _app_document(self, app_id, *, restoring=False):
        source = self._sources[app_id]
        raw = self._documents[app_id].to_bytes()
        if _read(source) != raw:
            raise ConflictError(f"Ярлык изменён другой программой: {source.name}. Нажмите «Обновить».")
        document = DesktopDocument(raw)
        if restoring and document.boolean("Hidden") and not document.get("Exec") and not document.boolean("DBusActivatable"):
            fallback = self._fallback_document(app_id)
            if fallback:
                # Merge every existing key, including action groups; never lose
                # translations/unknown keys from an existing Hidden-only stub.
                for (group, key), value in document.values.items():
                    fallback.set(key, _unescape(value), group=group)
                document = fallback
        target = source if source.is_relative_to(self.app_dir) else self.app_dir / app_id
        self._valid_target(target)
        return document, target

    def _category_document(self, key):
        category = self._require_category(key)
        source, raw, target = self._category_sources[key]
        if source and _read(source) != raw:
            raise ConflictError("Раздел изменён другой программой. Нажмите «Обновить».")
        document = DesktopDocument(raw or b"[Desktop Entry]\nType=Directory\n")
        if not document.get("Name"):
            document.set("Name", category.name)
        if not document.get("Icon"):
            document.set("Icon", category.icon)
        self._valid_target(target)
        return document, target

    def move_apps(self, app_ids: list[str], target_key: str, source_key: str | None = None):
        category = self._require_category(target_key)
        if category.hidden:
            raise BackendError("Сначала верните целевой раздел в меню.")
        ids = self._require_apps(app_ids)
        if source_key is not None:
            self._require_category(source_key)
            if source_key == target_key:
                return
        document = self._document()
        for app_id in ids:
            sources = [source_key] if source_key is not None else list(self._memberships.get(app_id, {}))
            for source in sources:
                if source == target_key:
                    continue
                paths = {(source,)} | self._subpaths.get(source, set())
                for path in sorted(paths):
                    _filename_rule(document, _menu(document, path), app_id, False)
            _filename_rule(document, _menu(document, (target_key,)), app_id, True)
        self._perform(f"Перенос в «{category.name}»", {self.menu_path: self._menu_bytes(document)})

    def add_category(self, name: str, icon: str = "folder") -> str:
        name = _label(name, "название раздела")
        key = "mint-menu-studio-" + uuid.uuid4().hex
        file_id = key + ".directory"
        entry = DesktopDocument(b"[Desktop Entry]\nType=Directory\n")
        entry.set("Name", name, localized=True)
        entry.set("Icon", icon or "folder")
        document = self._document()
        _ensure_text(document, document.documentElement, "DirectoryDir", str(self.directory_dir))
        _text_node(document, _menu(document, (key,)), "Directory", file_id)
        self._perform(f"Новый раздел «{name}»", {self.menu_path: self._menu_bytes(document), self.directory_dir / file_id: entry.to_bytes()})
        return key

    def edit_category(self, key: str, name: str, icon: str):
        category = self._require_category(key)
        name = _label(name, "название раздела")
        if (name, icon) == (category.name, category.icon):
            return
        entry, target = self._category_document(key)
        if name != category.name:
            entry.set("Name", name, localized=True)
        if icon != category.icon:
            entry.set("Icon", icon or "folder")
        document = self._document()
        _ensure_text(document, document.documentElement, "DirectoryDir", str(self.directory_dir))
        _ensure_text(document, _menu(document, (key,)), "Directory", target.name)
        self._perform(f"Изменение раздела «{category.name}»", {target: entry.to_bytes(), self.menu_path: self._menu_bytes(document)})

    def set_category_hidden(self, key: str, hidden: bool):
        category = self._require_category(key)
        if category.hidden == bool(hidden):
            return
        entry, target = self._category_document(key)
        entry.set("NoDisplay", bool(hidden))
        if not hidden:
            entry.set("Hidden", False)
        document = self._document()
        node = _menu(document, (key,))
        if not hidden:
            for child in _children(node, "Deleted"):
                node.removeChild(child)
        _ensure_text(document, document.documentElement, "DirectoryDir", str(self.directory_dir))
        _ensure_text(document, node, "Directory", target.name)
        self._perform(("Скрытие" if hidden else "Возврат") + f" раздела «{category.name}»", {target: entry.to_bytes(), self.menu_path: self._menu_bytes(document)})

    def delete_category(self, key: str):
        """Remove a category from the menu without deleting any application."""
        self.set_category_hidden(key, True)

    def create_app(self, category_key: str, name: str, command: str, comment: str = "",
                   icon: str = "application-x-executable", terminal: bool = False) -> str:
        category = self._require_category(category_key)
        if category.hidden:
            raise BackendError("Сначала верните раздел в меню.")
        name, command = _label(name, "название приложения"), _command(command)
        app_id = "mint-menu-studio-" + uuid.uuid4().hex + ".desktop"
        entry = DesktopDocument(b"[Desktop Entry]\nType=Application\nVersion=1.0\n")
        for key, value in (("Name", name), ("Comment", comment), ("Exec", command),
                           ("Icon", icon or "application-x-executable"), ("Terminal", bool(terminal))):
            entry.set(key, value, localized=key in ("Name", "Comment"))
        document = self._document()
        _ensure_text(document, document.documentElement, "AppDir", str(self.app_dir))
        _filename_rule(document, _menu(document, (category_key,)), app_id, True)
        self._perform(f"Новый ярлык «{name}»", {self.app_dir / app_id: entry.to_bytes(), self.menu_path: self._menu_bytes(document)})
        return app_id

    def edit_app(self, app_id: str, name: str, command: str, comment: str, icon: str, terminal: bool):
        self._require_apps([app_id])
        app = self._apps_by_id[app_id]
        name = _label(name, "название приложения")
        # An unchanged empty Exec is valid for an existing DBusActivatable entry.
        if command != app.command:
            command = _command(command)
        if (name, command, comment, icon, bool(terminal)) == (app.name, app.command, app.comment, app.icon, app.terminal):
            return
        entry, target = self._app_document(app_id)
        for key, old, value in (("Name", app.name, name), ("Exec", app.command, command),
                                ("Comment", app.comment, comment), ("Icon", app.icon, icon),
                                ("Terminal", app.terminal, bool(terminal))):
            if old != value:
                entry.set(key, value, localized=key in ("Name", "Comment"))
        self._perform(f"Изменение ярлыка «{app.name}»", {target: entry.to_bytes()})

    def set_hidden(self, app_ids: list[str], hidden: bool):
        ids = self._require_apps(app_ids)
        changes = {}
        document = None
        for app_id in ids:
            app = self._apps_by_id[app_id]
            if app.hidden == bool(hidden) and (hidden or self._memberships.get(app_id) or not self._excluded.get(app_id)):
                continue
            entry, target = self._app_document(app_id, restoring=not hidden)
            entry.set("NoDisplay", bool(hidden))
            if not hidden:
                entry.set("Hidden", False)
                if not self._memberships.get(app_id) and self._excluded.get(app_id):
                    document = document or self._document()
                    for category, paths in self._excluded[app_id].items():
                        for path in paths:
                            _filename_rule(document, _menu(document, path), app_id, True)
            changes[target] = entry.to_bytes()
        if document:
            changes[self.menu_path] = self._menu_bytes(document)
        self._perform(("Скрытие" if hidden else "Возврат") + (f" «{self._apps_by_id[ids[0]].name}»" if len(ids) == 1 else f" ярлыков: {len(ids)}"), changes)

    def set_favorites(self, ids: list[str]):
        if not self.favorites_available:
            raise BackendError("Настройка избранного Cinnamon недоступна в этой сессии.")
        if not isinstance(ids, (list, tuple)) or any(not isinstance(item, str) or "\x00" in item for item in ids):
            raise BackendError("Некорректный список избранных приложений.")
        ids = list(dict.fromkeys(ids))
        # Preserve stale pre-existing IDs; only newly added IDs need to exist.
        if any(item not in self._apps_by_id and item not in self.favorites for item in ids):
            raise BackendError("Приложение для избранного больше не существует.")
        self._perform("Изменение избранного", {}, favorites=ids)

    @property
    def can_undo(self):
        return self._history["cursor"] > 0

    @property
    def can_redo(self):
        return self._history["cursor"] < len(self._history["operations"])

    @property
    def undo_label(self):
        return self._history["operations"][self._history["cursor"] - 1]["label"] if self.can_undo else ""

    @property
    def redo_label(self):
        return self._history["operations"][self._history["cursor"]]["label"] if self.can_redo else ""

    def _history_encoded(self, history):
        return json.dumps(history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _perform(self, label, changes, *, favorites=None):
        record = {"label": label, "time": time.time(), "files": {}, "favorite_before": None, "favorite_after": None}
        for path, after in changes.items():
            path = Path(path)
            self._valid_target(path)
            before = self._observed.get(path)
            if before == after:
                continue
            if after is not None and len(after) > _MAX_FILE:
                raise BackendError("Изменённый файл слишком велик для безопасного сохранения.")
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            record["files"][str(path)] = {"before": _pack(before), "after": _pack(after), "mode": mode}
        if favorites is not None and favorites != self.favorites:
            record["favorite_before"] = list(self.favorites)
            record["favorite_after"] = list(favorites)
        if not record["files"] and record["favorite_after"] is None:
            return
        history = json.loads(json.dumps(self._history))
        history["operations"] = history["operations"][:history["cursor"]] + [record]
        while len(history["operations"]) > self.history_limit or (len(history["operations"]) > 1 and len(self._history_encoded(history)) > _MAX_HISTORY):
            history["operations"].pop(0)
        history["cursor"] = len(history["operations"])
        if len(self._history_encoded(history)) > _MAX_HISTORY:
            raise BackendError("Изменение слишком велико для журнала отмены. Выберите меньше приложений.")
        self._execute(record, "after", history)
        self.refresh()

    def undo(self):
        if not self.can_undo:
            return
        history = json.loads(json.dumps(self._history))
        record = history["operations"][history["cursor"] - 1]
        history["cursor"] -= 1
        self._execute(record, "before", history)
        self.refresh()

    def redo(self):
        if not self.can_redo:
            return
        history = json.loads(json.dumps(self._history))
        record = history["operations"][history["cursor"]]
        history["cursor"] += 1
        self._execute(record, "after", history)
        self.refresh()

    def _check_record(self, record, side):
        for filename, change in record["files"].items():
            path = Path(filename)
            self._valid_target(path)
            if _read(path) != _unpack(change[side]):
                raise ConflictError(f"Файл изменён вне редактора: {path.name}. Отмена чужих изменений запрещена. Нажмите «Обновить».")
        favorite = record.get("favorite_" + side)
        if favorite is not None:
            if not self.favorites_available or self._settings is None:
                raise BackendError("Настройка избранного Cinnamon недоступна; операция не выполнена.")
            if list(self._settings.get_strv("favorite-apps")) != favorite:
                raise ConflictError("Избранное изменено вне редактора. Нажмите «Обновить».")

    def _write_setting(self, value):
        if value is not None:
            if not self._settings.set_strv("favorite-apps", value):
                raise BackendError("Cinnamon отклонил изменение избранного.")
            Gio.Settings.sync()

    def _execute(self, record, destination, history):
        source = "before" if destination == "after" else "after"
        with self._lock():
            if self.pending_path.exists():
                raise ConflictError("Есть незавершённая операция. Нажмите «Обновить» для восстановления.")
            current_history = self.history_path.read_bytes() if self.history_path.exists() else None
            if current_history != self._history_bytes:
                raise ConflictError("Журнал изменён другим экземпляром редактора. Нажмите «Обновить».")
            self._validate_record(record)
            self._check_record(record, source)
            new_history = self._history_encoded(history)
            pending = {"record": record, "destination": destination,
                       "old_history": _pack(current_history), "new_history": _pack(new_history)}
            _atomic(self.pending_path, self._history_encoded(pending), 0o600)
            written = []
            setting_written = False
            try:
                for filename, change in record["files"].items():
                    path = Path(filename)
                    if _read(path) != _unpack(change[source]):
                        raise ConflictError(f"Файл изменился во время сохранения: {path.name}")
                    _atomic(path, _unpack(change[destination]), change.get("mode", 0o644))
                    written.append(filename)
                favorite = record.get("favorite_" + destination)
                if favorite is not None:
                    if list(self._settings.get_strv("favorite-apps")) != record["favorite_" + source]:
                        raise ConflictError("Избранное изменилось во время сохранения.")
                    self._write_setting(favorite)
                    setting_written = True
                _atomic(self.history_path, new_history, 0o600)
            except Exception as exc:
                rollback_conflicts = []
                for filename in reversed(written):
                    change = record["files"][filename]
                    if _read(Path(filename)) == _unpack(change[destination]):
                        try:
                            _atomic(Path(filename), _unpack(change[source]), change.get("mode", 0o644))
                        except Exception:
                            rollback_conflicts.append(filename)
                    else:
                        rollback_conflicts.append(filename)
                if setting_written:
                    if list(self._settings.get_strv("favorite-apps")) == record["favorite_" + destination]:
                        self._write_setting(record["favorite_" + source])
                    else:
                        rollback_conflicts.append("избранное")
                # A directory fsync can fail after the history rename itself
                # succeeded. Restore the old journal as well before reporting
                # that the user-visible operation was rolled back.
                actual_history = self.history_path.read_bytes() if self.history_path.exists() else None
                if actual_history == new_history:
                    try:
                        _atomic(self.history_path, current_history, 0o600)
                    except Exception:
                        rollback_conflicts.append("журнал отмены")
                elif actual_history != current_history:
                    rollback_conflicts.append("журнал отмены")
                if not rollback_conflicts:
                    _atomic(self.pending_path, None, 0o600)
                if rollback_conflicts:
                    raise ConflictError("Сохранение прервано внешним изменением; журнал восстановления сохранён: " + ", ".join(rollback_conflicts)) from exc
                if isinstance(exc, BackendError):
                    raise
                raise BackendError(f"Сохранение не выполнено, исходные файлы восстановлены: {exc}") from exc
            # If interrupted here, recovery recognises the committed history.
            _atomic(self.pending_path, None, 0o600)
            self._history = history
            self._history_bytes = new_history

    def _recover(self):
        if not self.pending_path.exists():
            return
        try:
            raw = self.pending_path.read_bytes()
            if len(raw) > _MAX_HISTORY * 4:
                raise ValueError("слишком большой журнал восстановления")
            pending = json.loads(raw)
            record = pending["record"]
            self._validate_record(record)
            destination = pending["destination"]
            if destination not in ("before", "after"):
                raise ValueError("неверное направление")
            source = "before" if destination == "after" else "after"
            old_history = base64.b64decode(pending["old_history"], validate=True) if pending["old_history"] is not None else None
            new_history = base64.b64decode(pending["new_history"], validate=True)
            current_history = self.history_path.read_bytes() if self.history_path.exists() else None
            if current_history == new_history:
                self._check_record(record, destination)
                _atomic(self.pending_path, None, 0o600)
                return
            if current_history != old_history:
                raise ConflictError("Журнал изменён после прерывания сохранения. Автоматическое восстановление остановлено.")
            # Validate every resource before restoring any of them.
            for filename, change in record["files"].items():
                if _read(Path(filename)) not in (_unpack(change[source]), _unpack(change[destination])):
                    raise ConflictError(f"После прерывания изменён файл {Path(filename).name}. Чужие изменения сохранены.")
            favorite = record.get("favorite_" + destination)
            if favorite is not None:
                if not self.favorites_available:
                    raise BackendError("Для восстановления операции нужен доступ к избранному Cinnamon.")
                if list(self._settings.get_strv("favorite-apps")) not in (record["favorite_" + source], favorite):
                    raise ConflictError("После прерывания изменено избранное. Чужие изменения сохранены.")
            for filename, change in record["files"].items():
                path = Path(filename)
                if _read(path) == _unpack(change[destination]):
                    _atomic(path, _unpack(change[source]), change.get("mode", 0o644))
            if favorite is not None:
                self._write_setting(record["favorite_" + source])
            _atomic(self.pending_path, None, 0o600)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Не удалось безопасно восстановить прерванное изменение: {exc}") from exc
