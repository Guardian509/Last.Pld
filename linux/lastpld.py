#!/usr/bin/env python3
"""Last.Pld for Linux - a local play-history recorder.

The Windows build reads the System Media Transport Controls. Linux has the same
idea in MPRIS, published over D-Bus by essentially every media player and
browser, so this is a port of the concept rather than of the code.

One file. Download it, make it executable, run it once: it opens a window, puts
a note in your tray, and sets itself to start when you log in. Delete the file
and it is gone - the autostart entry notices and removes itself at the next
login. Your history stays where it is, because it is yours.

Deliberately depends on nothing you have to install. The recorder is standard
library only: no pip, no venv, no third-party modules. D-Bus is reached through
`busctl` (ships with systemd) or `gdbus` (ships with glib) - whichever is
present. If you have a Linux desktop playing audio, you already have one.

Optional extras light up only if the tools happen to exist, and their absence
never stops the recorder:

    Window     PyGObject (GTK4 + libadwaita), a distro package. Without it the
               file still records, it just has no face - see --no-gui.
    Tray       python3-dbus, published as a StatusNotifierItem.
    Identify   songrec, or shazamio on the python path, plus a recorder
               (pw-record / parec / ffmpeg)
    Playlists  nothing - the Spotify client here is stdlib urllib

Usage:
    lastpld.py                    open the window and start recording
    lastpld.py --background       start in the tray only (what login runs)
    lastpld.py --no-gui           record in the terminal (Ctrl-C to stop)
    lastpld.py --probe            show what MPRIS players are visible, and why
                                  each one would or would not be logged
    lastpld.py --history 20       print the last 20 tracks
    lastpld.py --search whitney   search the history
    lastpld.py --identify         fingerprint what is playing right now
    lastpld.py --connect-spotify  authorise pushing identified songs to Spotify
    lastpld.py --install-autostart / --uninstall-autostart
    lastpld.py --install-service  a systemd user service, for headless boxes
    lastpld.py --selftest         run the built-in tests
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "1.0"

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
ROOT_IFACE = "org.mpris.MediaPlayer2"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

CSV_HEADER = ["timestamp", "title", "artist", "album", "app"]

# Same window the Windows build uses: never record the same track from the same
# player twice inside this many seconds, however the sessions churn underneath.
DEDUP_SECONDS = 90

POLL_SECONDS = 1.0


# --------------------------------------------------------------------- paths

def data_dir():
    """Where history and settings live. XDG, with an env override."""
    override = os.environ.get("LASTPLD_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "lastpld")


def path_in_data(name):
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


# ----------------------------------------------------------------- d-bus i/o

class DBusError(Exception):
    pass


def _run(argv, timeout=10):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DBusError("%s is not installed" % argv[0])
    except subprocess.TimeoutExpired:
        raise DBusError("%s timed out" % argv[0])
    if p.returncode != 0:
        raise DBusError((p.stderr or p.stdout or "").strip() or
                        "%s exited %d" % (argv[0], p.returncode))
    return p.stdout


def _unwrap_busctl(node):
    """busctl --json wraps every value as {"type": ..., "data": ...}.

    Shapes vary between systemd versions, so rather than hard-coding one layout
    this strips the wrapper wherever it appears and leaves plain Python behind.
    """
    if isinstance(node, dict):
        if set(node.keys()) == {"type", "data"}:
            return _unwrap_busctl(node["data"])
        return {k: _unwrap_busctl(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_unwrap_busctl(v) for v in node]
    return node


def _gvariant_scan(text):
    """Very small GVariant-text reader, enough for gdbus property replies.

    gdbus prints things like ({'PlaybackStatus': <'Playing'>, 'Metadata':
    <{'xesam:title': <'Song'>, 'xesam:artist': <['A']>}>},). A full parser is
    not warranted - this pulls out the key/value structure and nothing else.
    """
    i = 0
    n = len(text)

    def skip_ws():
        nonlocal i
        while i < n and text[i] in " \t\r\n":
            i += 1

    def parse():
        nonlocal i
        skip_ws()
        if i >= n:
            return None
        c = text[i]
        if c == "<":                       # variant wrapper
            i += 1
            v = parse()
            skip_ws()
            if i < n and text[i] == ">":
                i += 1
            return v
        if c in "([":                      # tuple or array
            close = ")" if c == "(" else "]"
            i += 1
            items = []
            while i < n:
                skip_ws()
                if i < n and text[i] == close:
                    i += 1
                    break
                items.append(parse())
                skip_ws()
                if i < n and text[i] == ",":
                    i += 1
            return items
        if c == "{":                       # dict
            i += 1
            out = {}
            while i < n:
                skip_ws()
                if i < n and text[i] == "}":
                    i += 1
                    break
                k = parse()
                skip_ws()
                if i < n and text[i] == ":":
                    i += 1
                v = parse()
                out[k] = v
                skip_ws()
                if i < n and text[i] == ",":
                    i += 1
            return out
        if c in "'\"":                     # string
            quote = c
            i += 1
            buf = []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
            return "".join(buf)
        j = i                              # bare token: number, true, uint64 5
        while j < n and text[j] not in ",)]}>: \t\r\n":
            j += 1
        tok = text[i:j]
        i = j
        if tok in ("true", "false"):
            return tok == "true"
        try:
            return int(tok)
        except ValueError:
            try:
                return float(tok)
            except ValueError:
                return tok

    return parse()


class Bus:
    """Talks to the session bus through whichever CLI tool exists."""

    def __init__(self):
        self.busctl = shutil.which("busctl")
        self.gdbus = shutil.which("gdbus")
        self.dbus_send = shutil.which("dbus-send")

    @property
    def available(self):
        return bool(self.busctl or self.gdbus or self.dbus_send)

    def how(self):
        if self.busctl:
            return "busctl"
        if self.gdbus:
            return "gdbus"
        if self.dbus_send:
            return "dbus-send"
        return "none"

    # ---- names

    def list_names(self):
        if self.busctl:
            out = _run([self.busctl, "--user", "--json=short", "call",
                        "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "ListNames"])
            data = _unwrap_busctl(json.loads(out))
            names = _flatten_strings(data)
            return sorted(set(names))
        if self.gdbus:
            out = _run([self.gdbus, "call", "--session",
                        "--dest", "org.freedesktop.DBus",
                        "--object-path", "/org/freedesktop/DBus",
                        "--method", "org.freedesktop.DBus.ListNames"])
            return sorted(set(_flatten_strings(_gvariant_scan(out))))
        if self.dbus_send:
            out = _run([self.dbus_send, "--session", "--print-reply",
                        "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus.ListNames"])
            return sorted(set(re.findall(r'string\s+"([^"]+)"', out)))
        raise DBusError("no D-Bus command line tool found "
                        "(need one of busctl, gdbus, dbus-send)")

    def players(self):
        return [n for n in self.list_names() if n.startswith(MPRIS_PREFIX)]

    # ---- properties

    def get_all(self, service, interface):
        if self.busctl:
            out = _run([self.busctl, "--user", "--json=short", "call",
                        service, OBJECT_PATH,
                        "org.freedesktop.DBus.Properties", "GetAll", "s",
                        interface])
            data = _unwrap_busctl(json.loads(out))
            return _first_dict(data) or {}
        if self.gdbus:
            out = _run([self.gdbus, "call", "--session", "--dest", service,
                        "--object-path", OBJECT_PATH,
                        "--method", "org.freedesktop.DBus.Properties.GetAll",
                        interface])
            return _first_dict(_gvariant_scan(out)) or {}
        raise DBusError("reading properties needs busctl or gdbus")


def _flatten_strings(node):
    out = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_flatten_strings(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_flatten_strings(v))
    return out


def _first_dict(node):
    """Property replies arrive wrapped in a tuple/array of one dict."""
    if isinstance(node, dict):
        return node
    if isinstance(node, list):
        for item in node:
            found = _first_dict(item)
            if found is not None:
                return found
    return None


# ------------------------------------------------------------------- tracks

class Track:
    __slots__ = ("title", "artist", "album", "app", "url", "service")

    def __init__(self, title="", artist="", album="", app="", url="", service=""):
        self.title = title or ""
        self.artist = artist or ""
        self.album = album or ""
        self.app = app or ""
        self.url = url or ""
        self.service = service or ""

    def __repr__(self):
        return "Track(%r, %r, %r, app=%r)" % (self.title, self.artist,
                                              self.album, self.app)


def friendly_app(service, identity=""):
    """org.mpris.MediaPlayer2.chromium.instance42 -> chromium."""
    if identity:
        return identity
    name = service[len(MPRIS_PREFIX):] if service.startswith(MPRIS_PREFIX) else service
    name = re.sub(r"\.instance\d+$", "", name)
    return name or service


def parse_metadata(meta):
    """MPRIS metadata is a{sv}; artist is a list, everything else a scalar."""
    if not isinstance(meta, dict):
        return "", "", "", ""

    def one(value):
        if isinstance(value, list):
            value = value[0] if value else ""
        return "" if value is None else str(value)

    title = one(meta.get("xesam:title"))
    album = one(meta.get("xesam:album"))
    url = one(meta.get("xesam:url"))

    artists = meta.get("xesam:artist")
    if isinstance(artists, list):
        artist = ", ".join(str(a) for a in artists if a)
    else:
        artist = one(artists)
    if not artist:
        artist = one(meta.get("xesam:albumArtist"))

    return title, artist, album, url


# -------------------------------------------------------------------- filter

BROWSERS = ("chrome", "chromium", "firefox", "brave", "vivaldi", "opera",
            "epiphany", "midori", "librewolf", "waterfox")

DEFAULT_SOURCES = """\
# Last.Pld source filter - edit and restart to apply.
#
#   allow <text>   log any player whose bus name contains <text>
#   youtube        also log browser audio when the page URL is YouTube
#   no-youtube     turn that off

