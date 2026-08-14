#!/usr/bin/env python3
"""Last.Pld for Linux - GTK4 desktop front end.

The Windows build is one process: tray icon, window and logger together.
Linux splits it in two, on purpose:

    lastpld.py       the logger. Standard library only, no dependencies,
                     runs headless as lastpld.service. Unchanged by this file.
    lastpld-gui.py   this. Optional. Needs PyGObject (GTK4 + libadwaita) and
                     python3-dbus, both distro packages, never pip.

Nothing here is required for logging to work, which keeps the port's
no-dependency rule intact. The GUI reads the same CSV, watches the same
D-Bus, and drives the logger through systemctl.

    lastpld-gui.py                  open the window
    lastpld-gui.py --background     start in the tray only (used at login)
    lastpld-gui.py --install-autostart
    lastpld-gui.py --uninstall-autostart

The tray is a StatusNotifierItem published straight onto D-Bus. libappindicator
would have been less code but it is GTK3-only and cannot be loaded into a GTK4
process, so the protocol is implemented here directly.
"""

import importlib.util
import os
import subprocess
import sys
import threading
import time
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

import cairo  # noqa: E402

import dbus  # noqa: E402
import dbus.service  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402


APP_ID = "io.github.guardian509.LastPld"
ACCENT = "#fa5a64"          # the same red the Windows build uses
ICON_NAME = "lastpld"
SERVICE = "lastpld.service"


# --------------------------------------------------------------- the logger

def _load_logger_module():
    """Import lastpld.py as a module, from wherever this script lives."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "lastpld.py")
    spec = importlib.util.spec_from_file_location("lastpld", path)
    if spec is None or spec.loader is None:
        sys.exit("cannot find lastpld.py next to this script")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lastpld"] = module
    spec.loader.exec_module(module)
    return module


lp = _load_logger_module()


# ------------------------------------------------------------------- systemd

class Service:
    """The logger runs as a user unit; the GUI only starts and stops it."""

    @staticmethod
    def _run(*args):
        try:
            return subprocess.run(("systemctl", "--user") + args,
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None

    @classmethod
    def installed(cls):
        result = cls._run("cat", SERVICE)
        return bool(result and result.returncode == 0)

    @classmethod
    def active(cls):
        result = cls._run("is-active", SERVICE)
        return bool(result and result.stdout.strip() == "active")

    @classmethod
    def start(cls):
        cls._run("start", SERVICE)

    @classmethod
    def stop(cls):
        cls._run("stop", SERVICE)


# ---------------------------------------------------------------- the store

class History:
    """Reads the CSV, and moves rows between it and the trash file.

    Rows are matched by value rather than by index: the logger appends while
    the window is open, so a row's position is not stable between a read and
    the rewrite that follows it.
    """

    def __init__(self):
        self.store = lp.Store()

    @property
    def path(self):
        return self.store.path

    @property
    def trash_path(self):
        return self.store.trash_path

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return []
        import csv
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            out = []
            for i, row in enumerate(csv.reader(fh)):
                if i == 0 and row and row[0].lower().lstrip("﻿") == "timestamp":
                    continue
                if len(row) >= 5:
                    out.append(row)
            return out

    @staticmethod
    def _write(path, rows):
        import csv
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(lp.CSV_HEADER)
            writer.writerows(rows)
        os.replace(tmp, path)

    @staticmethod
    def _append(path, rows):
        import csv
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            if not exists:
                writer.writerow(lp.CSV_HEADER)
            writer.writerows(rows)

    def rows(self):
        return self._read(self.path)

    def trash_rows(self):
        return self._read(self.trash_path)

    def _move(self, source, target, rows):
        wanted = [tuple(r) for r in rows]
        if not wanted:
            return
        keep, moved = [], []
        pending = list(wanted)
        for row in self._read(source):
            key = tuple(row)
            if key in pending:
                pending.remove(key)
                moved.append(row)
            else:
                keep.append(row)
        if not moved:
            return
        self._append(target, moved)
        self._write(source, keep)

    def to_trash(self, rows):
        self._move(self.path, self.trash_path, rows)

    def restore(self, rows):
        self._move(self.trash_path, self.path, rows)

    def purge(self, rows):
        wanted = [tuple(r) for r in rows]
        keep = [r for r in self._read(self.trash_path) if tuple(r) not in wanted]
        self._write(self.trash_path, keep)

    def empty_trash(self):
        self._write(self.trash_path, [])


# ------------------------------------------------------------------- icon

def _draw_icon(size):
    """The Windows build draws a musical note in the accent red. So does this."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    layout = PangoCairo.create_layout(ctx)
    desc = Pango.FontDescription("Sans Bold %d" % max(8, int(size * 0.72)))
    layout.set_font_description(desc)
    layout.set_text("♫", -1)

    ink, logical = layout.get_pixel_extents()
    width = ink.width or logical.width or size
    height = ink.height or logical.height or size
    x = (size - width) / 2.0 - ink.x
    y = (size - height) / 2.0 - ink.y

    ctx.set_source_rgb(0xfa / 255.0, 0x5a / 255.0, 0x64 / 255.0)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)
    surface.flush()
    return surface


