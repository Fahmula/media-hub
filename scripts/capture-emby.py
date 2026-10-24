"""Capture redacted Emby API responses as test fixtures.

Usage (play something first, then):
    uv run python scripts/capture-emby.py <label>     # e.g. direct-play, transcode-hdr

Reads EMBY_URL and EMBY_API_KEY from the environment or from .dev-config/emby.env. Writes
tests/fixtures/emby/<label>.json holding /System/Info/Public, /Sessions (active ones) and the
item detail for each playing item. Nothing is ever sent anywhere except the configured Emby
server, and the output is safe to commit:

* masked: client IPs, device ids and names, tokens, user names, the server name
* anonymized: what was watched. Titles become "Movie 1", "Series 1", "Channel 1"...
  (consistently across one capture), file names become "file.<ext>", and overviews, cast,
  studios, taglines, provider ids and external links are emptied. Technical fields (codecs,
  HDR, pipelines, bitrates, ticks, image tags) are kept as captured.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".dev-config" / "emby.env"
OUT_DIR = ROOT / "tests" / "fixtures" / "emby"

SECRET_KEY = re.compile(r"token|apikey|api_key|password|secret", re.IGNORECASE)
MASK_KEYS = {
    "DeviceId",
    "RemoteEndPoint",
    "LocalAddress",
    "LocalAddresses",
    "WanAddress",
    "RemoteAddresses",
}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Item dicts (NowPlayingItem, item details, CurrentProgram) are recognised by "MediaType".
# Name-like fields get a placeholder by category; same original -> same placeholder.
TYPE_LABELS = {
    "Movie": "Movie",
    "Episode": "Episode",
    "TvChannel": "Channel",
    "Program": "Program",
    "Audio": "Track",
    "Series": "Series",
}
ITEM_NAME_KEYS = {"Name", "SortName", "ForcedSortName", "OriginalTitle"}
ITEM_LABEL_KEYS = {
    "SeriesName": "Series",
    "ChannelName": "Channel",
    "EpisodeTitle": "Episode",
    "Album": "Album",
    "AlbumArtist": "Artist",
}
ITEM_CLEAR_KEYS = {
    "Overview",
    "Taglines",
    "People",
    "Studios",
    "ExternalUrls",
    "ProviderIds",
    "RemoteTrailers",
    "TagItems",
    "Artists",
    "ArtistItems",
    "AlbumArtists",
}


def load_env() -> tuple[str, str]:
    values = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    url, key = values.get("EMBY_URL", ""), values.get("EMBY_API_KEY", "")
    if not url or not key:
        sys.exit(f"Set EMBY_URL and EMBY_API_KEY (environment or {ENV_FILE})")
    root = url.rstrip("/")
    if not root.lower().endswith("/emby"):
        root += "/emby"
    return root, key


class Redactor:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.devices: dict[str, str] = {}
        self.labels: dict[tuple[str, str], str] = {}

    def label(self, category: str, original: str) -> str:
        if not original:
            return original
        count = sum(1 for c, _ in self.labels if c == category)
        return self.labels.setdefault((category, original), f"{category} {count + 1}")

    def item(self, value: dict[str, Any]) -> dict[str, Any]:
        category = TYPE_LABELS.get(value.get("Type", ""), "Item")
        name = value.get("Name") if isinstance(value.get("Name"), str) else ""
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in ITEM_NAME_KEYS and isinstance(v, str):
                out[k] = self.label(category, name or v)
            elif k in ITEM_LABEL_KEYS and isinstance(v, str):
                out[k] = self.label(ITEM_LABEL_KEYS[k], v)
            elif k in ITEM_CLEAR_KEYS:
                out[k] = type(v)() if isinstance(v, list | dict | str) else None
            elif k == "PresentationUniqueKey" and isinstance(v, str):
                out[k] = "REDACTED"  # embeds provider ids
            elif k in ("Chapters", "MediaSources") and isinstance(v, list):
                # MediaSources names are release titles ("Title (2026) [tmdb-1] [WEBDL]-Group").
                prefix = "Chapter" if k == "Chapters" else "Source"
                out[k] = [
                    {**self(c), "Name": f"{prefix} {i + 1}"} if isinstance(c, dict) else c
                    for i, c in enumerate(v)
                ]
            else:
                out[k] = self(v, k)
        return out

    def __call__(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            if "MediaType" in value:
                return self.item(value)
            return {k: self(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v, key) for v in value]
        if not isinstance(value, str):
            return value
        if SECRET_KEY.search(key) or key in MASK_KEYS:
            return "REDACTED" if value else value
        if key == "UserName":
            return self.users.setdefault(value, f"user{len(self.users) + 1}")
        if key == "DeviceName":  # often "<owner>'s phone"
            return self.devices.setdefault(value, f"Device {len(self.devices) + 1}")
        if key == "ServerName":
            return "Emby"
        if key in ("Path", "FileName"):
            suffix = Path(value).suffix if len(Path(value).suffix) <= 5 else ""
            return ("/redacted/file" if key == "Path" else "file") + suffix
        if key.endswith("Version"):  # "4.11.0.9" looks like an IPv4 address
            return value
        return IPV4.sub("192.0.2.1", value)


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z0-9-]+", sys.argv[1]):
        sys.exit("Usage: capture-emby.py <label>   (lowercase letters, digits, dashes)")
    label = sys.argv[1]
    root, key = load_env()
    redact = Redactor()
    with httpx.Client(base_url=root, headers={"X-Emby-Token": key}, timeout=10) as client:

        def get(path: str) -> Any:
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        public = get("/System/Info/Public")
        sessions = [s for s in get("/Sessions") if s.get("NowPlayingItem")]
        items = {}
        for session in sessions:
            item, user = session["NowPlayingItem"], session.get("UserId")
            if user and item.get("Id") and item["Id"] not in items:
                items[item["Id"]] = get(f"/Users/{user}/Items/{item['Id']}")

    fixture = {
        "system_info_public": redact(public),
        "sessions": redact(sessions),
        "items": redact(items),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{label}.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    print(f"{len(sessions)} active session(s), Emby {public.get('Version')} -> {out}")
    for s in sessions:
        play = s.get("PlayState", {})
        trans = s.get("TranscodingInfo") or {}
        print(
            f"  {s['NowPlayingItem'].get('Name')!r} on {s.get('Client')}: "
            f"{play.get('PlayMethod')} paused={play.get('IsPaused')} "
            f"video_direct={trans.get('IsVideoDirect')} reasons={trans.get('TranscodeReasons')}"
        )


if __name__ == "__main__":
    main()