allow spotify
allow vlc
allow rhythmbox
allow clementine
allow strawberry
allow audacious
allow mpd
allow mpv
allow amarok
allow elisa
allow lollypop
allow quodlibet
allow tidal
allow deezer
allow youtubemusic

youtube
"""


class Filter:
    """Which players are worth recording.

    Browsers publish a media session for any page with audio, so on Windows
    Facebook and Airbnb turned up in the history as songs. Same problem here,
    but MPRIS hands us a better answer than Windows did: xesam:url carries the
    actual page address, so YouTube can be recognised by its domain instead of
    by scraping window titles. That also fixes the Windows limitation where a
    background tab was never logged, and sidesteps Wayland, which forbids
    reading other applications' window titles at all.
    """

    def __init__(self, allow=None, youtube=True):
        self.allow = list(allow or [])
        self.youtube = youtube

    @classmethod
    def load(cls, path=None):
        path = path or path_in_data("sources.txt")
        if not os.path.exists(path):
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(DEFAULT_SOURCES)
            except OSError:
                pass
        allow, youtube = [], True
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    low = line.lower()
                    if low == "youtube":
                        youtube = True
                    elif low == "no-youtube":
                        youtube = False
                    elif low.startswith("allow "):
                        value = line[6:].strip().lower()
                        if value:
                            allow.append(value)
        except OSError:
            pass
        if not allow:
            allow = ["spotify", "vlc", "rhythmbox", "mpv"]
        return cls(allow, youtube)

    @staticmethod
    def is_browser(name):
        low = name.lower()
        return any(b in low for b in BROWSERS)

    @staticmethod
    def is_youtube_url(url):
        if not url:
            return False
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except ValueError:
            return False
        host = host.split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host.startswith("m."):
            host = host[2:]
        # Match the domain itself, never a lookalike like youtube.com.evil.tld
        return host in ("youtube.com", "youtu.be", "music.youtube.com")

    def decision(self, service, url):
        """Returns (should_log, human readable reason)."""
        low = service.lower()
        for token in self.allow:
            if token in low:
                return True, "allowed by 'allow %s'" % token
        if self.is_browser(low):
            if not self.youtube:
                return False, "browser, and youtube exception is off"
            if self.is_youtube_url(url):
                return True, "browser playing a YouTube URL"
            if not url:
                return False, "browser, but it publishes no xesam:url"
            return False, "browser playing a non-YouTube URL"
        return False, "not in the allow list"

    def should_log(self, service, url):
        return self.decision(service, url)[0]


# -------------------------------------------------------------------- store

class Store:
    """The same CSV the Windows build writes, so histories interchange."""

    def __init__(self, path=None, trash_path=None):
        self.path = path or path_in_data("lastpld.csv")
        self.trash_path = trash_path or path_in_data("lastpld.trash.csv")
        self._ensure()

    def _ensure(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # utf-8-sig: the Windows build writes a BOM, and matching it means
            # the two files can be concatenated or swapped without surprises.
            with open(self.path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow(CSV_HEADER)

    def append(self, track, when=None):
        when = when or datetime.now()
        row = [when.strftime("%Y-%m-%d %H:%M:%S"),
               track.title, track.artist, track.album, track.app]
        with open(self.path, "a", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh).writerow(row)
        return row

    def rows(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            out = []
            for i, row in enumerate(reader):
                if i == 0 and row and row[0].lower().lstrip("﻿") == "timestamp":
                    continue
                if len(row) >= 5:
                    out.append(row)
            return out


# ------------------------------------------------------------------ logger

class Logger:
    def __init__(self, store, filt, bus, verbose=True):
        self.store = store
        self.filter = filt
        self.bus = bus
        self.verbose = verbose
        self._last_per_app = {}
        self._recent = {}
        self._identity_cache = {}
        self._seed()

    def _seed(self):
        """Don't re-log the track that was already playing when we started.

        Seeding _recent alone did not do it: the stamp was DEDUP_SECONDS old
        the moment it was written, so the "seen recently" test was already
        false and every restart re-logged whatever was playing. What actually
        answers the question is _last_per_app - if the newest row for an app
        is still the track it is playing, we have that play already.
        """
        now = time.time()
        for row in self.store.rows()[-200:]:
            self._recent[(row[4], row[1], row[2])] = now
            self._last_per_app[row[4]] = (row[1], row[2])

    def identity(self, service):
        if service not in self._identity_cache:
            name = ""
            try:
                props = self.bus.get_all(service, ROOT_IFACE)
                value = props.get("Identity")
                if isinstance(value, str):
                    name = value
            except DBusError:
                pass
            self._identity_cache[service] = name
        return self._identity_cache[service]

    def snapshot(self):
        """Every player currently reporting Playing."""
        found = []
        try:
            services = self.bus.players()
        except DBusError:
            return found
        for service in services:
            try:
                props = self.bus.get_all(service, PLAYER_IFACE)
            except DBusError:
                continue
            if str(props.get("PlaybackStatus", "")) != "Playing":
                continue
            title, artist, album, url = parse_metadata(props.get("Metadata"))
            if not title and not artist:
                continue
            found.append(Track(title, artist, album,
                               friendly_app(service, self.identity(service)),
                               url, service))
        return found

    def tick(self):
        logged = []
        for track in self.snapshot():
            if not self.filter.should_log(track.service, track.url):
                continue

            key = (track.app, track.title, track.artist)
            if self._last_per_app.get(track.app) == (track.title, track.artist):
                continue
            self._last_per_app[track.app] = (track.title, track.artist)

            now = time.time()
            seen = self._recent.get(key)
            if seen is not None and now - seen < DEDUP_SECONDS:
                continue
            self._recent[key] = now

            self.store.append(track)
            logged.append(track)
            if self.verbose:
                line = track.title
                if track.artist:
                    line += "  -  " + track.artist
                print("[%s] %s   (%s)" % (datetime.now().strftime("%H:%M:%S"),
                                          line, track.app), flush=True)

            push_note = Playlists.add_everywhere(track)
            if push_note and self.verbose:
                print("    %s" % push_note, flush=True)
        return logged

    def run(self):
        if self.verbose:
            print("Last.Pld %s - logging to %s" % (VERSION, self.store.path))
            print("D-Bus via %s. Ctrl-C to stop.\n" % self.bus.how(), flush=True)
        try:
            while True:
                try:
                    self.tick()
                except DBusError as exc:
                    if self.verbose:
                        print("  (d-bus hiccup: %s)" % exc, flush=True)
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            if self.verbose:
                print("\nstopped.")


# ----------------------------------------------------------------- settings

class Settings:
    """key=value beside the history. Chmod 0600: it holds a Spotify token.

    The Windows build encrypts this with DPAPI. Linux has no equivalent that is
    dependency-free - libsecret would mean a third-party module - so the file is
    owner-only instead, which is what every other CLI tool on the platform does.
    """

    _cache = None

    @classmethod
    def path(cls):
        return path_in_data("playlists.txt")

    @classmethod
    def _load(cls):
        if cls._cache is not None:
            return cls._cache
        cls._cache = {}
        try:
            with open(cls.path(), "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cls._cache[k.strip()] = v.strip()
        except OSError:
            pass
        return cls._cache

    @classmethod
    def get(cls, key, default=""):
        return cls._load().get(key, default)

    @classmethod
    def set(cls, key, value):
        data = cls._load()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        path = cls.path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Last.Pld playlist settings. Contains an OAuth refresh\n"
                     "# token - keep it owner-readable only.\n")
            for k, v in data.items():
                fh.write("%s=%s\n" % (k, v))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


# ----------------------------------------------------------------- spotify

class Spotify:
    AUTH = "https://accounts.spotify.com/authorize"
    TOKEN = "https://accounts.spotify.com/api/token"
    API = "https://api.spotify.com/v1"
    SCOPES = "playlist-modify-private playlist-modify-public"
    PLAYLIST_NAME = "Last.Pld"

    _access = ""
    _expires = 0.0

    # ---- helpers

    @staticmethod
    def _request(method, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise DBusError("network error: %s" % exc.reason)

    @staticmethod
    def _describe(body):
        try:
            doc = json.loads(body)
        except ValueError:
            return (body or "no detail")[:200]
        for key in ("error_description", "message"):
            if isinstance(doc.get(key), str):
                return doc[key]
        err = doc.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
        return (body or "no detail")[:200]

    @classmethod
    def client_id(cls):
        return Settings.get("spotify.client_id")

    @classmethod
    def port(cls):
        try:
            return int(Settings.get("spotify.redirect_port", "8888"))
        except ValueError:
            return 8888

    @classmethod
    def redirect_uri(cls):
        # Must be the loopback literal. Spotify stopped accepting "localhost"
        # for clients created after 2025-04-09, and permits plain http only for
        # loopback addresses.
        return "http://127.0.0.1:%d/callback" % cls.port()

    @classmethod
    def connected(cls):
        return bool(Settings.get("spotify.refresh_token"))

    # ---- connect

    @classmethod
    def connect(cls, client_id=None):
        import http.server

        if client_id:
            Settings.set("spotify.client_id", client_id)
        if not cls.client_id():
            return ("No Spotify client ID. Create a free app at\n"
                    "  https://developer.spotify.com/dashboard\n"
                    "add the redirect URI %s\n"
                    "(it must be 127.0.0.1, not localhost), then rerun with\n"
                    "  lastpld.py --connect-spotify --client-id <ID>"
                    % cls.redirect_uri())

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)

        url = cls.AUTH + "?" + urllib.parse.urlencode({
            "client_id": cls.client_id(),
            "response_type": "code",
            "redirect_uri": cls.redirect_uri(),
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": cls.SCOPES,
        })

        caught = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                caught["code"] = (query.get("code") or [None])[0]
                caught["state"] = (query.get("state") or [None])[0]
                caught["error"] = (query.get("error") or [None])[0]
                body = b"<meta charset=utf-8><p>You can close this tab.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        try:
            server = http.server.HTTPServer(("127.0.0.1", cls.port()), Handler)
        except OSError as exc:
            return "Cannot listen on 127.0.0.1:%d - %s" % (cls.port(), exc)

        print("Open this in a browser if it does not open by itself:\n\n  %s\n" % url)
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.timeout = 180
        server.handle_request()
        server.server_close()

        if caught.get("error"):
            return "Spotify sign-in was declined (%s)." % caught["error"]
        if caught.get("state") != state:
            return "The sign-in response did not match this request."
        if not caught.get("code"):
            return "Spotify returned no authorisation code."

        payload = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": caught["code"],
            "redirect_uri": cls.redirect_uri(),
            "client_id": cls.client_id(),
            "code_verifier": verifier,
        }).encode()
        status, body = cls._request(
            "POST", cls.TOKEN, payload,
            {"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            return "Spotify refused the sign-in: " + cls._describe(body)

        doc = json.loads(body)
        if not doc.get("refresh_token"):
            return "Spotify returned no refresh token."
        Settings.set("spotify.refresh_token", doc["refresh_token"])
        cls._access = doc.get("access_token", "")
        cls._expires = time.time() + int(doc.get("expires_in", 3600)) - 60
        return cls._ensure_playlist()

    @classmethod
    def disconnect(cls):
        Settings.set("spotify.refresh_token", "")
        Settings.set("spotify.playlist_id", "")
        cls._access = ""
        cls._expires = 0.0

    # ---- tokens

    @classmethod
    def _ensure_access(cls):
        if cls._access and time.time() < cls._expires:
            return None
        refresh = Settings.get("spotify.refresh_token")
        if not refresh:
            return "Spotify is not connected."
        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": cls.client_id(),
        }).encode()
        status, body = cls._request(
            "POST", cls.TOKEN, payload,
            {"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            return "Spotify sign-in expired, reconnect: " + cls._describe(body)
        doc = json.loads(body)
        cls._access = doc.get("access_token", "")
        cls._expires = time.time() + int(doc.get("expires_in", 3600)) - 60
        if doc.get("refresh_token"):      # Spotify rotates these
            Settings.set("spotify.refresh_token", doc["refresh_token"])
        return None if cls._access else "Spotify returned no access token."

    # ---- playlist

    @classmethod
    def _ensure_playlist(cls):
        if Settings.get("spotify.playlist_id"):
            return None
        err = cls._ensure_access()
        if err:
            return err
        # POST /me/playlists - the /users/{id}/playlists form was removed in
        # Spotify's February 2026 API change.
        payload = json.dumps({"name": cls.PLAYLIST_NAME, "public": False,
                              "description": "Songs identified by Last.Pld."}).encode()
        status, body = cls._request(
            "POST", cls.API + "/me/playlists", payload,
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + cls._access})
        if status not in (200, 201):
            return "Could not create the playlist: " + cls._describe(body)
        pid = json.loads(body).get("id")
        if not pid:
            return "Spotify created no playlist id."
        Settings.set("spotify.playlist_id", pid)
        return None

    # ---- resolve + add

    @staticmethod
    def normalise_isrc(isrc):
        """Spotify silently finds nothing for lowercase or dashed ISRCs."""
        return "".join(c for c in (isrc or "") if c.isalnum()).upper()

    @staticmethod
    def first_track_uri(body):
        """Pick the track URI, not the album's.

        Spotify nests an album object - with its own "uri" - before the track's
        own uri in every item, so reading the first "uri" field finds the wrong
        thing.
        """
        match = re.search(r"spotify:track:[A-Za-z0-9]+", body or "")
        return match.group(0) if match else None

    @classmethod
    def _search(cls, query):
        url = cls.API + "/search?" + urllib.parse.urlencode({
            "type": "track", "limit": 1,
            "market": Settings.get("spotify.market", "US"), "q": query})
        status, body = cls._request(
            "GET", url, None, {"Authorization": "Bearer " + cls._access})
        if status != 200:
            return None, "Spotify search failed: " + cls._describe(body)
        return cls.first_track_uri(body), None

    @classmethod
    def add(cls, title="", artist="", isrc=""):
        if not cls.connected():
            return "Spotify is not connected."
        err = cls._ensure_access()
        if err:
            return err
        err = cls._ensure_playlist()
        if err:
            return err

        uri = None
        isrc = cls.normalise_isrc(isrc)
        if isrc:
            uri, err = cls._search("isrc:" + isrc)
            if err:
                return err
        if not uri and title:
            query = title + (" artist:" + artist if artist else "")
            uri, err = cls._search(query)
            if err:
                return err
        if not uri:
            return "Spotify does not seem to have this track."

        payload = json.dumps({"uris": [uri]}).encode()
        pid = Settings.get("spotify.playlist_id")
        # POST /playlists/{id}/items - /tracks was removed in Feb 2026.
        status, body = cls._request(
            "POST", "%s/playlists/%s/items" % (cls.API, pid), payload,
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + cls._access})
        if status in (200, 201):
            return None
        if status == 404:                  # playlist deleted; make a new one
            Settings.set("spotify.playlist_id", "")
        return "Spotify rejected the add: " + cls._describe(body)


class Playlists:
    @staticmethod
    def auto_add():
        return Settings.get("auto_add", "1") != "0"

    @staticmethod
    def add_everywhere(track, isrc=""):
        """Only pushes what was identified, or a normal play if asked to."""
        if not Playlists.auto_add() or not Spotify.connected():
            return None
        try:
            err = Spotify.add(track.title, track.artist, isrc)
        except Exception as exc:                       # never kill the logger
            return "Spotify: %s" % exc
        return "Spotify: " + err if err else "added to your Spotify playlist"


# ------------------------------------------------------------------ identify

class Identify:
    """Optional. Absent tools disable it; they never stop the logger."""

    @staticmethod
    def recorder():
        for name, argv in (
            ("pw-record", ["pw-record", "--rate", "16000", "--channels", "1"]),
            ("parec", ["parec", "--format=s16le", "--rate=16000", "--channels=1"]),
            ("ffmpeg", ["ffmpeg"]),
        ):
            if shutil.which(name):
                return name, argv
        return None, None

    @staticmethod
    def capture(seconds, out_path):
        name, _ = Identify.recorder()
        if not name:
            return "no recorder found (install pipewire-utils, pulseaudio-utils or ffmpeg)"

        if name == "ffmpeg":
            argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "pulse", "-i", "default", "-t", str(seconds),
                    "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", out_path]
            try:
                subprocess.run(argv, timeout=seconds + 25, capture_output=True)
            except Exception as exc:
                return "ffmpeg failed: %s" % exc
        elif name == "pw-record":
            argv = ["pw-record", "--rate", "16000", "--channels", "1",
                    "--format", "s16", out_path]
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                time.sleep(seconds)
                proc.terminate()
                proc.wait(timeout=10)
            except Exception as exc:
                return "pw-record failed: %s" % exc
        else:
            # parec needs the monitor of the default sink to hear playback
            monitor = Identify.default_monitor()
            argv = ["parec", "--format=s16le", "--rate=16000", "--channels=1"]
            if monitor:
                argv += ["-d", monitor]
            try:
                with open(out_path, "wb") as fh:
                    proc = subprocess.Popen(argv, stdout=fh,
                                            stderr=subprocess.DEVNULL)
                    time.sleep(seconds)
                    proc.terminate()
                    proc.wait(timeout=10)
            except Exception as exc:
                return "parec failed: %s" % exc

        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
            return "nothing was playing - no audio was captured"
        return None

    @staticmethod
    def default_monitor():
        if not shutil.which("pactl"):
            return None
        try:
            out = subprocess.run(["pactl", "get-default-sink"],
                                 capture_output=True, text=True, timeout=5)
            sink = out.stdout.strip()
            return sink + ".monitor" if sink else None
        except Exception:
            return None

    @staticmethod
    def recognise(path):
        """songrec first - it is native and needs no python packages."""
        if shutil.which("songrec"):
            try:
                out = subprocess.run(
                    ["songrec", "audio-file-to-recognized-song", path],
                    capture_output=True, text=True, timeout=90)
                doc = json.loads(out.stdout or "{}")
                return Identify._from_shazam(doc)
            except Exception as exc:
                return {"matched": False, "error": "songrec: %s" % exc}

        try:
            import asyncio
            from shazamio import Shazam                      # optional extra
        except ImportError:
            return {"matched": False,
                    "error": "no recogniser installed - install songrec "
                             "(recommended) or 'pip install shazamio'"}
        try:
            doc = asyncio.run(Shazam().recognize(path))
            return Identify._from_shazam(doc)
        except Exception as exc:
            return {"matched": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    @staticmethod
    def _from_shazam(doc):
        track = (doc or {}).get("track") or {}
        if not track:
            return {"matched": False}
        meta = {}
        for section in track.get("sections") or []:
            for item in section.get("metadata") or []:
                if item.get("title") and item.get("text"):
                    meta[item["title"]] = item["text"]
        hub = track.get("hub") or {}
        apple_id = ""
        for action in hub.get("actions") or []:
            if action.get("type") == "applemusicplay" and action.get("id"):
                apple_id = str(action["id"])
                break
        return {
            "matched": True,
            "title": track.get("title") or "",
            "artist": track.get("subtitle") or "",
            "album": meta.get("Album", ""),
            "isrc": track.get("isrc") or "",
            "apple_track_id": apple_id,
        }

    @staticmethod
    def run(seconds=12):
        tmp = os.path.join(tempfile.gettempdir(), "lastpld_clip.wav")
        try:
            os.remove(tmp)
        except OSError:
            pass
        err = Identify.capture(seconds, tmp)
        if err:
            return {"matched": False, "error": err}
        return Identify.recognise(tmp)


# -------------------------------------------------------------------- systemd

SERVICE_UNIT = """\
[Unit]
Description=Last.Pld play-history recorder
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exe} --quiet
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_service():
    exe = os.path.abspath(__file__)
    os.chmod(exe, 0o755)
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    unit_path = os.path.join(unit_dir, "lastpld.service")
    with open(unit_path, "w", encoding="utf-8") as fh:
        fh.write(SERVICE_UNIT.format(exe=exe))
    print("wrote %s" % unit_path)

    if not shutil.which("systemctl"):
        print("systemctl not found - enable it yourself when convenient.")
        return 0
    for argv in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", "lastpld.service"]):
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            print("  %s -> %s" % (" ".join(argv),
                                  (result.stderr or "").strip()))
            return 1
    print("enabled and started. Check it with:\n"
          "  systemctl --user status lastpld")
    return 0