def icon_pixmap(size=32):
    """SNI wants width, height, then ARGB32 in network byte order."""
    surface = _draw_icon(size)
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    out = bytearray(size * size * 4)
    at = 0
    for y in range(size):
        row = data[y * stride:y * stride + size * 4]
        for x in range(0, size * 4, 4):
            # cairo hands back premultiplied BGRA on a little-endian machine
            blue, green, red, alpha = row[x], row[x + 1], row[x + 2], row[x + 3]
            out[at] = alpha
            out[at + 1] = red
            out[at + 2] = green
            out[at + 3] = blue
            at += 4
    return dbus.Struct((dbus.Int32(size), dbus.Int32(size), dbus.ByteArray(bytes(out))),
                       signature="iiay")


def install_icon_theme():
    """Write an SVG into the user icon theme so IconName resolves.

    IconPixmap is supplied too, but hosts that prefer a themed name get a
    crisper icon this way.
    """
    base = os.path.join(GLib.get_user_data_dir(), "icons")
    target = os.path.join(base, "hicolor", "scalable", "apps", ICON_NAME + ".svg")
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
        'viewBox="0 0 48 48">\n'
        '  <text x="24" y="36" text-anchor="middle" font-family="sans-serif" '
        'font-size="38" font-weight="bold" fill="%s">♫</text>\n'
        '</svg>\n' % ACCENT
    )
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(svg)
        index = os.path.join(base, "hicolor", "index.theme")
        if not os.path.exists(index):
            with open(index, "w", encoding="utf-8") as fh:
                fh.write("[Icon Theme]\nName=hicolor\nDirectories=scalable/apps\n\n"
                         "[scalable/apps]\nSize=48\nType=Scalable\nContext=Applications\n")
    except OSError:
        return None
    return base


# --------------------------------------------------------------- dbus menu

DBUSMENU_IFACE = "com.canonical.dbusmenu"
SNI_IFACE = "org.kde.StatusNotifierItem"


class MenuItem:
    _next_id = 1

    def __init__(self, label="", callback=None, kind="standard",
                 toggle=None, enabled=True, children=None):
        self.id = MenuItem._next_id
        MenuItem._next_id += 1
        self.label = label
        self.callback = callback
        self.kind = kind                # "standard" or "separator"
        self.toggle = toggle            # None, or True/False for a checkmark
        self.enabled = enabled
        self.children = children or []

    def properties(self):
        if self.kind == "separator":
            return dbus.Dictionary({"type": dbus.String("separator")}, signature="sv")
        props = {
            "label": dbus.String(self.label),
            "enabled": dbus.Boolean(self.enabled),
            "visible": dbus.Boolean(True),
        }
        if self.toggle is not None:
            props["toggle-type"] = dbus.String("checkmark")
            props["toggle-state"] = dbus.Int32(1 if self.toggle else 0)
        if self.children:
            props["children-display"] = dbus.String("submenu")
        return dbus.Dictionary(props, signature="sv")

    def node(self):
        children = dbus.Array([child.node() for child in self.children], signature="v")
        return dbus.Struct((dbus.Int32(self.id), self.properties(), children),
                           signature="ia{sv}av")

    def walk(self):
        yield self
        for child in self.children:
            for item in child.walk():
                yield item


