# Last.Pld for Linux

A port of the concept, not of the code. Windows reads the System Media
Transport Controls; Linux has the same idea in **MPRIS**, published over D-Bus
by essentially every media player and browser.

One file, `lastpld.py`. **Standard library only** — no pip, no venv, nothing to
install before the first run. D-Bus is reached through `busctl` (ships with
systemd) or `gdbus` (ships with glib); if you have a Linux desktop playing
audio, you already have one.

    ./lastpld.py            start logging (Ctrl-C to stop)
    ./lastpld.py --probe    show every MPRIS player and why it would or
                            would not be logged
    ./lastpld.py --selftest run the built-in tests

`--probe` is the one to reach for when something isn't logged. It prints what
each player is publishing and the filter's verdict with its reasoning.

## Where things live

`~/.local/share/lastpld/` — override with `$LASTPLD_HOME`.

| File | What it is |
|---|---|
| `lastpld.csv` | History: `timestamp,title,artist,album,app`. |
| `sources.txt` | Which players to log. Created with defaults on first run. |
| `playlists.txt` | Spotify token. Written `0600`. |

The CSV is **byte-compatible with the Windows build** — same columns, same
UTF-8 BOM — so histories from both machines can be concatenated or swapped.

## Run it at login

    ./lastpld.py --install-service

Writes and enables `~/.config/systemd/user/lastpld.service`. Check it with
`systemctl --user status lastpld`.

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

Needs a recorder (`pw-record`, `parec`, or `ffmpeg`) and a recogniser. It
prefers [**SongRec**](https://github.com/marin-m/SongRec), which is native,
packaged for most distros, and needs no Python packages:

    sudo apt install songrec        # or dnf/pacman

`pip install shazamio` also works if you'd rather. **If neither is present the
feature is simply unavailable** — it never blocks logging, which is the whole
point of the no-dependency rule.

(Pleasing footnote: SongRec is why this is easier here. It's the tool that
couldn't be built on Windows — no binary, and its Rust dependencies need the
full MSYS2 stack — which is what sent the Windows build down the shazamio road
in the first place.)

## Playlists (optional)

    ./lastpld.py --connect-spotify --client-id <YOUR_CLIENT_ID>

Identified songs then get pushed to a playlist called **Last.Pld**. You need a
free app from the [Spotify dashboard](https://developer.spotify.com/dashboard)
with redirect URI `http://127.0.0.1:8888/callback` — **it must be the loopback
literal, not `localhost`**, which Spotify stopped accepting.

Auth is Authorization Code + PKCE, so there's no client secret. Matching uses
the ISRC from the recognition, which is an exact lookup rather than a fuzzy
title search.

Unlike the Windows build there's no DPAPI here, so the token is protected by
file permissions (`0600`) instead — libsecret would mean a third-party module.

## Differences from the Windows build

| | Windows | Linux |
|---|---|---|
| Source | SMTC (WinRT) | MPRIS (D-Bus) |
| Detection | event-driven, 1–25 ms | 1 s poll |
| UI | WinForms window + tray | CLI (`--history`, `--search`) |
| YouTube check | window titles | `xesam:url` domain — better |
| Token storage | DPAPI | file mode `0600` |
| Autostart | Scheduled Task | systemd user service |

## Testing

    ./lastpld.py --selftest          26 checks, no D-Bus needed

For a genuine end-to-end run, `tests/mock_mpris.py` publishes a fake player on
the real session bus:

    python3 tests/mock_mpris.py --name testplayer --title "Song" --artist "Band" &
    ./lastpld.py --probe

That fixture is the only thing here that needs `python3-dbus` — a test-only
dependency the app itself never has.