# --------------------------------------------------------------------- probe

def probe():
    bus = Bus()
    print("Last.Pld %s" % VERSION)
    print("data dir : %s" % data_dir())
    print("d-bus    : %s" % bus.how())
    if not bus.available:
        print("\nNo D-Bus command line tool found. Install one of:")
        print("  systemd (busctl), glib2 (gdbus), or dbus (dbus-send)")
        return 1

    name, _ = Identify.recorder()
    print("recorder : %s" % (name or "none - Identify disabled"))
    print("recogniser: %s" % ("songrec" if shutil.which("songrec") else
                              "shazamio (if installed) or none"))
    print("spotify  : %s" % ("connected" if Spotify.connected() else "not connected"))

    try:
        services = bus.players()
    except DBusError as exc:
        print("\ncould not list players: %s" % exc)
        return 1

    if not services:
        print("\nNo MPRIS players on the bus. Start a media player and retry.")
        return 0

    filt = Filter.load()
    print("\n%d MPRIS player(s):" % len(services))
    for service in services:
        print("\n  %s" % service)
        try:
            props = bus.get_all(service, PLAYER_IFACE)
        except DBusError as exc:
            print("      unreadable: %s" % exc)
            continue
        status = str(props.get("PlaybackStatus", "?"))
        title, artist, album, url = parse_metadata(props.get("Metadata"))
        print("      status : %s" % status)
        print("      title  : %s" % (title or "-"))
        print("      artist : %s" % (artist or "-"))
        print("      album  : %s" % (album or "-"))
        print("      url    : %s" % (url or "-"))
        ok, why = filt.decision(service, url)
        print("      logged : %s (%s)" % ("YES" if ok else "no", why))
    return 0


