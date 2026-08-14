#!/usr/bin/env python3
"""Last.Pld for Linux - a local play-history recorder.

The Windows build reads the System Media Transport Controls. Linux has the same
idea in MPRIS, published over D-Bus by essentially every media player and
browser, so this is a port of the concept rather than of the code.

Deliberately depends on nothing you have to install. Standard library only: no
pip, no venv, no third-party modules. D-Bus is reached through `busctl` (ships
with systemd) or `gdbus` (ships with glib) - whichever is present. If you have a
Linux desktop playing audio, you already have one of them.

Optional extras light up only if the tools happen to exist, and their absence
never stops the logger:

    Identify   songrec, or shazamio on the python path, plus a recorder
               (pw-record / parec / ffmpeg)
    Playlists  nothing - the Spotify client here is stdlib urllib

Usage:
    lastpld.py                    log in the foreground (Ctrl-C to stop)
    lastpld.py --probe            show what MPRIS players are visible, and why
                                  each one would or would not be logged
    lastpld.py --history 20       print the last 20 tracks
    lastpld.py --search whitney   search the history
    lastpld.py --identify         fingerprint what is playing right now
    lastpld.py --connect-spotify  authorise pushing identified songs to Spotify
    lastpld.py --install-service  write and enable a systemd user service
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
import time
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
        """Don't re-log the track that was already playing when we started."""
        now = time.time()
        for row in self.store.rows()[-200:]:
            self._recent[(row[4], row[1], row[2])] = now - DEDUP_SECONDS

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
    parser.add_argument("--install-service", action="store_true",
                        help="write and enable a systemd user service")
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
    if args.install_service:
        return install_service()
    if args.disconnect_spotify:
        Spotify.disconnect()
        print("Spotify disconnected.")
        return 0
    if args.connect_spotify:
        err = Spotify.connect(args.client_id)
        print(err if err else "Connected. Identified songs will go to your "
                              "\"Last.Pld\" playlist.")
        return 1 if err else 0
    if args.identify:
        return do_identify()

    bus = Bus()
    if not bus.available:
        print("No D-Bus command line tool found (need busctl, gdbus or "
              "dbus-send).\nRun --probe for details.", file=sys.stderr)
        return 1
    Logger(Store(), Filter.load(), bus, verbose=not args.quiet).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
