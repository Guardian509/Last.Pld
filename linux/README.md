# Last.Pld for Linux

A port of the concept, not of the code. Windows reads the System Media
Transport Controls; Linux has the same idea in **MPRIS**, published over D-Bus
by essentially every media player and browser.

**One file.** Download `lastpld.py`, make it executable, run it. It opens a
window, sits in your tray, and sets itself to start when you log in — so it
keeps recording for as long as the machine is on and you are logged in.

    chmod +x lastpld.py
    ./lastpld.py

That is the whole install. There is no installer, no package, no service to
configure, and nothing is copied anywhere: the app is the file you downloaded.

**To uninstall, delete the file.** The autostart entry runs the file where it
sits, checks that it is still there, and removes itself at the next login if it
is not. Nothing is left running. Your history stays in
`~/.local/share/lastpld/` on purpose — deleting the app should not delete your
listening history. Remove that folder too if you want it gone.

## Dependencies

The recorder is **standard library only** — no pip, no venv, nothing to install
before the first run. D-Bus is reached through `busctl` (systemd) or `gdbus`
(glib); if you have a Linux desktop playing audio, you already have one.

The window and tray are optional extras, both distro packages, never pip:

| | |
|---|---|
| **Window** | PyGObject with GTK4 and libadwaita. Debian/Ubuntu: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`. Fedora: `python3-gobject gtk4 libadwaita`. |
| **Tray** | `python3-dbus`. Published as a StatusNotifierItem. On GNOME you also need the AppIndicator extension, which most distros ship enabled. |

**Without them the file still records** — it just has no face. It falls back to
terminal logging automatically, including when started from the autostart entry
at login, so logging in never silently does nothing.

## Running it

    ./lastpld.py            open the window and start recording
    ./lastpld.py --background   start in the tray only (what login runs)
    ./lastpld.py --no-gui       record in the terminal, Ctrl-C to stop
    ./lastpld.py --probe        show every MPRIS player and why it would or
                                would not be logged
    ./lastpld.py --selftest     run the built-in tests

`--probe` is the one to reach for when something isn't logged. It prints what
each player is publishing and the filter's verdict with its reasoning.

Closing the window hides it to the tray and recording continues — Esc does the
same. **Quit** in the tray menu stops recording, because with the recorder
inside the app there is no daemon left behind for it to mean anything else.
Use the **Logging** checkmark to stop recording but keep the window.

Autostart can be turned off from the tray menu, the window menu, or with
`--uninstall-autostart`.

For a headless box with no desktop at all, `--install-service` still writes a
systemd user unit the old way.

## Where things live

`~/.local/share/lastpld/` — override with `$LASTPLD_HOME`.

| File | What it is |
|---|---|
| `lastpld.csv` | History: `timestamp,title,artist,album,app`. |
| `lastpld.trash.csv` | Rows you moved to the trash, restorable from the window. |
| `sources.txt` | Which players to log. Created with defaults on first run. |
| `playlists.txt` | Spotify token. Written `0600`. |

The CSV is **byte-compatible with the Windows build** — same columns, same
UTF-8 BOM — so histories from both machines can be concatenated or swapped.

## The browser problem, and why Linux solves it better

Browsers publish a media session for *any* page with audio, so without a filter
Facebook and Airbnb show up as songs. Browsers are excluded by default, with
YouTube allowed back in.

On Windows that exception has to work by scraping visible window titles, which
means a YouTube video in a **background tab is never logged**, and on Wayland
it wouldn't work at all — reading other applications' window titles is
forbidden by design.

MPRIS gives a much better signal: `xesam:url` carries the actual page address,
so YouTube is recognised **by its domain**. Background tabs work, Wayland is
irrelevant, and lookalike domains like `youtube.com.evil.tld` are rejected
because the host is compared exactly rather than by substring.

Edit `sources.txt` to change any of it:

    allow spotify      log any player whose bus name contains "spotify"
    youtube            also log browser audio when the page URL is YouTube
    no-youtube         turn that off

If a browser publishes no `xesam:url` at all, it is not logged — the same
conservative default the Windows build takes.

## Identify (optional)

Fingerprints ~12s of system audio against Shazam, for sources that publish no
metadata — reels, games, streams.

    ./lastpld.py --identify

or the **Identify** button in the window. Needs a recorder (`pw-record`,
`parec`, or `ffmpeg`) and a recogniser. It prefers
[**SongRec**](https://github.com/marin-m/SongRec), which is native, packaged
for most distros, and needs no Python packages:

    sudo apt install songrec        # or dnf/pacman

`pip install shazamio` also works if you'd rather. **If neither is present the
feature is simply unavailable** — it never blocks recording, which is the whole
point of the no-dependency rule.