# ------------------------------------------------------------------ selftest

def selftest():
    failures = []

    def check(name, ok, detail=""):
        print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                              "   [%s]" % detail if detail else ""))
        if not ok:
            failures.append(name)

    print("=== metadata parsing ===")
    meta = {"xesam:title": "Take On Me", "xesam:artist": ["a-ha"],
            "xesam:album": "Hunting High and Low",
            "xesam:url": "https://open.spotify.com/track/x"}
    t, a, al, u = parse_metadata(meta)
    check("title/artist/album parsed", (t, a, al) == ("Take On Me", "a-ha",
                                                      "Hunting High and Low"),
          "%s / %s / %s" % (t, a, al))
    check("artist list joined",
          parse_metadata({"xesam:artist": ["a", "b"]})[1] == "a, b")
    check("missing metadata is harmless", parse_metadata(None) == ("", "", "", ""))
    check("albumArtist used as fallback",
          parse_metadata({"xesam:albumArtist": ["Various"]})[1] == "Various")

    print("\n=== app naming ===")
    check("instance suffix stripped",
          friendly_app("org.mpris.MediaPlayer2.chromium.instance1234") == "chromium")
    check("plain name kept",
          friendly_app("org.mpris.MediaPlayer2.spotify") == "spotify")
    check("Identity preferred",
          friendly_app("org.mpris.MediaPlayer2.vlc", "VLC media player")
          == "VLC media player")

    print("\n=== source filter ===")
    filt = Filter(["spotify", "vlc"], youtube=True)
    check("allowed player logs",
          filt.should_log("org.mpris.MediaPlayer2.spotify", ""))
    check("unknown player does not",
          not filt.should_log("org.mpris.MediaPlayer2.weirdthing", ""))
    check("browser on YouTube logs",
          filt.should_log("org.mpris.MediaPlayer2.chromium.instance1",
                          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    check("browser on Facebook does not",
          not filt.should_log("org.mpris.MediaPlayer2.chromium.instance1",
                              "https://www.facebook.com/reel/123"))
    check("youtu.be short link logs",
          filt.should_log("org.mpris.MediaPlayer2.firefox", "https://youtu.be/abc"))
    check("lookalike domain rejected",
          not filt.should_log("org.mpris.MediaPlayer2.firefox",
                              "https://youtube.com.evil.tld/watch?v=1"))
    check("browser with no url does not log",
          not filt.should_log("org.mpris.MediaPlayer2.chromium.instance1", ""))
    check("no-youtube switches it off",
          not Filter(["spotify"], youtube=False).should_log(
              "org.mpris.MediaPlayer2.chromium.instance1",
              "https://www.youtube.com/watch?v=x"))

    print("\n=== busctl json unwrapping ===")
    raw = {"type": "a{sv}", "data": [{
        "PlaybackStatus": {"type": "s", "data": "Playing"},
        "Metadata": {"type": "a{sv}", "data": {
            "xesam:title": {"type": "s", "data": "Song"},
            "xesam:artist": {"type": "as", "data": ["Band"]}}}}]}
    flat = _first_dict(_unwrap_busctl(raw))
    check("properties unwrapped", flat.get("PlaybackStatus") == "Playing", str(flat))
    check("nested metadata unwrapped",
          parse_metadata(flat.get("Metadata"))[:2] == ("Song", "Band"))

    print("\n=== gvariant text parsing ===")
    text = ("({'PlaybackStatus': <'Playing'>, 'Metadata': "
            "<{'xesam:title': <'Song'>, 'xesam:artist': <['Band']>}>},)")
    parsed = _first_dict(_gvariant_scan(text))
    check("gdbus reply parsed", parsed.get("PlaybackStatus") == "Playing", str(parsed))
    check("gdbus metadata parsed",
          parse_metadata(parsed.get("Metadata"))[:2] == ("Song", "Band"))

    print("\n=== spotify helpers ===")
    check("isrc normalised",
          Spotify.normalise_isrc("us-wb1 1001072") == "USWB11001072")
    body = ('{"tracks":{"items":[{"album":{"uri":"spotify:album:AAA"},'
            '"uri":"spotify:track:BBB"}]}}')
    check("track uri picked over album uri",
          Spotify.first_track_uri(body) == "spotify:track:BBB",
          str(Spotify.first_track_uri(body)))
    check("no match returns None", Spotify.first_track_uri("{}") is None)

    print("\n=== csv round trip ===")
    tmpdir = tempfile.mkdtemp(prefix="lastpld-test-")
    try:
        store = Store(os.path.join(tmpdir, "h.csv"), os.path.join(tmpdir, "t.csv"))
        store.append(Track("Song, with comma", 'Quote"Artist', "Album", "spotify"))
        rows = store.rows()
        check("row survives commas and quotes",
              rows and rows[0][1] == "Song, with comma"
              and rows[0][2] == 'Quote"Artist', str(rows))
        with open(store.path, "rb") as fh:
            check("written with a BOM, matching the Windows build",
                  fh.read(3) == b"\xef\xbb\xbf")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n=== dedup ===")
    tmpdir = tempfile.mkdtemp(prefix="lastpld-test-")
    try:
        store = Store(os.path.join(tmpdir, "h.csv"), os.path.join(tmpdir, "t.csv"))

        class FakeBus(Bus):
            def __init__(self, tracks):
                self._tracks = tracks

            def players(self):
                return ["org.mpris.MediaPlayer2.spotify"]

            def get_all(self, service, interface):
                if interface == ROOT_IFACE:
                    return {"Identity": "Spotify"}
                return self._tracks

        props = {"PlaybackStatus": "Playing",
                 "Metadata": {"xesam:title": "One", "xesam:artist": ["A"]}}
        logger = Logger(store, Filter(["spotify"]), FakeBus(props), verbose=False)
        first = logger.tick()
        second = logger.tick()
        check("first play logged", len(first) == 1)
        check("repeat suppressed", len(second) == 0)

        props["Metadata"] = {"xesam:title": "Two", "xesam:artist": ["A"]}
        check("new track logged", len(logger.tick()) == 1)
        check("history has exactly two rows", len(store.rows()) == 2,
              str(store.rows()))

        # A restart mid-song must not log that song again. The app now quits
        # and starts with the window, so this happens often enough to notice.
        restarted = Logger(store, Filter(["spotify"]), FakeBus(props), verbose=False)
        check("restart does not re-log what is already playing",
              len(restarted.tick()) == 0)
        check("still exactly two rows after a restart", len(store.rows()) == 2,
              str(store.rows()))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n%s" % ("ALL PASSED" if not failures
                    else "%d FAILED: %s" % (len(failures), ", ".join(failures))))
    return 0 if not failures else 1


