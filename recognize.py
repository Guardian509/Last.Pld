"""Identify a song from an audio file using Shazam's backend.

Prints a single line of JSON so the caller (Last.Pld) can parse it trivially.
Only an audio fingerprint leaves this machine, never the raw recording.

Usage:  recognize.py <audiofile>
"""

import asyncio
import json
import sys

from shazamio import Shazam


async def identify(path):
    shazam = Shazam()
    result = await shazam.recognize(path)

    track = (result or {}).get("track")
    if not track:
        return {"matched": False}

    sections = track.get("sections") or []
    meta = {}
    for section in sections:
        for item in section.get("metadata") or []:
            if item.get("title") and item.get("text"):
                meta[item["title"]] = item["text"]

    hub = track.get("hub") or {}

    # Exact Apple Music catalog id. It is the "applemusicplay" action's id --
    # the same number that shows up as "i=" in the deeplink below.
    apple_track_id = ""
    for action in hub.get("actions") or []:
        if action.get("type") == "applemusicplay" and action.get("id"):
            apple_track_id = str(action["id"])
            break

    apple_music_url = ""
    for option in hub.get("options") or []:
        for action in option.get("actions") or []:
            if action.get("type") == "uri" and "music.apple.com" in (action.get("uri") or ""):
                apple_music_url = action["uri"]
                break
        if apple_music_url:
            break

    # Shazam's SPOTIFY provider is only a search deeplink, never a track URI --
    # spotify:search:Take%20On%20Me%20a-ha. Kept for reference; the caller
    # resolves the ISRC through Spotify's own search instead.
    spotify_search = ""
    for provider in hub.get("providers") or []:
        if (provider.get("type") or "").upper() == "SPOTIFY":
            for action in provider.get("actions") or []:
                if action.get("uri"):
                    spotify_search = action["uri"]
                    break
            break

    return {
        "matched": True,
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "album": meta.get("Album"),
        "released": meta.get("Released"),
        "label": meta.get("Label"),
        "url": track.get("url"),
        "isrc": track.get("isrc") or "",
        "apple_track_id": apple_track_id,
        "apple_music_url": apple_music_url,
        "spotify_search": spotify_search,
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"matched": False, "error": "no input file"}))
        return 2
    try:
        out = asyncio.run(identify(sys.argv[1]))
    except Exception as exc:  # network down, bad audio, API change
        out = {"matched": False, "error": "{}: {}".format(type(exc).__name__, exc)}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("matched") else 1


if __name__ == "__main__":
    sys.exit(main())
