#!/usr/bin/python3
"""Small, local GTK editor for the real Cinnamon application menu."""
from __future__ import annotations

import html
import os
import sys
import traceback
from pathlib import Path

if "--version" in sys.argv:
    print("Mint Menu Studio 1.1.1")
    raise SystemExit(0)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango

from menu_backend import Backend, BackendError, ConflictError

VERSION = "1.1.1"
APP_ID = "ru.amosh.MenuStudio"
DRAG_TYPE = "application/x-mint-menu-studio-apps"
CATEGORY_DRAG_TYPE = "application/x-mint-menu-studio-category"


def icon(value, fallback="application-x-executable"):
    try:
        if value:
            return Gio.Icon.new_for_string(value)
    except (GLib.Error, TypeError):
        pass
    return Gio.ThemedIcon.new(fallback)


def button(label, callback, icon_name=None, tooltip=None):
    result = Gtk.Button.new_with_label(label)
    if icon_name:
        result.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
        result.set_always_show_image(True)
    result.connect("clicked", callback)
    if tooltip:
        result.set_tooltip_text(tooltip)
    return result


def scrolled(child):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.add(child)
    return scroll


class EditorWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Редактор меню")
        self.set_default_size(1080, 740)
        self.set_size_request(860, 560)
        self.set_icon_name("applications-system")
        self.set_wmclass("mint-menu-studio", "MintMenuStudio")
        self.backend = Backend()
        self.section = "@all"
        self.rebuilding = False
        self.drag_ids = []
        self.drag_category_key = None
        self.drag_candidates = []
        self.status_timeout = None
        self.closed = False
        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
        .mms-sidebar { background-color: alpha(@theme_fg_color, 0.035); }
        .mms-heading { font-size: 21px; font-weight: bold; }
        .mms-muted { opacity: 0.65; }
        .mms-empty { font-size: 16px; opacity: 0.65; }
        .mms-status { padding: 10px 14px; background-color: alpha(@theme_fg_color, 0.04); }
        treeview { padding: 4px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Gtk.HeaderBar(title="Редактор меню", subtitle="Приложения, разделы и избранное")
        header.set_show_close_button(True)
        header.pack_start(button("Раздел", self.new_category, "folder-new-symbolic"))
        header.pack_start(button("Приложение", self.new_app, "list-add-symbolic"))
        header.pack_end(button("", self.help_dialog, "help-browser-symbolic", "Как пользоваться"))
        header.pack_end(button("", self.manual_refresh, "view-refresh-symbolic", "Обновить меню · F5"))
        self.redo_button = button("", lambda *_: self.change(self.backend.redo, "Изменение повторено"),
                                  "edit-redo-symbolic", "Повторить · Ctrl+Shift+Z")
        self.undo_button = button("", lambda *_: self.change(self.backend.undo, "Изменение отменено"),
                                  "edit-undo-symbolic", "Отменить · Ctrl+Z")
        header.pack_end(self.redo_button)
        header.pack_end(self.undo_button)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        panes.set_position(255)
        outer.pack_start(panes, True, True, 0)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sidebar.set_size_request(225, -1)
        sidebar.get_style_context().add_class("mms-sidebar")
        panes.pack1(sidebar, False, False)
        sidebar_title = Gtk.Label(label="Разделы меню", xalign=0)
        sidebar_title.set_margin_start(17)
        sidebar_title.set_margin_top(16)
        sidebar_title.get_style_context().add_class("mms-muted")
        sidebar.pack_start(sidebar_title, False, False, 0)
        self.category_store = Gtk.ListStore(object, str, str, str)
        self.category_view = Gtk.TreeView(model=self.category_store, headers_visible=False)
        self.category_view.set_enable_search(False)
        self.category_view.get_selection().set_mode(Gtk.SelectionMode.BROWSE)
        col = Gtk.TreeViewColumn()
        col.set_spacing(10)
        col.set_expand(True)
        col.set_min_width(155)
        pix = Gtk.CellRendererPixbuf()
        pix.set_property("stock-size", Gtk.IconSize.MENU)
        col.pack_start(pix, False)
        col.set_cell_data_func(pix, lambda c, cell, m, it, d: cell.set_property("gicon", m[it][0]))
        name_cell = Gtk.CellRendererText()
        name_cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_cell.set_property("ypad", 5)
        col.pack_start(name_cell, True)
        col.add_attribute(name_cell, "markup", 1)
        self.category_view.append_column(col)
        count_col = Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=2)
        count_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        count_col.set_fixed_width(35)
        self.category_view.append_column(count_col)
        self.category_view.get_selection().connect("changed", self.on_category)
        self.category_view.connect("button-press-event", self.category_popup)
        sidebar.pack_start(scrolled(self.category_view), True, True, 0)
        self.show_empty_categories = Gtk.CheckButton.new_with_label("Пустые системные разделы")
        self.show_empty_categories.set_margin_start(12)
        self.show_empty_categories.set_margin_end(8)
        self.show_empty_categories.connect("toggled", lambda *_: self.refresh_view())
        sidebar.pack_start(self.show_empty_categories, False, False, 0)
        category_actions = Gtk.Box(spacing=6)
        category_actions.set_margin_start(12)
        category_actions.set_margin_end(12)
        category_actions.set_margin_bottom(12)
        self.category_edit = button("Изменить", self.edit_category, "document-edit-symbolic")
        self.category_remove = button("", self.remove_category, "user-trash-symbolic", "Убрать или вернуть раздел")
        category_actions.pack_start(self.category_edit, True, True, 0)
        category_actions.pack_start(self.category_remove, False, False, 0)
        sidebar.pack_end(category_actions, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_top(18)
        content.set_margin_bottom(14)
        panes.pack2(content, True, False)
        self.heading = Gtk.Label(label="Все приложения", xalign=0)
        self.heading.get_style_context().add_class("mms-heading")
        content.pack_start(self.heading, False, False, 0)
        self.hint = Gtk.Label(label="Перетащите приложение в раздел слева или нажмите «Переместить».", xalign=0)
        self.hint.set_line_wrap(True)
        self.hint.get_style_context().add_class("mms-muted")
        content.pack_start(self.hint, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Найти приложение по названию, описанию или команде")
        self.search.connect("search-changed", lambda *_: self.populate_apps())
        content.pack_start(self.search, False, False, 0)

        # icon, escaped title/comment, category names, desktop id, starred icon.
        self.app_store = Gtk.ListStore(object, str, str, str, str)
        self.app_view = Gtk.TreeView(model=self.app_store)
        self.app_view.set_enable_search(False)
        self.app_view.set_headers_visible(True)
        self.app_view.set_rubber_banding(False)
        self.app_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.app_view.get_selection().connect("changed", self.selection_changed)
        self.app_view.connect("row-activated", lambda *_: self.edit_app())
        self.app_view.connect("button-press-event", self.app_popup)
        app_col = Gtk.TreeViewColumn("Приложение")
        app_col.set_expand(True)
        app_col.set_spacing(12)
        app_col.set_min_width(240)
        app_pix = Gtk.CellRendererPixbuf()
        app_pix.set_property("stock-size", Gtk.IconSize.DND)
        app_col.pack_start(app_pix, False)
        app_col.set_cell_data_func(app_pix, lambda c, cell, m, it, d: cell.set_property("gicon", m[it][0]))
        app_name = Gtk.CellRendererText()
        app_name.set_property("ellipsize", Pango.EllipsizeMode.END)
        app_name.set_property("ypad", 10)
        app_col.pack_start(app_name, True)
        app_col.add_attribute(app_name, "markup", 1)
        self.app_view.append_column(app_col)
        category_cell = Gtk.CellRendererText()
        category_cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.membership_column = Gtk.TreeViewColumn("Раздел", category_cell, text=2)
        self.membership_column.set_max_width(200)
        self.membership_column.set_min_width(140)
        self.app_view.append_column(self.membership_column)
        star_cell = Gtk.CellRendererPixbuf()
        star_column = Gtk.TreeViewColumn("", star_cell, icon_name=4)
        self.app_view.append_column(star_column)

        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(scrolled(self.app_view), "list")
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_halign(Gtk.Align.CENTER)
        empty_image = Gtk.Image.new_from_icon_name("view-list-symbolic", Gtk.IconSize.DIALOG)
        empty_image.set_pixel_size(52)
        empty.pack_start(empty_image, False, False, 0)
        self.empty_label = Gtk.Label(label="Здесь пока нет приложений")
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.set_line_wrap(True)
        self.empty_label.get_style_context().add_class("mms-empty")
        empty.pack_start(self.empty_label, False, False, 0)
        self.list_stack.add_named(empty, "empty")
        content.pack_start(self.list_stack, True, True, 0)

        actions = Gtk.Box(spacing=6)
        self.edit_button = button("Изменить", self.edit_app, "document-edit-symbolic")
        self.move_button = button("Переместить", self.move_dialog, "folder-move-symbolic")
        self.favorite_button = button("В избранное", self.toggle_favorites, "non-starred-symbolic")
        self.hide_button = button("Убрать", self.hide_apps, "view-conceal-symbolic")
        for w in [self.edit_button, self.move_button, self.favorite_button, self.hide_button]:
            actions.pack_start(w, False, False, 0)
        self.up_button = button("↑", lambda *_: self.reorder_favorite(-1), tooltip="Поднять в избранном")
        self.down_button = button("↓", lambda *_: self.reorder_favorite(1), tooltip="Опустить в избранном")
        actions.pack_end(self.down_button, False, False, 0)
        actions.pack_end(self.up_button, False, False, 0)
        content.pack_start(actions, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("mms-status")
        outer.pack_end(self.status, False, False, 0)

        targets = [Gtk.TargetEntry.new(DRAG_TYPE, Gtk.TargetFlags.SAME_APP, 0)]
        category_targets = [Gtk.TargetEntry.new(CATEGORY_DRAG_TYPE, Gtk.TargetFlags.SAME_APP, 1)]
        self.app_view.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, targets, Gdk.DragAction.MOVE)
        self.app_view.drag_dest_set(Gtk.DestDefaults(0), targets, Gdk.DragAction.MOVE)
        self.app_view.connect("drag-data-get", self.drag_data_get)
        self.app_view.connect("drag-begin", self.drag_begin)
        self.app_view.connect("drag-motion", self.app_drag_motion)
        self.app_view.connect("drag-drop", self.app_drag_drop)
        self.app_view.connect("drag-data-received", self.app_drag_received)
        self.category_view.enable_model_drag_source(
            Gdk.ModifierType.BUTTON1_MASK, category_targets, Gdk.DragAction.MOVE)
        self.category_view.connect("drag-begin", self.category_drag_begin)
        self.category_view.connect("drag-data-get", self.category_drag_data_get)
        self.category_view.drag_dest_set(
            Gtk.DestDefaults(0), targets + category_targets, Gdk.DragAction.MOVE)
        self.category_view.connect("drag-data-received", self.drag_received)
        self.category_view.connect("drag-motion", self.drag_motion)
        self.category_view.connect("drag-drop", self.drag_drop)

        self.refresh_view()
        self.show_all()
        self.selection_changed()
        self.refresh_timer = GLib.timeout_add_seconds(6, self.external_refresh)

    def on_destroy(self, *_):
        self.closed = True
        if hasattr(self, "refresh_timer"):
            GLib.source_remove(self.refresh_timer)

    def app_map(self):
        return {a.id: a for a in self.backend.apps}

    def selected_ids(self):
        model, paths = self.app_view.get_selection().get_selected_rows()
        return [model[p][3] for p in paths]

    def selected_category(self):
        return next((c for c in self.backend.categories if c.key == self.section), None)

    def show_status(self, message):
        self.status.set_text(message)

    def refresh_view(self, select_ids=None):
        selected = self.selected_ids() if select_ids is None else select_ids
        self.rebuilding = True
        self.category_store.clear()
        visible = [a for a in self.backend.apps if not a.hidden]
        rows = [("@all", "Все приложения", "view-grid-symbolic", len(visible)),
                ("@favorites", "Избранное", "starred-symbolic", len(self.backend.favorites)),
                ("@hidden", "Скрытые приложения", "view-conceal-symbolic",
                 sum(a.hidden for a in self.backend.apps))]
        for key, title, image, count in rows:
            self.category_store.append([icon(image), html.escape(title), str(count), key])
        for category in self.backend.ordered_categories():
            count = sum(not a.hidden and category.key in a.categories for a in self.backend.apps)
            if (count == 0 and not category.hidden and not category.key.startswith("mint-menu-studio-")
                    and category.key != self.section and not self.show_empty_categories.get_active()):
                continue
            title = html.escape(category.name)
            if category.hidden:
                title = '<span alpha="55%">' + title + ' · скрыт</span>'
            self.category_store.append([icon(category.icon, "folder"), title, str(count), category.key])
        available = {row[3] for row in self.category_store}
        if self.section not in available:
            self.section = "@all"
        for row in self.category_store:
            if row[3] == self.section:
                self.category_view.get_selection().select_path(row.path)
                break
        self.rebuilding = False
        self.populate_apps(selected)
        self.undo_button.set_sensitive(self.backend.can_undo)
        self.redo_button.set_sensitive(self.backend.can_redo)
        self.undo_button.set_tooltip_text("Отменить: " + (self.backend.undo_label or "нет изменений") + " · Ctrl+Z")
        self.redo_button.set_tooltip_text("Повторить: " + (self.backend.redo_label or "нет изменений") + " · Ctrl+Shift+Z")
        self.category_edit.set_sensitive(self.selected_category() is not None)
        self.category_remove.set_sensitive(self.selected_category() is not None)
        self.show_status("Изменения сохраняются сразу. Отмена работает и после закрытия редактора.")

    def populate_apps(self, selected=None):
        selected = self.selected_ids() if selected is None else selected
        query = self.search.get_text().strip().casefold()
        category = self.selected_category()
        apps = list(self.backend.apps)
        if query:
            apps = [a for a in apps if query in " ".join([a.name, a.comment or "", a.command or ""]).casefold()]
            title = "Результаты поиска"
            hint = "Поиск по всем приложениям. Перенос заменит все текущие разделы приложения."
        elif self.section == "@favorites":
            app_map = self.app_map()
            apps = [app_map[i] for i in self.backend.favorites if i in app_map]
            title = "Избранное"
            hint = "Эти значки находятся слева в меню. Их порядок можно менять стрелками ↑ и ↓."
        elif self.section == "@hidden":
            apps = [a for a in apps if a.hidden]
            title = "Скрытые приложения"
            hint = "Выберите приложение и нажмите «Вернуть», чтобы снова показать его в меню."
        elif category:
            apps = self.backend.ordered_apps(category.key)
            title = category.name
            hint = ("Раздел скрыт. Нажмите кнопку корзины слева, чтобы вернуть его."
                    if category.hidden else
                    "Перетащите приложение выше или ниже для сортировки; в раздел слева — для переноса.")
        else:
            apps = self.backend.ordered_apps("@all")
            title = "Все приложения"
            hint = "Перетащите приложение выше или ниже для сортировки или в раздел слева для переноса."
        if query or self.section == "@hidden":
            apps.sort(key=lambda a: a.name.casefold())
        self.heading.set_text(title)
        self.hint.set_text(hint)
        self.app_store.clear()
        names = {c.key: c.name for c in self.backend.categories}
        favorite_ids = set(self.backend.favorites)
        for app in apps:
            title_markup = "<b>" + html.escape(app.name) + "</b>"
            comment = (app.comment or "").replace("\n", " ").strip()
            if app.hidden:
                comment = "Скрыто в меню" + (" · " + comment if comment else "")
            if comment:
                title_markup += '\n<span size="small" alpha="65%">' + html.escape(comment[:140]) + "</span>"
            membership = ", ".join(names.get(k, k) for k in app.categories) or "Без раздела"
            self.app_store.append([icon(app.icon), title_markup, membership, app.id,
                                   "starred-symbolic" if app.id in favorite_ids else None])
        selection = self.app_view.get_selection()
        for row in self.app_store:
            if row[3] in selected:
                selection.select_path(row.path)
        if apps:
            self.list_stack.set_visible_child_name("list")
        else:
            self.list_stack.set_visible_child_name("empty")
            if query:
                self.empty_label.set_text("Ничего не найдено\nПопробуйте другое название или команду")
            elif category:
                self.empty_label.set_text("В разделе пока нет приложений\nДобавьте или переместите сюда приложение —\nпосле этого раздел появится в меню Cinnamon")
            elif self.section == "@hidden":
                self.empty_label.set_text("Скрытых приложений нет")
            elif self.section == "@favorites":
                self.empty_label.set_text("В избранном пока пусто\nПеретащите сюда приложение из любого раздела")
            else:
                self.empty_label.set_text("Здесь пока нет приложений")
        self.selection_changed()

    def on_category(self, selection):
        if self.rebuilding:
            return
        model, it = selection.get_selected()
        if it is not None:
            self.section = model[it][3]
            self.search.set_text("")
            self.populate_apps([])
            category = self.selected_category()
            self.category_edit.set_sensitive(category is not None)
            self.category_remove.set_sensitive(category is not None)
            self.category_remove.set_tooltip_text("Вернуть раздел" if category and category.hidden else "Убрать раздел")

    def selection_changed(self, *_):
        if not hasattr(self, "edit_button"):
            return
        ids = self.selected_ids()
        app_map = self.app_map()
        selected = [app_map[i] for i in ids if i in app_map]
        count = len(selected)
        self.edit_button.set_sensitive(count == 1)
        self.move_button.set_sensitive(count > 0)
        self.hide_button.set_sensitive(count > 0)
        self.hide_button.set_label("Вернуть" if selected and all(a.hidden for a in selected) else "Убрать")
        favorites = set(self.backend.favorites)
        all_favorite = bool(ids) and all(i in favorites for i in ids)
        self.favorite_button.set_label("Из избранного" if all_favorite else "В избранное")
        self.favorite_button.set_sensitive(count > 0 and getattr(self.backend, "favorites_available", True))
        reorder = self.section == "@favorites" and not self.search.get_text()
        self.up_button.set_visible(reorder)
        self.down_button.set_visible(reorder)
        self.up_button.set_sensitive(count == 1)
        self.down_button.set_sensitive(count == 1)
        if count:
            self.show_status("Выбрано: " + str(count) + (" · Двойной щелчок — свойства" if count == 1 else " · Перенос и скрытие применятся ко всем выбранным"))

    def change(self, operation, message, select_ids=None):
        try:
            result = operation()
            self.refresh_view(select_ids)
            self.show_status(message)
            return result
        except (BackendError, GLib.Error, OSError, ValueError) as exc:
            self.error_dialog(str(exc))
            return None

    def external_refresh(self):
        if self.closed:
            return False
        if any(isinstance(w, Gtk.Dialog) and w.get_visible() for w in Gtk.Window.list_toplevels()):
            return True
        try:
            before = repr((self.backend.apps, self.backend.categories, self.backend.favorites,
                           self.backend.order_apps, self.backend.order_categories))
            self.backend.refresh()
            after = repr((self.backend.apps, self.backend.categories, self.backend.favorites,
                          self.backend.order_apps, self.backend.order_categories))
            if before != after:
                self.refresh_view()
                self.show_status("Меню обновлено: обнаружены изменения вне редактора.")
        except Exception as exc:
            self.show_status("Не удалось обновить меню: " + str(exc))
        return True

    def manual_refresh(self, *_):
        try:
            self.backend.refresh()
            self.refresh_view()
            self.show_status("Меню обновлено.")
        except (BackendError, GLib.Error, OSError, ValueError) as exc:
            self.error_dialog(str(exc))

    def error_dialog(self, text):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text="Изменение не применено")
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def confirm(self, title, explanation, action="Убрать"):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text(explanation)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button(action, Gtk.ResponseType.OK)
        result = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return result

    def category_combo(self, default=None):
        combo = Gtk.ComboBoxText()
        categories = sorted((c for c in self.backend.categories if not c.hidden), key=lambda c: c.name.casefold())
        for category in categories:
            combo.append(category.key, category.name)
        if default and any(c.key == default for c in categories):
            combo.set_active_id(default)
        elif categories:
            preferred = next((c.key for c in categories if c.key.lower() in ["utility", "accessories"]), categories[0].key)
            combo.set_active_id(preferred)
        return combo

    def choose_file(self, entry, title, image_only=False):
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self, modal=True,
                                        action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Выбрать", Gtk.ResponseType.OK)
        if image_only:
            file_filter = Gtk.FileFilter()
            file_filter.set_name("Значки и изображения")
            for mime in ["image/png", "image/svg+xml", "image/jpeg", "image/x-xpixmap"]:
                file_filter.add_mime_type(mime)
            dialog.add_filter(file_filter)
        if dialog.run() == Gtk.ResponseType.OK:
            value = dialog.get_filename()
            if not image_only:
                # Desktop Entry Exec quoting is double-quoted, not shell quoting.
                value = '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
            entry.set_text(value)
        dialog.destroy()

    def property_dialog(self, app=None, category=None, creating_category=False):
        is_category = category is not None or creating_category
        title = ("Новый раздел" if creating_category else "Свойства раздела") if is_category else ("Свойства приложения" if app else "Новое приложение")
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True, destroy_with_parent=True)
        dialog.set_default_size(620, -1)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        save = dialog.add_button("Создать" if app is None and category is None else "Сохранить", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=14)
        grid.set_border_width(22)
        dialog.get_content_area().pack_start(grid, True, True, 0)
        row = 0
        def field(label, widget):
            nonlocal row
            caption = Gtk.Label(label=label, xalign=0)
            caption.set_valign(Gtk.Align.CENTER)
            grid.attach(caption, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            row += 1
        name = Gtk.Entry(text=(category.name if category else app.name if app else ""))
        name.set_activates_default(True)
        name.set_placeholder_text("Например, Работа или Мой редактор")
        field("Название", name)
        name.set_name("mms-name")
        command = Gtk.Entry(text=app.command if app else "")
        comment = Gtk.Entry(text=(app.comment or "") if app else "")
        terminal = Gtk.CheckButton.new_with_label("Запускать в терминале")
        terminal.set_active(app.terminal if app else False)
        destination = None
        if not is_category:
            command.set_name("mms-command")
            command.set_placeholder_text("Команда запуска, например firefox %u")
            command_box = Gtk.Box(spacing=6)
            command_box.pack_start(command, True, True, 0)
            command_box.pack_end(button("Обзор…", lambda *_: self.choose_file(command, "Выберите программу")), False, False, 0)
            field("Команда", command_box)
            field("Описание", comment)
            if app is None:
                destination = self.category_combo(self.section)
                field("Раздел", destination)
        default_icon = "folder" if is_category else "application-x-executable"
        icon_entry = Gtk.Entry(text=(category.icon if category else app.icon if app else default_icon) or default_icon)
        icon_entry.set_placeholder_text("Имя значка из темы или путь к изображению")
        icon_box = Gtk.Box(spacing=6)
        icon_preview = Gtk.Image.new_from_gicon(icon(icon_entry.get_text(), default_icon), Gtk.IconSize.DND)
        icon_entry.connect("changed", lambda *_: icon_preview.set_from_gicon(icon(icon_entry.get_text(), default_icon), Gtk.IconSize.DND))
        icon_box.pack_start(icon_preview, False, False, 2)
        icon_box.pack_start(icon_entry, True, True, 0)
        icon_box.pack_end(button("Файл…", lambda *_: self.choose_file(icon_entry, "Выберите значок", True)), False, False, 0)
        field("Значок", icon_box)
        if not is_category:
            field("", terminal)
        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(62)
        note.get_style_context().add_class("mms-muted")
        note.set_text("Новый раздел появится в меню Cinnamon после добавления приложения." if is_category else "Команда будет сохранена в ярлыке. Редактор её не запускает. Для веб-сайта можно указать: xdg-open https://адрес")
        grid.attach(note, 0, row, 2, 1)
        row += 1
        error = Gtk.Label(xalign=0)
        error.set_line_wrap(True)
        grid.attach(error, 0, row, 2, 1)
        dialog.show_all()
        name.grab_focus()
        while dialog.run() == Gtk.ResponseType.OK:
            if not name.get_text().strip():
                error.set_markup('<span foreground="#e57373">Введите название.</span>')
                name.grab_focus()
                continue
            if not is_category and not command.get_text().strip():
                error.set_markup('<span foreground="#e57373">Введите команду запуска.</span>')
                command.grab_focus()
                continue
            try:
                if category:
                    self.backend.edit_category(category.key, name.get_text().strip(), icon_entry.get_text().strip())
                    self.section = category.key
                elif creating_category:
                    self.section = self.backend.add_category(name.get_text().strip(), icon_entry.get_text().strip())
                elif app:
                    self.backend.edit_app(app.id, name.get_text().strip(), command.get_text().strip(), comment.get_text(), icon_entry.get_text().strip(), terminal.get_active())
                else:
                    if not destination.get_active_id():
                        raise BackendError("Сначала создайте раздел меню.")
                    self.section = destination.get_active_id()
                    self.backend.create_app(self.section, name.get_text().strip(), command.get_text().strip(), comment.get_text(), icon_entry.get_text().strip(), terminal.get_active())
                self.search.set_text("")
                self.refresh_view()
                self.show_status("Сохранено. Изменение можно отменить кнопкой ↶ или Ctrl+Z.")
                break
            except (BackendError, GLib.Error, OSError, ValueError) as exc:
                error.set_markup('<span foreground="#e57373">' + html.escape(str(exc)) + '</span>')
        dialog.destroy()

    def new_category(self, *_):
        self.property_dialog(creating_category=True)

    def edit_category(self, *_):
        category = self.selected_category()
        if category:
            self.property_dialog(category=category)

    def remove_category(self, *_):
        category = self.selected_category()
        if not category:
            return
        if category.hidden:
            self.change(lambda: self.backend.set_category_hidden(category.key, False), "Раздел возвращён в меню.")
        elif self.confirm("Убрать раздел «" + category.name + "»?",
                          "Раздел исчезнет из меню. Программы останутся установленными. Его можно вернуть здесь или отменить действие."):
            self.change(lambda: self.backend.delete_category(category.key), "Раздел скрыт. Его можно вернуть в редакторе.")

    def new_app(self, *_):
        self.property_dialog()

    def edit_app(self, *_):
        ids = self.selected_ids()
        if len(ids) == 1:
            app = self.app_map().get(ids[0])
            if app:
                self.property_dialog(app=app)

    def move_to(self, ids, destination):
        if not ids:
            return
        category = self.selected_category()
        source = category.key if category and not self.search.get_text().strip() else None
        if source == destination:
            self.show_status("Приложение уже находится в этом разделе.")
            return
        self.change(lambda: self.backend.move_apps(ids, destination, source), "Перемещено. Чтобы отменить: Ctrl+Z.", ids)

    def move_dialog(self, *_):
        ids = self.selected_ids()
        if not ids:
            return
        dialog = Gtk.Dialog(title="Переместить в раздел", transient_for=self, modal=True)
        dialog.set_default_size(430, -1)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Переместить", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(22)
        box.set_spacing(14)
        label = Gtk.Label(label="Выбрано приложений: " + str(len(ids)), xalign=0)
        box.add(label)
        destination = self.category_combo()
        box.add(destination)
        note = Gtk.Label(label="Значки появятся в выбранном разделе.\nСам способ запуска приложений сохранится.", xalign=0)
        note.get_style_context().add_class("mms-muted")
        box.add(note)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK and destination.get_active_id():
            self.move_to(ids, destination.get_active_id())
        dialog.destroy()

    def hide_apps(self, *_):
        ids = self.selected_ids()
        apps = self.app_map()
        if not ids:
            return
        hidden = not all(apps[i].hidden for i in ids if i in apps)
        if hidden and not self.confirm("Убрать выбранные приложения из меню?",
                                       "Программы останутся установленными. Ярлыки будут доступны в «Скрытых приложениях»; оттуда их можно вернуть."):
            return
        self.change(lambda: self.backend.set_hidden(ids, hidden), "Приложения убраны из меню." if hidden else "Приложения возвращены в меню.")

    def toggle_favorites(self, *_):
        ids = self.selected_ids()
        if not ids:
            return
        favorites = list(self.backend.favorites)
        if all(i in favorites for i in ids):
            favorites = [i for i in favorites if i not in ids]
        else:
            favorites.extend(i for i in ids if i not in favorites)
        self.change(lambda: self.backend.set_favorites(favorites), "Избранное обновлено.", ids)

    def reorder_favorite(self, delta):
        ids = self.selected_ids()
        if len(ids) != 1 or ids[0] not in self.backend.favorites:
            return
        favorites = list(self.backend.favorites)
        old = favorites.index(ids[0])
        new = old + delta
        if not 0 <= new < len(favorites):
            return
        favorites[old], favorites[new] = favorites[new], favorites[old]
        self.change(lambda: self.backend.set_favorites(favorites), "Порядок избранного изменён.", ids)

    def drag_begin(self, widget, context):
        self.drag_ids = list(self.drag_candidates or self.selected_ids())
        if len(self.drag_ids) > 1:
            selection = self.app_view.get_selection()
            for row in self.app_store:
                if row[3] in self.drag_ids:
                    selection.select_path(row.path)
        if os.environ.get("MMS_UI_TRACE"):
            print("drag-begin", self.drag_ids, flush=True)
        if self.drag_ids:
            Gtk.drag_set_icon_name(context, "applications-other", 0, 0)

    def drag_data_get(self, widget, context, selection_data, info, time):
        import json
        if os.environ.get("MMS_UI_TRACE"):
            print("drag-data-get", self.drag_ids, flush=True)
        selection_data.set(selection_data.get_target(), 8, json.dumps(self.drag_ids).encode())

    @staticmethod
    def drop_position(widget, x, y):
        bx, by = widget.convert_widget_to_bin_window_coords(x, y)
        found = widget.get_path_at_pos(bx, by)
        if not found:
            return None, None
        path = found[0]
        rect = widget.get_cell_area(path, None)
        before = by < rect.y + rect.height / 2
        return path, before

    def category_drag_begin(self, widget, context):
        self.drag_category_key = self.section if self.selected_category() else None
        if self.drag_category_key:
            Gtk.drag_set_icon_name(context, "folder", 0, 0)

    def category_drag_data_get(self, widget, context, selection_data, info, time):
        selection_data.set(selection_data.get_target(), 8,
                           (self.drag_category_key or "").encode("utf-8"))

    def drag_motion(self, widget, context, x, y, time):
        path, before = self.drop_position(widget, x, y)
        key = self.category_store[path][3] if path is not None else None
        from_category = Gtk.drag_get_source_widget(context) == self.category_view
        category = next((c for c in self.backend.categories if c.key == key), None)
        if from_category:
            valid = bool(category and self.drag_category_key and key != self.drag_category_key)
        else:
            valid = key == "@favorites" or bool(category and not category.hidden)
        if valid:
            pos = (Gtk.TreeViewDropPosition.BEFORE if before
                   else Gtk.TreeViewDropPosition.AFTER) if from_category else Gtk.TreeViewDropPosition.INTO_OR_AFTER
            widget.set_drag_dest_row(path, pos)
            Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
        else:
            widget.set_drag_dest_row(None, Gtk.TreeViewDropPosition.INTO_OR_AFTER)
            Gdk.drag_status(context, Gdk.DragAction(0), time)
        return True

    def drag_drop(self, widget, context, x, y, time):
        path, _before = self.drop_position(widget, x, y)
        if path is not None:
            key = self.category_store[path][3]
            category = next((c for c in self.backend.categories if c.key == key), None)
            from_category = Gtk.drag_get_source_widget(context) == self.category_view
            if (from_category and category and key != self.drag_category_key or
                not from_category and (key == "@favorites" or category and not category.hidden)):
                target = widget.drag_dest_find_target(context, None)
                if target is not None:
                    widget.drag_get_data(context, target, time)
                    return True
        Gtk.drag_finish(context, False, False, time)
        return True

    def drag_received(self, widget, context, x, y, data, info, time):
        import json
        widget.stop_emission_by_name("drag-data-received")
        path, before = self.drop_position(widget, x, y)
        success = False
        try:
            if path is None:
                raise BackendError("Перетащите на нужную строку раздела.")
            key = self.category_store[path][3]
            if Gtk.drag_get_source_widget(context) == self.category_view:
                source = bytes(data.get_data()).decode("utf-8")
                if source != self.drag_category_key or source == key:
                    raise BackendError("Не удалось определить перемещаемый раздел.")
                self.backend.reorder_category(source, key, before)
                self.section = source
                success = True
                self.refresh_view()
                self.show_status("Порядок разделов изменён. Отмена: Ctrl+Z.")
            else:
                ids = json.loads(bytes(data.get_data()).decode())
                if not isinstance(ids, list) or not all(
                        isinstance(i, str) and i in self.app_map() for i in ids):
                    raise ValueError("Некорректные данные перетаскивания")
                if ids and key == "@favorites":
                    favorites = list(self.backend.favorites)
                    favorites.extend(i for i in ids if i not in favorites)
                    self.backend.set_favorites(favorites)
                    success = True
                elif ids and any(c.key == key and not c.hidden for c in self.backend.categories):
                    category = self.selected_category()
                    source = category.key if category and not self.search.get_text().strip() else None
                    self.backend.move_apps(ids, key, source)
                    success = True
                if success:
                    self.refresh_view(ids)
                    self.show_status("Добавлено в избранное." if key == "@favorites"
                                     else "Приложения перемещены. Отмена: Ctrl+Z.")
        except (BackendError, GLib.Error, OSError, ValueError, TypeError) as exc:
            self.error_dialog(str(exc))
        Gtk.drag_finish(context, success, False, time)
        widget.set_drag_dest_row(None, Gtk.TreeViewDropPosition.INTO_OR_AFTER)

    def app_drag_motion(self, widget, context, x, y, time):
        path, before = self.drop_position(widget, x, y)
        valid_section = (not self.search.get_text().strip()
                         and self.section != "@hidden"
                         and (self.section in ("@all", "@favorites") or self.selected_category()
                              and not self.selected_category().hidden))
        if path is not None and valid_section:
            widget.set_drag_dest_row(path, Gtk.TreeViewDropPosition.BEFORE
                                     if before else Gtk.TreeViewDropPosition.AFTER)
            Gdk.drag_status(context, Gdk.DragAction.MOVE, time)
        else:
            widget.set_drag_dest_row(None, Gtk.TreeViewDropPosition.INTO_OR_AFTER)
            Gdk.drag_status(context, Gdk.DragAction(0), time)
        return True

    def app_drag_drop(self, widget, context, x, y, time):
        path, _before = self.drop_position(widget, x, y)
        if path is not None and not self.search.get_text().strip() and self.section != "@hidden":
            target = widget.drag_dest_find_target(context, None)
            if target is not None:
                widget.drag_get_data(context, target, time)
                return True
        Gtk.drag_finish(context, False, False, time)
        return True

    def app_drag_received(self, widget, context, x, y, data, info, time):
        import json
        widget.stop_emission_by_name("drag-data-received")
        path, before = self.drop_position(widget, x, y)
        success = False
        try:
            ids = json.loads(bytes(data.get_data()).decode("utf-8"))
            if (path is None or self.search.get_text().strip() or self.section == "@hidden"
                    or not isinstance(ids, list) or not ids
                    or not all(isinstance(i, str) for i in ids)):
                raise BackendError("Нельзя изменить порядок в этом списке.")
            target_id = self.app_store[path][3]
            self.backend.reorder_apps(self.section, ids, target_id, before)
            self.refresh_view(ids)
            self.show_status("Порядок пунктов изменён. Отмена: Ctrl+Z.")
            success = True
        except (BackendError, GLib.Error, OSError, ValueError, TypeError) as exc:
            self.error_dialog(str(exc))
        Gtk.drag_finish(context, success, False, time)
        widget.set_drag_dest_row(None, Gtk.TreeViewDropPosition.INTO_OR_AFTER)

    def app_popup(self, widget, event):
        if event.button == 1:
            position = widget.get_path_at_pos(int(event.x), int(event.y))
            modifying_selection = event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
            self.drag_candidates = (self.selected_ids() if position and not modifying_selection
                                    and widget.get_selection().path_is_selected(position[0]) else [])
            return False
        if event.button != 3:
            return False
        position = widget.get_path_at_pos(int(event.x), int(event.y))
        if position and not widget.get_selection().path_is_selected(position[0]):
            widget.get_selection().unselect_all()
            widget.get_selection().select_path(position[0])
        if not self.selected_ids():
            return False
        menu = Gtk.Menu()
        for label, action in [("Изменить…", self.edit_app), ("Переместить…", self.move_dialog),
                              (self.favorite_button.get_label(), self.toggle_favorites),
                              (self.hide_button.get_label() + " из меню" if self.hide_button.get_label() == "Убрать" else "Вернуть в меню", self.hide_apps)]:
            item = Gtk.MenuItem.new_with_label(label)
            item.connect("activate", action)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def category_popup(self, widget, event):
        if event.button != 3:
            return False
        position = widget.get_path_at_pos(int(event.x), int(event.y))
        if position:
            widget.get_selection().select_path(position[0])
        category = self.selected_category()
        if not category:
            return False
        menu = Gtk.Menu()
        for label, action in [("Изменить раздел…", self.edit_category),
                              ("Вернуть в меню" if category.hidden else "Убрать раздел", self.remove_category),
                              ("Добавить приложение…", self.new_app)]:
            item = Gtk.MenuItem.new_with_label(label)
            item.connect("activate", action)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_key(self, widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        key = Gdk.keyval_name(event.keyval).lower()
        if key == "f5":
            self.manual_refresh()
            return True
        if ctrl and key == "f":
            self.search.grab_focus()
            self.search.select_region(0, -1)
            return True
        if isinstance(self.get_focus(), (Gtk.Entry, Gtk.TextView)):
            return False
        if ctrl and key == "z":
            if shift and self.backend.can_redo:
                self.change(self.backend.redo, "Изменение повторено.")
            elif not shift and self.backend.can_undo:
                self.change(self.backend.undo, "Изменение отменено.")
            return True
        if ctrl and key == "y" and self.backend.can_redo:
            self.change(self.backend.redo, "Изменение повторено.")
            return True
        if key == "delete":
            self.hide_apps()
            return True
        if key == "f2":
            self.edit_category() if self.get_focus() == self.category_view else self.edit_app()
            return True
        return False

    def help_dialog(self, *_):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.CLOSE, text="Как устроить меню под себя")
        dialog.format_secondary_text(
            "• Перетащите приложение в раздел слева. Можно выделить несколько с Ctrl или Shift.\n\n"
            "• Кнопка «Переместить» делает то же без перетаскивания. Из «Всех приложений» перенос заменяет все текущие разделы; из конкретного — только его.\n\n"
            "• «Раздел» и «Приложение» вверху создают новые пункты. Двойной щелчок открывает название, команду и значок.\n\n"
            "• «Убрать» скрывает ярлык, сохраняя установленную программу. В «Скрытых приложениях» есть кнопка «Вернуть». Скрытые разделы остаются слева и тоже восстанавливаются.\n\n"
            "• Перетащите ярлык в «Избранное» для значков в левой части меню. Стрелки ↑ и ↓ меняют их порядок.\n\n"
            "• Изменения сохраняются сразу. Ctrl+Z — отменить; Ctrl+Shift+Z — повторить. История сохраняется после закрытия редактора.\n\n"
            "• Разделы и приложения сортируются перетаскиванием; для сортировки в самом Cinnamon нужна необязательная надстройка.  Пустой раздел появится в меню после добавления первого приложения.\n\n"
            "Редактор работает локально, от вашего пользователя. Версия " + VERSION)
        dialog.run()
        dialog.destroy()


class MenuApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

    def do_activate(self):
        if self.window is None:
            try:
                self.window = EditorWindow(self)
            except Exception as exc:
                traceback.print_exc()
                dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.CLOSE,
                                           text="Редактор меню не смог открыть меню Cinnamon")
                dialog.format_secondary_text(str(exc) + "\n\nВаше меню не изменено.")
                dialog.run()
                dialog.destroy()
                self.quit()
                return
        self.window.present()


if __name__ == "__main__":
    if os.geteuid() == 0:
        print("Запускайте редактор от своего пользователя, без sudo.", file=sys.stderr)
        raise SystemExit(1)
    app = MenuApplication()
    raise SystemExit(app.run(sys.argv))