# ---------------------------------------------------------------------- cli

def show_history(limit):
    store = Store()
    rows = store.rows()[-limit:]
    if not rows:
        print("nothing logged yet - run lastpld.py to start")
        return 0
    for row in rows:
        line = "%s  %s" % (row[0], row[1])
        if row[2]:
            line += "  -  " + row[2]
        print("%s   (%s)" % (line, row[4]))
    return 0


def search_history(query):
    store = Store()
    needle = query.lower()
    hits = [r for r in store.rows()
            if needle in (r[1] + " " + r[2] + " " + r[3]).lower()]
    if not hits:
        print("no match for %r" % query)
        return 1
    for row in hits[-100:]:
        print("%s  %s  -  %s   (%s)" % (row[0], row[1], row[2], row[4]))
    return 0


def do_identify():
    print("listening for 12 seconds...")
    result = Identify.run()
    if not result.get("matched"):
        print("no match: %s" % result.get("error", "nothing recognised"))
        return 1
    print("\n  %s\n  %s" % (result["title"], result.get("artist", "")))
    if result.get("album"):
        print("  %s" % result["album"])

    store = Store()
    track = Track(result["title"], result.get("artist", ""),
                  result.get("album", ""), "Shazam")
    store.append(track)
    print("\nadded to your history.")

    note = Playlists.add_everywhere(track, result.get("isrc", ""))
    if note:
        print(note)
    return 0