class DBusMenu(dbus.service.Object):
    """Just enough of com.canonical.dbusmenu for a tray menu."""

    def __init__(self, bus_name, path, build):
        dbus.service.Object.__init__(self, bus_name, path)
        self._build = build
        self._revision = 1
        self._items = build()

    def rebuild(self):
        """Menus are rebuilt before they open, so state is never stale."""
        MenuItem._next_id = 1
        self._items = self._build()
        self._revision += 1
        self.LayoutUpdated(dbus.UInt32(self._revision), dbus.Int32(0))

    def _root(self):
        root = MenuItem()
        root.id = 0
        root.children = self._items
        return root

    def _find(self, wanted):
        for item in self._root().walk():
            if item.id == wanted:
                return item
        return None

    @dbus.service.method(DBUSMENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        item = self._find(parent_id) or self._root()
        return dbus.UInt32(self._revision), item.node()

    @dbus.service.method(DBUSMENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        out = []
        for item in self._root().walk():
            if not ids or item.id in ids:
                out.append(dbus.Struct((dbus.Int32(item.id), item.properties()),
                                       signature="ia{sv}"))
        return dbus.Array(out, signature="(ia{sv})")

    @dbus.service.method(DBUSMENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, item_id, name):
        item = self._find(item_id)
        if item is None:
            return dbus.String("")
        return item.properties().get(name, dbus.String(""))

    @dbus.service.method(DBUSMENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_id, data, timestamp):
        if event_id != "clicked":
            return
        item = self._find(item_id)
        if item is not None and item.callback is not None:
            GLib.idle_add(item.callback)

    @dbus.service.method(DBUSMENU_IFACE, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for item_id, event_id, data, timestamp in events:
            self.Event(item_id, event_id, data, timestamp)
        return dbus.Array([], signature="i")

    @dbus.service.method(DBUSMENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        self.rebuild()
        return True

    @dbus.service.method(DBUSMENU_IFACE, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        self.rebuild()
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    @dbus.service.signal(DBUSMENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    @dbus.service.signal(DBUSMENU_IFACE, signature="a(ia{sv})as")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        return self.GetAll(interface).get(name, dbus.String(""))

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return dbus.Dictionary({
            "Version": dbus.UInt32(3),
            "TextDirection": dbus.String("ltr"),
            "Status": dbus.String("normal"),
            "IconThemePath": dbus.Array([], signature="s"),
        }, signature="sv")


# -------------------------------------------------------------- tray icon

class Tray(dbus.service.Object):
    """A StatusNotifierItem, published directly rather than via libappindicator."""

    def __init__(self, build_menu, on_activate):
        self._on_activate = on_activate
        self._tooltip = "Last.Pld - logging"
        self._theme_path = install_icon_theme()
        self._pixmap = None

        bus = dbus.SessionBus()
        name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        self._bus_name = dbus.service.BusName(name, bus)
        dbus.service.Object.__init__(self, self._bus_name, "/StatusNotifierItem")

        self.menu = DBusMenu(self._bus_name, "/StatusNotifierMenu", build_menu)
        self.registered = self._register(bus, name)

    def _register(self, bus, name):
        try:
            watcher = bus.get_object("org.kde.StatusNotifierWatcher",
                                     "/StatusNotifierWatcher")
            watcher.RegisterStatusNotifierItem(
                name, dbus_interface="org.kde.StatusNotifierWatcher")
            return True
        except dbus.DBusException:
            # No watcher: GNOME without the AppIndicator extension, or a bare
            # session. The window still works; there is simply no tray icon.
            return False

    def set_tooltip(self, text):
        text = text or "Last.Pld - logging"
        if text == self._tooltip:
            return
        self._tooltip = text
        try:
            self.NewToolTip()
        except dbus.DBusException:
            pass

    def _icon_pixmap(self):
        if self._pixmap is None:
            self._pixmap = dbus.Array([icon_pixmap(32)], signature="(iiay)")
        return self._pixmap

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        GLib.idle_add(self._on_activate)

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        GLib.idle_add(self._on_activate)

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        self.menu.rebuild()

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewToolTip(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewTitle(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="s")
    def NewStatus(self, status):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        return self.GetAll(interface).get(name, dbus.String(""))

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        props = {
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String("lastpld"),
            "Title": dbus.String("Last.Pld"),
            "Status": dbus.String("Active"),
            "WindowId": dbus.Int32(0),
            "IconName": dbus.String(ICON_NAME),
            "IconPixmap": self._icon_pixmap(),
            "OverlayIconName": dbus.String(""),
            "AttentionIconName": dbus.String(""),
            "ItemIsMenu": dbus.Boolean(False),
            "Menu": dbus.ObjectPath("/StatusNotifierMenu"),
            "ToolTip": dbus.Struct(
                (dbus.String(ICON_NAME), dbus.Array([], signature="(iiay)"),
                 dbus.String("Last.Pld"), dbus.String(self._tooltip)),
                signature="sa(iiay)ss"),
        }
        if self._theme_path:
            props["IconThemePath"] = dbus.String(self._theme_path)
        return dbus.Dictionary(props, signature="sv")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv", out_signature="")
    def Set(self, interface, name, value):
        pass


# ------------------------------------------------------------ now playing

class NowPlaying(GObject.Object):
    """Polls MPRIS on a worker thread, exactly as the logger does.

    Reading is harmless: the logger owns the writing, and two readers on the
    same bus do not interfere.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        GObject.Object.__init__(self)
        self._bus = lp.Bus()
        self._stop = threading.Event()
        self._current = None
        self._identities = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _identity(self, service):
        """Players publish a display name; "Spotify" beats "spotify"."""
        if service not in self._identities:
            name = ""
            try:
                value = self._bus.get_all(service, lp.ROOT_IFACE).get("Identity")
                if isinstance(value, str):
                    name = value
            except lp.DBusError:
                pass
            self._identities[service] = name
        return self._identities[service]

    def _snapshot(self):
        if not self._bus.available:      # a property, not a method
            return None
        for service in self._bus.players():
            try:
                props = self._bus.get_all(service, lp.PLAYER_IFACE)
            except lp.DBusError:
                continue
            if props.get("PlaybackStatus") != "Playing":
                continue
            # parse_metadata hands back a plain tuple, not a Track.
            title, artist, album, url = lp.parse_metadata(props.get("Metadata") or {})
            if not title:
                continue
            return lp.Track(title, artist, album,
                            lp.friendly_app(service, self._identity(service)),
                            url, service)
        return None

    def _loop(self):
        complained = False
        while not self._stop.wait(1.0):
            try:
                track = self._snapshot()
            except lp.DBusError:
                track = None                    # a player vanished mid-read
            except Exception:
                # Anything else is a bug in here. Say so once - swallowing it
                # silently just looks like "nothing is ever playing".
                track = None
                if not complained:
                    complained = True
                    import traceback
                    traceback.print_exc()
            key = None if track is None else (track.title, track.artist, track.app)
            if key != self._current:
                self._current = key
                GLib.idle_add(self.emit, "changed", track)


# ---------------------------------------------------------------- the rows

class Row(GObject.Object):
    __gtype_name__ = "LastPldRow"

    def __init__(self, values):
        GObject.Object.__init__(self)
        self.values = list(values)

    when = property(lambda self: self.values[0])
    title = property(lambda self: self.values[1])
    artist = property(lambda self: self.values[2])
    album = property(lambda self: self.values[3])
    source = property(lambda self: self.values[4])


# -------------------------------------------------------------- the window

CSS = """
.nowplaying {
  padding: 14px 18px;
  border-bottom: 1px solid alpha(currentColor, 0.12);
}
.nowplaying-title {
  font-size: 15pt;
  font-weight: bold;
  color: %s;
}
.nowplaying-sub { opacity: 0.7; }
.dim { opacity: 0.6; }
""" % ACCENT


class Window(Adw.ApplicationWindow):
    def __init__(self, app, history, nowplaying):
        Adw.ApplicationWindow.__init__(self, application=app)
        self.history = history
        self.nowplaying = nowplaying
        self.viewing_trash = False
        self._identifying = False

        self.set_title("Last.Pld")
        self.set_default_size(1120, 640)
        self.set_icon_name(ICON_NAME)

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._match)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.selection = Gtk.MultiSelection(model=filtered)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._header())
        toolbar.set_content(self._content())
        self.set_content(toolbar)

        self._wire_keys()
        self.connect("close-request", self._on_close)

        nowplaying.connect("changed", self._on_now_playing)
        self._watch_files()
        self.reload()

    # ---- chrome

    def _header(self):
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle.new("Last.Pld", "play history")
        header.set_title_widget(self.window_title)

        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self.search_button.set_tooltip_text("Search (Ctrl+F)")
        self.search_button.connect("toggled", self._on_search_toggled)
        header.pack_start(self.search_button)

        self.trash_button = Gtk.ToggleButton(icon_name="user-trash-symbolic")
        self.trash_button.set_tooltip_text("Trash")
        self.trash_button.connect("toggled", self._on_trash_toggled)
        header.pack_start(self.trash_button)

        self.identify_button = Gtk.Button(label="Identify")
        self.identify_button.add_css_class("suggested-action")
        self.identify_button.set_tooltip_text("Fingerprint what is playing right now")
        self.identify_button.connect("clicked", lambda *_: self.identify_now())
        header.pack_end(self.identify_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Open CSV", "win.open-csv")
        section.append("Open folder", "win.open-folder")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Logging", "win.toggle-logging")
        section.append("Edit sources…", "win.edit-sources")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Connect Spotify…", "win.spotify")
        section.append("Why not Apple Music?", "win.apple")
        menu.append_section(None, section)

        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(button)
        self._install_actions()
        return header

    def _content(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        banner.add_css_class("nowplaying")
        self.now_title = Gtk.Label(xalign=0, label="Nothing playing")
        self.now_title.add_css_class("nowplaying-title")
        self.now_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.now_sub = Gtk.Label(xalign=0, label="waiting for a player")
        self.now_sub.add_css_class("nowplaying-sub")
        self.now_sub.set_ellipsize(Pango.EllipsizeMode.END)
        banner.append(self.now_title)
        banner.append(self.now_sub)
        box.append(banner)

        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search title, artist, album")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", lambda *_: self._refilter())
        bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar_box.append(self.search_entry)
        self.source_dropdown = Gtk.DropDown.new_from_strings(["All sources"])
        self.source_dropdown.connect("notify::selected", lambda *_: self._refilter())
        bar_box.append(self.source_dropdown)
        self.search_bar.set_child(bar_box)
        self.search_bar.connect_entry(self.search_entry)
        box.append(self.search_bar)

        self.column_view = Gtk.ColumnView(model=self.selection)
        self.column_view.set_vexpand(True)
        for title, getter, expand in (
            ("When", lambda r: r.when, False),
            ("Title", lambda r: r.title, True),
            ("Artist", lambda r: r.artist, True),
            ("Album", lambda r: r.album, True),
            ("Source", lambda r: r.source, False),
        ):
            self.column_view.append_column(self._column(title, getter, expand))
        self.column_view.connect("activate", self._on_activate_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.column_view)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.status = Gtk.Label(xalign=0, label="")
        self.status.add_css_class("dim")
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(6)
        self.status.set_margin_start(14)
        box.append(self.status)

        self._attach_row_menu()
        return box

    @staticmethod
    def _column(title, getter, expand):
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item):
            label = Gtk.Label(xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_child(label)

        def bind(_factory, item):
            item.get_child().set_text(getter(item.get_item()) or "")

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        column.set_resizable(True)
        return column

    # ---- actions

    def _install_actions(self):
        for name, handler in (
            ("open-csv", lambda *_: self._open(self.history.path)),
            ("open-folder", lambda *_: self._open(os.path.dirname(self.history.path))),
            ("toggle-logging", lambda *_: self.toggle_logging()),
            ("edit-sources", lambda *_: self._open(lp.path_in_data("sources.txt"))),
            ("spotify", lambda *_: self.spotify_setup()),
            ("apple", lambda *_: self._apple_music_note()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _wire_keys(self):
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _on_key(self, _controller, keyval, _code, state):
        from gi.repository import Gdk
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_button.set_active(True)
            self.search_entry.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape:
            # Esc clears a search if there is one, otherwise drops to the tray -
            # logging carries on either way. Same as the Windows build.
            if self.search_entry.get_text():
                self.search_entry.set_text("")
            elif self.search_button.get_active():
                self.search_button.set_active(False)
            else:
                self.set_visible(False)
            return True
        if keyval == Gdk.KEY_Delete:
            if self.viewing_trash:
                self.purge_selected()
            else:
                self.trash_selected()
            return True
        return False

    def _attach_row_menu(self):
        self.row_menu = Gtk.PopoverMenu()
        self.row_menu.set_parent(self.column_view)
        self.row_menu.set_has_arrow(False)

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("pressed", self._on_right_click)
        self.column_view.add_controller(gesture)

        for name, handler in (
            ("copy-row", lambda *_: self.copy_selected()),
            ("search-apple", lambda *_: self.search_apple_music()),
            ("trash-row", lambda *_: self.trash_selected()),
            ("restore-row", lambda *_: self.restore_selected()),
            ("purge-row", lambda *_: self.purge_selected()),
            ("empty-trash", lambda *_: self.empty_trash()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _on_right_click(self, gesture, _n, x, y):
        menu = Gio.Menu()
        if self.viewing_trash:
            menu.append("Restore", "win.restore-row")
            section = Gio.Menu()
            section.append("Delete permanently  (Del)", "win.purge-row")
            section.append("Empty trash", "win.empty-trash")
            menu.append_section(None, section)
        else:
            menu.append('Copy "Artist — Title"', "win.copy-row")
            menu.append("Search on Apple Music", "win.search-apple")
            section = Gio.Menu()
            section.append("Move to trash  (Del)", "win.trash-row")
            menu.append_section(None, section)
        self.row_menu.set_menu_model(menu)
        self.row_menu.set_pointing_to(Gdk_rect(x, y))
        self.row_menu.popup()

    # ---- data

    def _watch_files(self):
        """Reload when the logger appends, so the list is always live."""
        self._monitors = []
        for path in (self.history.path, self.history.trash_path):
            try:
                monitor = Gio.File.new_for_path(path).monitor_file(
                    Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect("changed", self._on_file_changed)
            self._monitors.append(monitor)

    def _on_file_changed(self, *_args):
        if getattr(self, "_reload_pending", False):
            return
        self._reload_pending = True

        def go():
            self._reload_pending = False
            self.reload()
            return False

        # The logger writes a row at a time; coalesce bursts.
        GLib.timeout_add(400, go)

    def reload(self):
        rows = self.history.trash_rows() if self.viewing_trash else self.history.rows()
        rows = list(reversed(rows))          # newest first
        self.store.remove_all()
        for row in rows:
            self.store.append(Row(row))
        self._rebuild_sources(rows)
        self._refilter()

    def _rebuild_sources(self, rows):
        seen = []
        for row in rows:
            if row[4] and row[4] not in seen:
                seen.append(row[4])
        seen.sort(key=str.lower)
        current = self._selected_source()
        model = Gtk.StringList.new(["All sources"] + seen)
        self.source_dropdown.set_model(model)
        if current and current in seen:
            self.source_dropdown.set_selected(seen.index(current) + 1)
        else:
            self.source_dropdown.set_selected(0)

    def _selected_source(self):
        model = self.source_dropdown.get_model()
        index = self.source_dropdown.get_selected()
        if model is None or index == Gtk.INVALID_LIST_POSITION or index == 0:
            return None
        return model.get_string(index)

    def _match(self, row, *_):
        source = self._selected_source()
        if source and row.source != source:
            return False
        needle = self.search_entry.get_text().strip().lower()
        if not needle:
            return True
        return any(needle in (value or "").lower()
                   for value in (row.title, row.artist, row.album, row.source))

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        shown = self.selection.get_n_items()
        total = self.store.get_n_items()
        where = "trash" if self.viewing_trash else "plays"
        if shown == total:
            self.status.set_text("%d %s" % (total, where))
        else:
            self.status.set_text("%d of %d %s" % (shown, total, where))

    def selected_rows(self):
        out = []
        for i in range(self.selection.get_n_items()):
            if self.selection.is_selected(i):
                out.append(self.selection.get_item(i).values)
        return out

    # ---- row commands

    def copy_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        lines = []
        for row in rows:
            lines.append("%s — %s" % (row[2], row[1]) if row[2] else row[1])
        self.get_clipboard().set("\n".join(lines))
        self._toast("Copied")

    def search_apple_music(self):
        rows = self.selected_rows()
        if not rows:
            return
        row = rows[0]
        term = urllib.parse.quote((row[2] + " " + row[1]).strip())
        self._open("https://music.apple.com/us/search?term=" + term)

    def trash_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        self.history.to_trash(rows)
        self.reload()
        self._toast("Moved %d to trash" % len(rows))

    def restore_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        self.history.restore(rows)
        self.reload()
        self._toast("Restored %d" % len(rows))

    def purge_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        self._confirm("Delete permanently?",
                      "%d row(s) will be gone for good." % len(rows),
                      lambda: (self.history.purge(rows), self.reload()))

    def empty_trash(self):
        self._confirm("Empty the trash?",
                      "Everything in the trash will be gone for good.",
                      lambda: (self.history.empty_trash(), self.reload()))

    def _on_activate_row(self, _view, _position):
        if self.viewing_trash:
            self.restore_selected()
        else:
            self.search_apple_music()

    # ---- toggles

    def _on_search_toggled(self, button):
        self.search_bar.set_search_mode(button.get_active())
        if button.get_active():
            self.search_entry.grab_focus()

    def _on_trash_toggled(self, button):
        self.viewing_trash = button.get_active()
        self.window_title.set_subtitle("trash" if self.viewing_trash else "play history")
        self.reload()

    def toggle_logging(self):
        if Service.active():
            Service.stop()
            self._toast("Logging stopped")
        else:
            Service.start()
            self._toast("Logging started")

    # ---- identify

    def identify_now(self):
        if self._identifying:
            return
        self.present()
        self._identifying = True
        self.identify_button.set_label("Listening…")
        self.identify_button.set_sensitive(False)

        def work():
            try:
                result = lp.Identify.run()
            except Exception as exc:
                result = {"matched": False, "error": str(exc)}
            GLib.idle_add(done, result)

        def done(result):
            self._identifying = False
            self.identify_button.set_label("Identify")
            self.identify_button.set_sensitive(True)
            if not result.get("matched"):
                self._alert("No match",
                            result.get("error", "nothing recognised"))
                return False
            track = lp.Track(result["title"], result.get("artist", ""),
                             result.get("album", ""), "Shazam")
            self.history.store.append(track)
            self.reload()
            self._alert("%s" % result["title"],
                        "\n".join(x for x in (result.get("artist", ""),
                                              result.get("album", ""),
                                              "Added to your history.") if x))
            return False

        threading.Thread(target=work, daemon=True).start()

    # ---- spotify

    def spotify_setup(self):
        if lp.Spotify.connected():
            self._confirm("Disconnect Spotify?",
                          "Nothing further will be added to your playlist.",
                          lambda: (lp.Spotify.disconnect(), self._toast("Disconnected")))
            return
        self._alert(
            "Connect Spotify",
            "Spotify needs a free developer app, and the connect flow opens a "
            "browser and waits on a local redirect. Run it from a terminal:\n\n"
            "    ./lastpld.py --connect-spotify\n\n"
            "Once it is connected this menu will offer to disconnect instead.")

    def _apple_music_note(self):
        self._alert(
            "Why not Apple Music?",
            "Apple Music has no playlist API a desktop app can use without a "
            "paid Apple Developer account and a MusicKit token, and the token "
            "cannot be issued from the app itself.\n\n"
            "Capturing the plays is the part Apple does not do for you, and "
            "that works: radio tracks land in the CSV either way.")

    # ---- small helpers

    def _toast(self, text):
        self.status.set_text(text)
        GLib.timeout_add_seconds(3, lambda: (self._refilter(), False)[1])

    def _dialog(self, heading, body):
        """libadwaita renamed MessageDialog to AlertDialog; support both."""
        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog(heading=heading, body=body)
            return dialog, lambda: dialog.present(self)
        dialog = Adw.MessageDialog(heading=heading, body=body,
                                   transient_for=self, modal=True)
        return dialog, dialog.present

    def _alert(self, heading, body):
        dialog, show = self._dialog(heading, body)
        dialog.add_response("ok", "OK")
        show()

    def _confirm(self, heading, body, on_yes):
        dialog, show = self._dialog(heading, body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", "Delete")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response):
            if response == "go":
                on_yes()

        dialog.connect("response", responded)
        show()

    def _open(self, target):
        if not target:
            return
        if not target.startswith("http"):
            target = Gio.File.new_for_path(target).get_uri()
        Gtk.UriLauncher.new(target).launch(self, None, None, None)

    def _on_now_playing(self, _source, track):
        if track is None:
            self.now_title.set_text("Nothing playing")
            self.now_sub.set_text("waiting for a player")
            return
        self.now_title.set_text(track.title)
        detail = " · ".join(x for x in (track.artist, track.album) if x)
        self.now_sub.set_text("%s — %s" % (detail, track.app) if detail else track.app)

    def _on_close(self, *_args):
        # Closing hides to the tray so logging continues; Quit comes from the
        # tray menu. The Windows build behaves the same way.
        self.set_visible(False)
        return True


def Gdk_rect(x, y):
    from gi.repository import Gdk
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect


# --------------------------------------------------------------- autostart

AUTOSTART = """\
[Desktop Entry]
Type=Application
Name=Last.Pld
Comment=Play history recorder
Exec=%s --background
Icon=%s
Terminal=false
Categories=AudioVideo;Audio;
X-GNOME-Autostart-enabled=true
"""


def autostart_path():
    return os.path.join(GLib.get_user_config_dir(), "autostart", "lastpld-gui.desktop")


def desktop_entry_path():
    return os.path.join(GLib.get_user_data_dir(), "applications", "lastpld-gui.desktop")


def install_autostart():
    script = os.path.abspath(__file__)
    body = AUTOSTART % (script, ICON_NAME)
    for path in (autostart_path(), desktop_entry_path()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    install_icon_theme()
    print("installed:\n  %s\n  %s" % (autostart_path(), desktop_entry_path()))
    print("the tray app will start at your next login.")
    return 0


def uninstall_autostart():
    for path in (autostart_path(), desktop_entry_path()):
        try:
            os.remove(path)
            print("removed %s" % path)
        except OSError:
            pass
    return 0


# -------------------------------------------------------------------- app

class Application(Adw.Application):
    def __init__(self, background):
        Adw.Application.__init__(self, application_id=APP_ID,
                                 flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background = background
        self.window = None
        self.tray = None
        self.nowplaying = None
        self.history = None
        self._quitting = False

    def do_startup(self):
        Adw.Application.do_startup(self)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        from gi.repository import Gdk
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.history = History()
        self.nowplaying = NowPlaying()
        self.nowplaying.connect("changed", self._tray_tooltip)

        self.window = Window(self, self.history, self.nowplaying)
        self.tray = Tray(self._build_tray_menu, self.surface_window)

        # Without a window the app would exit as soon as the last one closes;
        # the tray needs the process to stay alive.
        self.hold()

    def do_activate(self):
        if self.background:
            self.background = False      # only the very first launch is silent
            if not self.tray.registered:
                # No tray to hide in, so showing the window is the only sane
                # outcome - otherwise the app would be invisible and unusable.
                self.surface_window()
            return
        self.surface_window()

    def surface_window(self):
        self.window.set_visible(True)
        self.window.present()
        return False

    def _tray_tooltip(self, _source, track):
        if self.tray is None:
            return
        if track is None:
            self.tray.set_tooltip("Last.Pld - logging")
        else:
            text = "%s - %s" % (track.title, track.artist) if track.artist else track.title
            if len(text) > 62:
                text = text[:59] + "..."
            self.tray.set_tooltip(text)

    def _build_tray_menu(self):
        logging_on = Service.active()
        items = [
            MenuItem("Open history", self.surface_window),
            MenuItem("Identify song now", self._tray_identify),
            MenuItem(kind="separator"),
            MenuItem("Logging", self._tray_toggle_logging,
                     toggle=logging_on, enabled=Service.installed()),
            MenuItem("Open CSV folder", self._tray_open_folder),
            MenuItem(kind="separator"),
            MenuItem("Quit Last.Pld", self._tray_quit),
        ]
        return items

    def _tray_identify(self):
        self.window.identify_now()
        return False

    def _tray_toggle_logging(self):
        self.window.toggle_logging()
        return False

    def _tray_open_folder(self):
        self.window._open(os.path.dirname(self.history.path))
        return False

    def _tray_quit(self):
        # Quitting closes the window and the tray. It deliberately does NOT
        # stop lastpld.service - logging is meant to outlive the GUI.
        self._quitting = True
        if self.nowplaying:
            self.nowplaying.stop()
        self.release()
        self.quit()
        return False


def main(argv):
    if "--install-autostart" in argv:
        return install_autostart()
    if "--uninstall-autostart" in argv:
        return uninstall_autostart()

    DBusGMainLoop(set_as_default=True)
    background = "--background" in argv
    app = Application(background)
    return app.run([argv[0]])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
