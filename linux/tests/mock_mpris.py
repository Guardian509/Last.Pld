#!/usr/bin/env python3
"""A fake MPRIS player, so the logger can be tested without a real one.

Publishes org.mpris.MediaPlayer2.<name> on the session bus and answers the
property reads Last.Pld makes. Test-only: this is the one place that needs
python3-dbus, which the app itself deliberately does not.

    mock_mpris.py --name testplayer --title "Song" --artist "Band"
    mock_mpris.py --name chromium.instance1 --title "Clip" \
                  --url https://www.youtube.com/watch?v=x
"""

import argparse
import sys

try:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
except ImportError:
    sys.exit("needs python3-dbus and python3-gi (test-only dependency)")

PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
ROOT = "org.mpris.MediaPlayer2"
PROPS = "org.freedesktop.DBus.Properties"


class MockPlayer(dbus.service.Object):
    def __init__(self, bus_name, identity, title, artist, album, url, status):
        dbus.service.Object.__init__(self, bus_name, PATH)
        self.identity = identity
        self.metadata = dbus.Dictionary({
            "mpris:trackid": dbus.ObjectPath("/org/mpris/track/1"),
            "xesam:title": dbus.String(title),
            "xesam:artist": dbus.Array([dbus.String(artist)], signature="s"),
            "xesam:album": dbus.String(album),
        }, signature="sv")
        if url:
            self.metadata["xesam:url"] = dbus.String(url)
        self.status = status

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self.GetAll(interface).get(prop, "")

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == PLAYER:
            return dbus.Dictionary({
                "PlaybackStatus": dbus.String(self.status),
                "Metadata": self.metadata,
                "CanPlay": dbus.Boolean(True),
            }, signature="sv")
        if interface == ROOT:
            return dbus.Dictionary({
                "Identity": dbus.String(self.identity),
                "CanQuit": dbus.Boolean(True),
            }, signature="sv")
        return dbus.Dictionary({}, signature="sv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="testplayer")
    ap.add_argument("--identity", default="")
    ap.add_argument("--title", default="Test Song")
    ap.add_argument("--artist", default="Test Artist")
    ap.add_argument("--album", default="Test Album")
    ap.add_argument("--url", default="")
    ap.add_argument("--status", default="Playing")
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName("org.mpris.MediaPlayer2." + args.name, bus)
    MockPlayer(name, args.identity or args.name, args.title, args.artist,
               args.album, args.url, args.status)

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(int(args.seconds), loop.quit)
    print("mock player org.mpris.MediaPlayer2.%s up for %gs"
          % (args.name, args.seconds), flush=True)
    loop.run()


if __name__ == "__main__":
    main()