# ------------------------------------------------------------------ desktop
#
# Everything below is the optional desktop front end. It is guarded by the
# import that follows: if PyGObject is not installed the file still runs, it
# just logs headlessly. That is the whole no-dependency promise - the GUI is a
# bonus where the desktop stack happens to exist, never a requirement.
#
# Importing Gtk does not open a display; only Gtk.init() does, and that happens
# inside Adw.Application.run(). A headless service pays a few milliseconds.

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango, PangoCairo
    import cairo

    HAVE_GTK = True
except (ImportError, ValueError):
    HAVE_GTK = False

try:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop

    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False


APP_ID = "io.github.guardian509.LastPld"
ACCENT = "#fa5a64"          # the red the Windows build draws its icon in
ICON_NAME = "lastpld"


def gui_possible():
    return HAVE_GTK and bool(os.environ.get("WAYLAND_DISPLAY") or
                             os.environ.get("DISPLAY"))


# ------------------------------------------------------------- old service

def retire_old_service():
    """Earlier versions logged from a systemd unit. This one logs in-process.

    Leaving both running would double-log every track, so the unit is stood
    down the first time the app starts. Nothing is removed that the user did
    not get from us in the first place.
    """
    unit = os.path.expanduser("~/.config/systemd/user/lastpld.service")
    if not os.path.exists(unit) or not shutil.which("systemctl"):
        return False
    for argv in (["systemctl", "--user", "disable", "--now", "lastpld.service"],):
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.remove(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    return True


# -------------------------------------------------------------- autostart
#
# The desktop entry runs the file where it sits. Move the file and re-run it
# once and the entry is rewritten; delete the file and the entry deletes
# itself at the next login rather than failing forever in the background.

AUTOSTART_DESKTOP = """\
[Desktop Entry]
Type=Application
Name=Last.Pld
Comment=Records what you actually played
Exec=sh -c 'test -x "{exe}" && exec "{exe}" --background || rm -f "{entry}"'
Icon={icon}
Terminal=false
Categories=AudioVideo;Audio;
X-GNOME-Autostart-enabled=true
"""

LAUNCHER_DESKTOP = """\
[Desktop Entry]
Type=Application
Name=Last.Pld
Comment=Records what you actually played
Exec="{exe}"
Icon={icon}
Terminal=false
Categories=AudioVideo;Audio;
"""


def autostart_path():
    return os.path.join(user_config_dir(), "autostart", "lastpld.desktop")


def launcher_path():
    return os.path.join(user_data_base(), "applications", "lastpld.desktop")


def user_config_dir():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def user_data_base():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def install_autostart(quiet=False):
    exe = os.path.abspath(__file__)
    try:
        os.chmod(exe, 0o755)
    except OSError:
        pass
    write_icon()
    pairs = ((autostart_path(), AUTOSTART_DESKTOP), (launcher_path(), LAUNCHER_DESKTOP))
    for path, template in pairs:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(template.format(exe=exe, entry=autostart_path(), icon=ICON_NAME))
            os.chmod(path, 0o755)
        except OSError as exc:
            if not quiet:
                print("could not write %s: %s" % (path, exc), file=sys.stderr)
            return 1
    if not quiet:
        print("Last.Pld will start automatically when you log in.")
        print("  %s" % autostart_path())
    return 0


def uninstall_autostart():
    for path in (autostart_path(), launcher_path()):
        try:
            os.remove(path)
            print("removed %s" % path)
        except OSError:
            pass
    print("\nYour history is untouched: %s" % data_dir())
    print("Delete this file to remove the app itself.")
    return 0


def autostart_installed():
    return os.path.exists(autostart_path())


def write_icon():
    """A ♫ in the accent colour, so the tray and launcher have a face."""
    base = os.path.join(user_data_base(), "icons")
    target = os.path.join(base, "hicolor", "scalable", "apps", ICON_NAME + ".svg")
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
        'viewBox="0 0 48 48">\n'
        '  <text x="24" y="37" text-anchor="middle" font-family="sans-serif" '
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
                         "[scalable/apps]\nSize=48\nType=Scalable\n"
                         "Context=Applications\n")
    except OSError:
        return None
    return base


# ------------------------------------------------------------------ trash

class History:
    """The CSV, plus moving rows in and out of the trash file.

    Rows are matched by value, not by index: the recorder appends while the
    window is open, so a row's position is not stable between a read and the
    rewrite that follows it.
    """

    def __init__(self, store):
        self.store = store

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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
        os.replace(tmp, path)

    @staticmethod
    def _append(path, rows):
        exists = os.path.exists(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            if not exists:
                writer.writerow(CSV_HEADER)
            writer.writerows(rows)

    def rows(self):
        return self._read(self.path)

    def trash_rows(self):
        return self._read(self.trash_path)

    def _move(self, source, target, rows):
        pending = [tuple(r) for r in rows]
        if not pending:
            return
        keep, moved = [], []
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
        gone = [tuple(r) for r in rows]
        self._write(self.trash_path,
                    [r for r in self._read(self.trash_path) if tuple(r) not in gone])

    def empty_trash(self):
        self._write(self.trash_path, [])


# --------------------------------------------------------------- recorder

if HAVE_GTK:

    class Recorder(GObject.Object):
        """Runs the logger on a worker thread inside the GUI process.

        One process does both jobs, the way the Windows build does. Pausing
        stops the writing but keeps the polling, so the now-playing banner
        stays live while logging is off.
        """

        __gsignals__ = {
            "now-playing": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
            "logged": (GObject.SignalFlags.RUN_FIRST, None, ()),
        }

        def __init__(self, store, bus):
            GObject.Object.__init__(self)
            self.logger = Logger(store, Filter.load(), bus, verbose=False)
            self.paused = Settings.get("logging", "1") != "1"
            self._stop = threading.Event()
            self._current = None
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

        def stop(self):
            self._stop.set()

        def set_paused(self, paused):
            self.paused = bool(paused)
            Settings.set("logging", "0" if self.paused else "1")

        def _loop(self):
            complained = False
            while not self._stop.wait(POLL_SECONDS):
                try:
                    playing = self._tick()
                except DBusError:
                    playing = None                  # a player went away mid-read
                except Exception:
                    # Anything else is a bug in here. Say so once; swallowing it
                    # silently just looks like "nothing is ever playing".
                    playing = None
                    if not complained:
                        complained = True
                        traceback.print_exc()

                key = None if playing is None else (playing.title, playing.artist,
                                                    playing.app)
                if key != self._current:
                    self._current = key
                    GLib.idle_add(self.emit, "now-playing", playing)

        def _tick(self):
            if not self.paused:
                if self.logger.tick():
                    GLib.idle_add(self.emit, "logged")

            # snapshot() is what tick() itself reads, so the banner shows exactly
            # what the logger sees - including players the filter will not log.
            for track in self.logger.snapshot():
                return track
            return None


    # ------------------------------------------------------------------ icon

    def _icon_surface(size):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surface)
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        layout = PangoCairo.create_layout(ctx)
        layout.set_font_description(
            Pango.FontDescription("Sans Bold %d" % max(8, int(size * 0.72))))
        layout.set_text("♫", -1)
        ink, logical = layout.get_pixel_extents()
        width = ink.width or logical.width or size
        height = ink.height or logical.height or size
        ctx.set_source_rgb(0xfa / 255.0, 0x5a / 255.0, 0x64 / 255.0)
        ctx.move_to((size - width) / 2.0 - ink.x, (size - height) / 2.0 - ink.y)
        PangoCairo.show_layout(ctx, layout)
        surface.flush()
        return surface


    def icon_pixmap(size=32):
        """SNI wants width, height, then ARGB32 in network byte order."""
        surface = _icon_surface(size)
        data = bytes(surface.get_data())
        stride = surface.get_stride()
        out = bytearray(size * size * 4)
        at = 0
        for y in range(size):
            row = data[y * stride:y * stride + size * 4]
            for x in range(0, size * 4, 4):
                # cairo hands back premultiplied BGRA on a little-endian machine
                blue, green, red, alpha = row[x], row[x + 1], row[x + 2], row[x + 3]
                out[at], out[at + 1], out[at + 2], out[at + 3] = alpha, red, green, blue
                at += 4
        return dbus.Struct((dbus.Int32(size), dbus.Int32(size),
                            dbus.ByteArray(bytes(out))), signature="iiay")


