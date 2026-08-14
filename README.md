# Last.Pld

A local play-history recorder for Windows. It watches the System Media Transport
Controls (SMTC) session that media apps publish to, and appends every track
change to a CSV.

The point is to capture plays that the apps themselves don't record — notably
Apple Music radio stations, whose tracks never appear in Apple Music's own
History panel (which is only four entries deep and drops radio tracks entirely).

Single executable, no installer, no runtime to ship. It lives in the tray.

**One file, one double-click.** Run it once: it starts recording, sits in the
tray, and sets itself to start every time you log in. It keeps recording for as
long as the machine is on and you are logged in. To remove it, delete the exe —
there is nothing else to uninstall. (`Last.Pld.exe /uninstall` also clears the
logon entry, and **Start at login** in the tray menu toggles it.)

**Linux users:** see [`linux/`](linux/) — the same idea in one Python file,
built on MPRIS, writing the same CSV format. Standard library only: no pip, no
venv, nothing to install before the first run.

## Requirements

| | |
|---|---|
| **Core logging** | Windows 10/11. Nothing else — it builds against the C# compiler and .NET Framework 4.x that ship with Windows. |
| **Identify** (optional) | **Python 3.12** (see below). Audio capture and resampling are built into the exe. |
| **Playlists** (optional) | A free Spotify developer app. |

## Install

Grab `Last.Pld.exe` from Releases and run it. That is the whole install: no
installer, no admin rights, nothing written outside its own folder except one
per-user registry value for starting at logon.

It keeps its history **beside the exe**, so the folder you put it in is the
app. Somewhere like `%USERPROFILE%\Last.Pld\` works well; a folder that gets
cleaned out, like `Downloads`, does not.

Or build it yourself — the build needs no SDK, no Visual Studio, and no NuGet:

    git clone https://github.com/Guardian509/Last.Pld.git
    cd Last.Pld
    build.cmd

That produces `Last.Pld.exe` — self-contained, including the audio capture used
by Identify — and `loopcap.exe`, which is only a standalone tester for that
capture path (`loopcap.exe 12 test.wav`, then play the result). The app does
not need it at runtime.

### Optional: enable Identify

Identify fingerprints system audio against Shazam, for sources that publish no
metadata at all. Capture and the 16 kHz mono conversion happen inside the exe;
only the recogniser is external, and it needs a Python 3.12 environment:

    py -3.12 -m venv _build\venv312
    _build\venv312\Scripts\pip install -r requirements.txt

**It must be 3.12.** `shazamio-core`'s 3.14 wheel segfaults (`0xC0000005`) at
import, and on 3.13+ `pydub` additionally needs `audioop-lts`, since PEP 594
removed the stdlib `audioop` module. The app looks for the interpreter at
`_build\venv312\Scripts\python.exe` and simply hides the feature's failure
behind a clear message if it isn't there.

## Files

| File | What it is |
|---|---|
| `Last.Pld.exe` | The app. ~40 KB, WinForms, .NET Framework 4.x (in-box on Windows). |
| `Last.Pld.cs` | Logging, filtering and the UI. |
| `Playlists.cs` | Spotify/Apple Music targets, OAuth, settings storage. |
| `build.cmd` | Rebuilds both exes with the in-box C# compiler. No SDK needed. |
| `requirements.txt` | Python deps for Identify. |
| `lastpld.csv` | The history: `timestamp,title,artist,album,app`. UTF-8 with BOM. |
| `lastpld.trash.csv` | Deleted rows, with an extra leading `deleted` timestamp. |
| `sources.txt` | Which apps to log. Edit and restart to apply. |
| `startup.log` | One line per launch, so "did it start at logon?" is answerable. |
| `LoopCap.cs` | WASAPI loopback capture, for Identify. Compiled into `Last.Pld.exe`; also builds standalone as `loopcap.exe` for testing capture on its own. |
| `recognize.py` | Fingerprints a clip via Shazam. Needs `_build\venv312`. |
| `playlists.txt` | Playlist settings. Created on first connect; secrets DPAPI-encrypted. |
| `tests/` | Test suite. Run it with `test.cmd`. |
| `diagnostics/` | Probes kept from debugging, plus `check_startup.ps1`. Not needed to run. |

## Running

    Last.Pld.exe              open the history window (and start logging)
    Last.Pld.exe /background  start in the tray only, no window

Launching it again while it's already running surfaces the existing window
rather than starting a second copy.

### Start it at logon

Already done — the first run registers it under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, which needs no elevation
and affects only your account. Later runs correct the stored path, so moving the
exe and running it once is enough to keep it pointing at the right place.

    Last.Pld.exe /uninstall   stop starting at logon

**Start at login** in the tray menu toggles the same thing. Deleting the exe
stops it too, since Windows skips a Run entry whose target is missing; that
leaves one inert registry value, which `/uninstall` clears.

## The window

- **Search** filters on title, artist and album. `Ctrl+F` jumps to it.
- **Source** filters by app.
- **Del** moves the selected rows to the trash — no confirmation, because it's
  reversible. **Trash (n)** toggles the trash view, where double-click or
  right-click restores. *Delete permanently* and *Empty trash* do confirm.
- **Esc** clears the search, or drops the window back to the tray if it's empty.
- Double-clicking a row searches for it on Apple Music.
- Closing the window hides it. Logging continues. Exit is on the tray menu.

## sources.txt

Browsers publish a media session for *any* page with audio, so without a filter
Facebook and Airbnb show up in your history as songs. The file is an allowlist:

    allow spotify      log any app whose id contains "spotify"
    youtube            also log browser audio when a YouTube window matches
    no-youtube         turn that off

Browsers are excluded by default. YouTube is allowed back in by a window-title
check: some visible window must contain both "YouTube" and the first 20
characters of the track, so an unrelated YouTube tab can't wave Facebook
through. No SMTC field distinguishes them — Facebook reports
`PlaybackType = Music` exactly like Spotify does, so this is the only signal
that works.

**Known limitation:** YouTube playing in a *background* tab doesn't set the
window title, so it isn't logged. Pre-roll ads are skipped for the same reason,
which is a happy accident.

## Identify

For audio that publishes no metadata at all — TikTok / Instagram / Facebook
reels, games, streams — the **Identify** button records ~12 seconds of system
audio, downsamples it to 16 kHz mono, and asks Shazam what it is. A match is
appended to the history tagged `App = Shazam`.

Only a fingerprint is uploaded, never the recording.

Requires the Python environment in `_build\venv312`. That
venv **must be Python 3.12** — `shazamio-core`'s 3.14 wheel segfaults at import.

Reel audio is often sped up or pitch-shifted, which defeats fingerprint matching
regardless of backend. A "no match" there is expected, not a bug.

## Sending identified songs to a playlist

Tray icon → **Playlists**. Tick *Add identified songs to a playlist*, then
connect a service. Every Identify hit is then pushed to a playlist called
**Last.Pld** in that account.

There is deliberately no "log in with Shazam" — no such thing exists. Shazam has
no public account API, and the recognizer talks to Shazam's internal endpoint
anonymously. The login belongs to the destination service.

### Spotify — supported, free

Spotify only lets an account's *own* app write to its playlists, so you need a
free developer app of your own. The setup dialog walks through it:

1. developer.spotify.com/dashboard → **Create app**
2. Redirect URI: `http://127.0.0.1:8888/callback`
3. Tick **Web API**, save, copy the **Client ID** into the dialog