# -------------------------------------------------------------- dbus menu

if HAVE_DBUS and HAVE_GTK:

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
                return dbus.Dictionary({"type": dbus.String("separator")},
                                       signature="sv")
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
            return dbus.Struct(
                (dbus.Int32(self.id), self.properties(),
                 dbus.Array([c.node() for c in self.children], signature="v")),
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
            """Rebuilt before it opens, so the checkmark is never stale."""
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

        @dbus.service.method(DBUSMENU_IFACE, in_signature="iias",
                             out_signature="u(ia{sv}av)")
        def GetLayout(self, parent_id, recursion_depth, property_names):
            item = self._find(parent_id) or self._root()
            return dbus.UInt32(self._revision), item.node()

        @dbus.service.method(DBUSMENU_IFACE, in_signature="aias",
                             out_signature="a(ia{sv})")
        def GetGroupProperties(self, ids, property_names):
            return dbus.Array(
                [dbus.Struct((dbus.Int32(i.id), i.properties()), signature="ia{sv}")
                 for i in self._root().walk() if not ids or i.id in ids],
                signature="(ia{sv})")

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

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss",
                             out_signature="v")
        def Get(self, interface, name):
            return self.GetAll(interface).get(name, dbus.String(""))

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s",
                             out_signature="a{sv}")
        def GetAll(self, interface):
            return dbus.Dictionary({
                "Version": dbus.UInt32(3),
                "TextDirection": dbus.String("ltr"),
                "Status": dbus.String("normal"),
                "IconThemePath": dbus.Array([], signature="s"),
            }, signature="sv")


    class Tray(dbus.service.Object):
        """A StatusNotifierItem, published directly.

        libappindicator would have been less code, but it is GTK3-only and
        cannot be loaded into a GTK4 process.
        """

        def __init__(self, build_menu, on_activate):
            self._on_activate = on_activate
            self._tooltip = "Last.Pld - logging"
            self._theme_path = write_icon()
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
                # No watcher: a desktop without tray support, or GNOME without
                # the AppIndicator extension. The window still works.
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

        @dbus.service.signal(SNI_IFACE, signature="s")
        def NewStatus(self, status):
            pass

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss",
                             out_signature="v")
        def Get(self, interface, name):
            return self.GetAll(interface).get(name, dbus.String(""))

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s",
                             out_signature="a{sv}")
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

        @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv",
                             out_signature="")
        def Set(self, interface, name, value):
            pass


# ----------------------------------------------------------------- window

if HAVE_GTK:

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


    CSS = """
    .nowplaying { padding: 14px 18px; border-bottom: 1px solid alpha(currentColor, 0.12); }
    .nowplaying-title { font-size: 15pt; font-weight: bold; color: %s; }
    .nowplaying-sub { opacity: 0.7; }
    .dim { opacity: 0.6; }
    """ % ACCENT


    class Window(Adw.ApplicationWindow):
        def __init__(self, app, history, recorder):
            Adw.ApplicationWindow.__init__(self, application=app)
            self.history = history
            self.recorder = recorder
            self.viewing_trash = False
            self._identifying = False
            self._reload_pending = False

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

            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_key)
            self.add_controller(keys)
            self.connect("close-request", self._on_close)

            recorder.connect("now-playing", self._on_now_playing)
            recorder.connect("logged", lambda *_: self.reload())
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
            self.identify_button.set_tooltip_text("Fingerprint what is playing now")
            self.identify_button.connect("clicked", lambda *_: self.identify_now())
            header.pack_end(self.identify_button)

            menu = Gio.Menu()
            section = Gio.Menu()
            section.append("Open CSV", "win.open-csv")
            section.append("Open folder", "win.open-folder")
            menu.append_section(None, section)
            section = Gio.Menu()
            section.append("Pause logging", "win.toggle-logging")
            section.append("Edit sources…", "win.edit-sources")
            section.append("Start at login", "win.toggle-autostart")
            menu.append_section(None, section)
            section = Gio.Menu()
            section.append("Connect Spotify…", "win.spotify")
            section.append("Why not Apple Music?", "win.apple")
            menu.append_section(None, section)
            header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                           menu_model=menu))

            for name, handler in (
                ("open-csv", lambda *_: self._open(self.history.path)),
                ("open-folder", lambda *_: self._open(os.path.dirname(self.history.path))),
                ("toggle-logging", lambda *_: self.toggle_logging()),
                ("edit-sources", lambda *_: self._open(path_in_data("sources.txt"))),
                ("toggle-autostart", lambda *_: self.toggle_autostart()),
                ("spotify", lambda *_: self.spotify_setup()),
                ("apple", lambda *_: self._apple_music_note()),
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
            self.search_entry = Gtk.SearchEntry(
                placeholder_text="Search title, artist, album")
            self.search_entry.set_hexpand(True)
            self.search_entry.connect("search-changed", lambda *_: self._refilter())
            bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bar_box.append(self.search_entry)
            self.source_dropdown = Gtk.DropDown.new_from_strings(["All sources"])
            self.source_dropdown.connect("notify::selected",
                                         lambda *_: self._refilter())
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

            self.row_menu = Gtk.PopoverMenu()
            self.row_menu.set_parent(self.column_view)
            self.row_menu.set_has_arrow(False)
            gesture = Gtk.GestureClick()
            gesture.set_button(3)
            gesture.connect("pressed", self._on_right_click)
            self.column_view.add_controller(gesture)
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

        # ---- keys and menus

        def _on_key(self, _controller, keyval, _code, state):
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.search_button.set_active(True)
                self.search_entry.grab_focus()
                return True
            if keyval == Gdk.KEY_Escape:
                # Esc clears a search if there is one, otherwise drops to the
                # tray. Recording carries on either way.
                if self.search_entry.get_text():
                    self.search_entry.set_text("")
                elif self.search_button.get_active():
                    self.search_button.set_active(False)
                else:
                    self.set_visible(False)
                return True
            if keyval == Gdk.KEY_Delete:
                self.purge_selected() if self.viewing_trash else self.trash_selected()
                return True
            return False

        def _on_right_click(self, _gesture, _n, x, y):
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
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            self.row_menu.set_pointing_to(rect)
            self.row_menu.popup()

        # ---- data

        def reload(self):
            rows = (self.history.trash_rows() if self.viewing_trash
                    else self.history.rows())
            rows = list(reversed(rows))              # newest first
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
            self.source_dropdown.set_model(Gtk.StringList.new(["All sources"] + seen))
            self.source_dropdown.set_selected(
                seen.index(current) + 1 if current in seen else 0)

        def _selected_source(self):
            model = self.source_dropdown.get_model()
            index = self.source_dropdown.get_selected()
            if model is None or index in (0, Gtk.INVALID_LIST_POSITION):
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
            shown, total = self.selection.get_n_items(), self.store.get_n_items()
            where = "trash" if self.viewing_trash else "plays"
            paused = "" if not self.recorder.paused else "  ·  logging paused"
            self.status.set_text(("%d %s%s" % (total, where, paused)) if shown == total
                                 else ("%d of %d %s%s" % (shown, total, where, paused)))

        def selected_rows(self):
            return [self.selection.get_item(i).values
                    for i in range(self.selection.get_n_items())
                    if self.selection.is_selected(i)]

        # ---- row commands

        def copy_selected(self):
            rows = self.selected_rows()
            if not rows:
                return
            self.get_clipboard().set("\n".join(
                ("%s — %s" % (r[2], r[1])) if r[2] else r[1] for r in rows))
            self._toast("Copied")

        def search_apple_music(self):
            rows = self.selected_rows()
            if not rows:
                return
            term = urllib.parse.quote((rows[0][2] + " " + rows[0][1]).strip())
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
            self.restore_selected() if self.viewing_trash else self.search_apple_music()

        # ---- toggles

        def _on_search_toggled(self, button):
            self.search_bar.set_search_mode(button.get_active())
            if button.get_active():
                self.search_entry.grab_focus()

        def _on_trash_toggled(self, button):
            self.viewing_trash = button.get_active()
            self.window_title.set_subtitle("trash" if self.viewing_trash
                                           else "play history")
            self.reload()

        def toggle_logging(self):
            self.recorder.set_paused(not self.recorder.paused)
            self._toast("Logging paused" if self.recorder.paused else "Logging on")

        def toggle_autostart(self):
            if autostart_installed():
                uninstall_autostart()
                self._toast("Will no longer start at login")
            else:
                install_autostart(quiet=True)
                self._toast("Will start at login")

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
                    result = Identify.run()
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
                track = Track(result["title"], result.get("artist", ""),
                              result.get("album", ""), "Shazam")
                self.history.store.append(track)
                Playlists.add_everywhere(track, result.get("isrc", ""))
                self.reload()
                self._alert(result["title"],
                            "\n".join(x for x in (result.get("artist", ""),
                                                  result.get("album", ""),
                                                  "Added to your history.") if x))
                return False

            threading.Thread(target=work, daemon=True).start()

        # ---- spotify

        def spotify_setup(self):
            if Spotify.connected():
                self._confirm("Disconnect Spotify?",
                              "Nothing further will be added to your playlist.",
                              lambda: (Spotify.disconnect(), self._toast("Disconnected")))
                return
            self._alert(
                "Connect Spotify",
                "Spotify needs a free developer app, and the connect flow opens "
                "a browser and waits on a local redirect. Run it from a "
                "terminal:\n\n    %s --connect-spotify\n\nOnce connected this "
                "menu offers to disconnect instead." % os.path.abspath(__file__))

        def _apple_music_note(self):
            self._alert(
                "Why not Apple Music?",
                "Apple Music has no playlist API a desktop app can use without "
                "a paid Apple Developer account and a MusicKit token, and the "
                "token cannot be issued from the app itself.\n\n"
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
            dialog.connect("response",
                           lambda _d, response: on_yes() if response == "go" else None)
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
            self.now_sub.set_text(("%s — %s" % (detail, track.app)) if detail
                                  else track.app)

        def _on_close(self, *_args):
            # Closing hides to the tray so recording continues; Quit comes from
            # the tray menu. The Windows build behaves the same way.
            self.set_visible(False)
            return True


    # ------------------------------------------------------------ application

    class Application(Adw.Application):
        def __init__(self, background):
            Adw.Application.__init__(self, application_id=APP_ID,
                                     flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
            self.background = background
            self.window = None
            self.tray = None
            self.recorder = None

        def do_startup(self):
            Adw.Application.do_startup(self)

            provider = Gtk.CssProvider()
            provider.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            retire_old_service()
            store = Store()
            self.history = History(store)
            self.recorder = Recorder(store, Bus())
            self.recorder.connect("now-playing", self._tray_tooltip)
            self.window = Window(self, self.history, self.recorder)

            if HAVE_DBUS:
                self.tray = Tray(self._build_tray_menu, self.surface_window)

            # First run installs itself. One file, one click, running at login.
            if not autostart_installed():
                install_autostart(quiet=True)

            # Without a visible window the app would exit as soon as the last
            # one closes; the tray needs the process to stay alive.
            self.hold()

        def do_activate(self):
            if self.background:
                self.background = False      # only the very first launch is silent
                if self.tray is None or not self.tray.registered:
                    # No tray to hide in, so showing the window is the only sane
                    # outcome - otherwise the app would be invisible.
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
                return
            text = ("%s - %s" % (track.title, track.artist) if track.artist
                    else track.title)
            self.tray.set_tooltip(text[:59] + "..." if len(text) > 62 else text)

        def _build_tray_menu(self):
            return [
                MenuItem("Open history", self.surface_window),
                MenuItem("Identify song now", self._tray_identify),
                MenuItem(kind="separator"),
                MenuItem("Logging", self._tray_toggle_logging,
                         toggle=not self.recorder.paused),
                MenuItem("Start at login", self._tray_toggle_autostart,
                         toggle=autostart_installed()),
                MenuItem("Open CSV folder", self._tray_open_folder),
                MenuItem(kind="separator"),
                MenuItem("Quit Last.Pld", self._tray_quit),
            ]

        def _tray_identify(self):
            self.window.identify_now()
            return False

        def _tray_toggle_logging(self):
            self.window.toggle_logging()
            return False

        def _tray_toggle_autostart(self):
            self.window.toggle_autostart()
            return False

        def _tray_open_folder(self):
            self.window._open(os.path.dirname(self.history.path))
            return False

        def _tray_quit(self):
            # Quit stops recording too: with logging in-process there is no
            # daemon left behind, which is what "quit" has to mean.
            if self.recorder:
                self.recorder.stop()
            self.release()
            self.quit()
            return False


def run_gui(background):
    if not HAVE_GTK:
        print("The desktop front end needs PyGObject (GTK4 + libadwaita).\n"
              "Install it with your package manager, for example:\n"
              "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
              "  sudo dnf install python3-gobject gtk4 libadwaita\n\n"
              "Logging itself needs none of that - run with --quiet to record "
              "headlessly.", file=sys.stderr)
        return 1
    if HAVE_DBUS:
        DBusGMainLoop(set_as_default=True)
    return Application(background).run([sys.argv[0]])

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lastpld.py",
        description="Last.Pld for Linux - records what you actually played.")
    parser.add_argument("--probe", action="store_true",
                        help="show visible MPRIS players and the log decision for each")
    parser.add_argument("--selftest", action="store_true", help="run built-in tests")
    parser.add_argument("--history", nargs="?", type=int, const=20,
                        metavar="N", help="print the last N tracks (default 20)")
    parser.add_argument("--search", metavar="TEXT", help="search the history")
    parser.add_argument("--identify", action="store_true",
                        help="fingerprint what is playing right now")
    parser.add_argument("--connect-spotify", action="store_true",
                        help="authorise pushing identified songs to Spotify")
    parser.add_argument("--disconnect-spotify", action="store_true")
    parser.add_argument("--client-id", metavar="ID", help="Spotify app client ID")
    parser.add_argument("--background", action="store_true",
                        help="start in the tray without opening the window")
    parser.add_argument("--no-gui", action="store_true",
                        help="record in the terminal even on a desktop")
    parser.add_argument("--install-autostart", action="store_true",
                        help="start automatically when you log in")
    parser.add_argument("--uninstall-autostart", action="store_true",
                        help="stop starting automatically")
    parser.add_argument("--install-service", action="store_true",
                        help="write and enable a systemd user service (headless boxes)")
    parser.add_argument("--quiet", action="store_true", help="log without printing")
    parser.add_argument("--version", action="version", version="Last.Pld " + VERSION)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.probe:
        return probe()
    if args.history is not None:
        return show_history(args.history)
    if args.search:
        return search_history(args.search)
    if args.install_autostart:
        return install_autostart()
    if args.uninstall_autostart:
        return uninstall_autostart()
    if args.install_service:
        return install_service()
    if args.disconnect_spotify:
        Spotify.disconnect()
        print("Spotify disconnected.")
        return 0
    if args.connect_spotify:
        err = Spotify.connect(args.client_id)
        print(err if err else "Connected. Identified songs will go to your "
                              '"Last.Pld" playlist.')
        return 1 if err else 0
    if args.identify:
        return do_identify()

    # The desktop app is the default when there is a desktop to put it on.
    # --background comes from the autostart entry, and must still record on a
    # machine with no GTK - otherwise logging in would silently do nothing.
    if not (args.quiet or args.no_gui) and gui_possible():
        return run_gui(args.background)

    bus = Bus()
    if not bus.available:
        print("No D-Bus command line tool found (need busctl, gdbus or "
              "dbus-send).\nRun --probe for details.", file=sys.stderr)
        return 1
    verbose = not (args.quiet or args.background)
    if verbose and not autostart_installed():
        print("Tip: --install-autostart keeps this running after you log in.\n")
    Logger(Store(), Filter.load(), bus, verbose=verbose).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