The redirect **must** be the loopback literal `127.0.0.1`, not `localhost` —
Spotify stopped accepting `localhost`, and plain http is only permitted for
loopback addresses. Change the port with `spotify.redirect_port` in
`playlists.txt` if 8888 is taken (and update it in the dashboard to match).

Auth is Authorization Code + **PKCE**, so there is no client secret to store.
The refresh token is encrypted with DPAPI against your Windows account, so
`playlists.txt` is useless if copied to another machine.

### Apple Music — not enabled, and it's a paywall not a bug

Every Apple Music API request needs a *developer token*: a JWT signed with a
MusicKit key. Creating that key **requires a paid Apple Developer Program
membership** ($99/year) — a free Apple ID cannot make one. The code is stubbed
behind the same interface with the exact endpoint written out, so it's a short
job if that membership ever exists.

Note that the Shazam *app* already syncs its own recognitions into a "My Shazam
Tracks" playlist. That covers phone Shazams only; Last.Pld's desktop
recognitions never touch your account, so it can't substitute.

### How tracks are matched

Shazam's response carries an **ISRC** and an exact **Apple Music catalog id**,
so nothing here is guesswork. The one wrinkle is Spotify: Shazam's `SPOTIFY`
provider entry is only a *search deeplink* (`spotify:search:Take%20On%20Me…`),
never a track URI. So the ISRC is resolved through Spotify's own search
(`q=isrc:…`), which is an exact lookup; title/artist is the fallback for tracks
with no ISRC. ISRCs are normalised to bare uppercase first, because Spotify
silently returns nothing for lowercase or dashed forms.

## Building

Run `build.cmd`. It uses `csc.exe` from `C:\Windows\Microsoft.NET\Framework64`,
which ships with Windows — there is no .NET SDK or Windows Kit dependency.

Run the tests with `test.cmd`. They cover the settings/DPAPI round trip, the
OAuth loopback listener (including CSRF state rejection), Spotify response
parsing, and reachability of the live Spotify endpoints — stopping at the auth
boundary, since past it needs a signed-in account.

`diagnostics/` holds the probes used to work the SMTC behaviour out in the first
place. `DumpProps.cs` is the useful one: build and run it to print every field
Windows is publishing for whatever is playing, which answers most "why wasn't
this logged?" questions in one shot.

Two things are load-bearing and easy to get wrong:

- `System.Runtime.InteropServices.WindowsRuntime.dll` **must** be referenced, or
  binding `+=` to the SMTC events fails with a misleading CS1545.
- `System.Runtime.WindowsRuntime.dll` must **not** be, and `.AsTask()` must not
  be used — both pull in SDK union metadata that isn't present on a stock
  machine. The code spins on `IAsyncOperation.Status` instead.
