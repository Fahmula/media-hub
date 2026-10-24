"""Capture redacted Sonarr/Radarr API responses as test fixtures.

Usage (ideally while something is downloading, stuck or failed):
    uv run python scripts/capture-arr.py sonarr <label>     # e.g. downloading, import-blocked
    uv run python scripts/capture-arr.py radarr <label>

Reads SONARR_URL/SONARR_API_KEY or RADARR_URL/RADARR_API_KEY from the environment or from
.dev-config/arr.env. Writes tests/fixtures/<app>/<label>.json holding system/status, health,
queue, queue/status and recent history. Nothing is sent anywhere except the configured server,
and the output is safe to commit:

* masked: API keys, URLs that can embed indexer keys (download/info URLs, guids), torrent
  hashes / download ids (consistently), IPs, provider ids (tvdb/tmdb/imdb...)
* anonymized: what was downloaded. Series/movie/episode titles become "Series 1", "Movie 1",
  "Episode 1"; release names "Release 1"; indexers "Indexer 1"; release groups "Group 1";
  paths "/redacted/file.<ext>". Any of those originals that also appear inside free text
  (status messages, health messages) are replaced too. Overviews, genres, images' remote
  URLs, alternate titles, ratings and credits are emptied. Technical fields (states,
  statuses, sizes, timeleft, quality, protocol, client, event types) are kept as captured.
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
ENV_FILE = ROOT / ".dev-config" / "arr.env"
FIXTURES = ROOT / "tests" / "fixtures"

APPS: dict[str, dict[str, str]] = {
    "sonarr": {
        "queue": "includeUnknownSeriesItems=true&includeSeries=true&includeEpisode=true",
        "history": "includeSeries=true&includeEpisode=true",
    },
    "radarr": {
        "queue": "includeUnknownMovieItems=true&includeMovie=true",
        "history": "includeMovie=true",
    },
}

SECRET_KEY = re.compile(r"token|apikey|api_key|password|secret|guid|url$", re.IGNORECASE)
SAFE_URL_KEYS = {"url", "wikiUrl"}  # images[].url is a local /MediaCover/<id>/... path
HASH_KEYS = {"downloadId", "torrentInfoHash", "infoHash"}
PROVIDER_ID_KEYS = re.compile(r"^(tvdb|tmdb|imdb|tvRage|tvMaze|mal|aniList)Ids?$", re.IGNORECASE)
PATH_KEYS = {
    "path",
    "outputPath",
    "rootFolderPath",
    "folder",
    "folderName",
    "relativePath",
    "droppedPath",
    "importedPath",
    "appData",
    "startupPath",
    "sourcePath",
    "destinationPath",
}
CLEAR_KEYS = {
    "overview",
    "genres",
    "alternateTitles",
    "ratings",
    "credits",
    "certification",
    "network",
    "studio",
    "website",
    "youTubeTrailerId",
    "collection",
    "keywords",
    "images",  # remoteUrl identifies the title; mediacover ids are enough for tests
    "titleSlug",
    "cleanTitle",
    "sortTitle",
    "originalTitle",
    "secondaryYearSourceId",
    "imdbRating",
    "popularity",
}
# Key -> placeholder category. "title" depends on the containing object (see Redactor.walk).
LABEL_KEYS: dict[str, str | None] = {
    "seriesTitle": "Series",
    "movieTitle": "Movie",
    "sourceTitle": "Release",
    "releaseTitle": "Release",
    "sceneName": "Release",
    "indexer": "Indexer",
    "releaseGroup": "Group",
    "instanceName": None,  # replaced by the app name
}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ABS_PATH = re.compile(r"(?<![\w:/])(?:/[^\s/'\"(),]+){2,}/?")
# Health messages name titles inline: "Movie <title> (tmdbid 123) was removed from TMDb",
# "Series <a> (tvdbid 1), <b> (tvdbid 2) were removed from TheTVDB".
INLINE_TITLE = re.compile(
    r"(^(?:Movies?|Series) |, )([^,()]+?) \((tmdb|tvdb|imdb)id [\w]+\)", re.IGNORECASE
)


def load_env(app: str) -> tuple[str, str]:
    values = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    prefix = app.upper()
    url, key = values.get(f"{prefix}_URL", ""), values.get(f"{prefix}_API_KEY", "")
    if not url or not key:
        sys.exit(f"Set {prefix}_URL and {prefix}_API_KEY (environment or {ENV_FILE})")
    return url.rstrip("/"), key


def file_placeholder(value: str, base: str = "/redacted/file") -> str:
    suffix = Path(value).suffix
    return base + (suffix if 1 < len(suffix) <= 5 and suffix[1:].isalnum() else "")


class Redactor:
    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self.labels: dict[tuple[str, str], str] = {}
        self.hashes: dict[str, str] = {}
        self.placeholders: set[str] = set()

    def label(self, category: str, original: str) -> str:
        if not original:
            return original
        count = sum(1 for c, _ in self.labels if c == category)
        return self.labels.setdefault((category, original), f"{category} {count + 1}")

    def title_category(self, obj: dict[str, Any]) -> str:
        if "episodeNumber" in obj or "episodeFileId" in obj:
            return "Episode"
        if "seasons" in obj or "seriesType" in obj:
            return "Series"
        if "tmdbId" in obj or "movieFile" in obj or "hasFile" in obj:
            return "Movie"
        return "Release"  # queue rows: "title" is the release name

    def walk(self, value: Any, key: str = "", parent: dict[str, Any] | None = None) -> Any:
        if key in CLEAR_KEYS:
            return type(value)() if isinstance(value, str | list | dict) else None
        if isinstance(value, dict):
            return {k: self.walk(v, k, value) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, key, parent) for v in value]
        if PROVIDER_ID_KEYS.match(key):
            return "tt0000000" if isinstance(value, str) else 0
        if not isinstance(value, str):
            return value
        if key in HASH_KEYS:
            return self.hashes.setdefault(value, f"DOWNLOAD{len(self.hashes) + 1}")
        if key in SAFE_URL_KEYS:
            return IPV4.sub("192.0.2.1", value)
        if SECRET_KEY.search(key):
            return "REDACTED" if value else value
        if key in PATH_KEYS:
            return file_placeholder(value) if value else value
        if key == "title" and parent is not None:
            return self.label(self.title_category(parent), value)
        if key in LABEL_KEYS:
            category = LABEL_KEYS[key]
            return self.app_name if category is None else self.label(category, value)
        if key.endswith("ersion"):  # "4.0.15.2941" looks like an IPv4 address
            return value
        return self.scrub_text(value)

    def scrub_text(self, value: str) -> str:
        value = INLINE_TITLE.sub(
            lambda m: f"{m.group(1)}{self.label('Title', m.group(2))} ({m.group(3)}id 0)", value
        )
        value = ABS_PATH.sub(lambda m: file_placeholder(m.group(0)), value)
        return IPV4.sub("192.0.2.1", value)

    def patterns(self) -> list[tuple[re.Pattern[str], str]]:
        """Originals to hunt for in free text, longest first.

        Titles match loosely ("The Bear" also catches "The.Bear" and "the_bear") because
        release and file names in status messages spell them that way.
        """
        out: list[tuple[str, re.Pattern[str], str]] = []
        for (category, orig), ph in self.labels.items():
            words = re.findall(r"[^\W_]+", orig.replace("'", "").replace("’", ""))
            if category in ("Series", "Movie", "Episode") and len("".join(words)) >= 3:
                body = r"[\W_]+".join("['\u2019]?".join(map(re.escape, w)) for w in words)
                pattern = re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.IGNORECASE)
                out.append((orig, pattern, ph.replace(" ", "")))
            elif len(orig) >= 4:
                out.append((orig, re.compile(re.escape(orig)), ph))
        out += [(o, re.compile(re.escape(o)), ph) for o, ph in self.hashes.items() if len(o) >= 8]
        out.sort(key=lambda p: -len(p[0]))
        return [(pattern, ph) for _, pattern, ph in out]

    def replace_originals(self, value: Any, patterns: list[tuple[re.Pattern[str], str]]) -> Any:
        """Second pass: originals that leaked into free text (status messages...)."""
        if isinstance(value, dict):
            return {k: self.replace_originals(v, patterns) for k, v in value.items()}
        if isinstance(value, list):
            return [self.replace_originals(v, patterns) for v in value]
        if not isinstance(value, str) or value in self.placeholders:
            return value
        for pattern, ph in patterns:
            value = pattern.sub(ph, value)
        return value

    def __call__(self, value: Any) -> Any:
        walked = self.walk(value)
        self.placeholders = {*self.labels.values(), *self.hashes.values(), self.app_name}
        return self.replace_originals(walked, self.patterns())


def main() -> None:
    if (
        len(sys.argv) != 3
        or sys.argv[1] not in APPS
        or not re.fullmatch(r"[a-z0-9-]+", sys.argv[2])
    ):
        sys.exit("Usage: capture-arr.py sonarr|radarr <label>   (lowercase, digits, dashes)")
    app, label = sys.argv[1], sys.argv[2]
    url, key = load_env(app)
    includes = APPS[app]
    with httpx.Client(base_url=url, headers={"X-Api-Key": key}, timeout=15) as client:

        def get(path: str) -> Any:
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        raw = {
            "system_status": get("/api/v3/system/status"),
            "health": get("/api/v3/health"),
            "queue": get(f"/api/v3/queue?page=1&pageSize=200&{includes['queue']}"),
            "queue_status": get("/api/v3/queue/status"),
            "history": get(
                "/api/v3/history?page=1&pageSize=20&sortKey=date&sortDirection=descending"
                f"&{includes['history']}"
            ),
        }

    app_name = str(raw["system_status"].get("appName") or app.title())
    if app_name.lower() != app:
        sys.exit(f"{url} is {app_name}, not {app.title()}")
    fixture = Redactor(app_name)(raw)
    out_dir = FIXTURES / app
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{label}.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")

    records = raw["queue"].get("records", [])
    print(
        f"{app_name} {raw['system_status'].get('version')}: {len(records)} queue row(s), "
        f"{len(raw['history'].get('records', []))} history event(s), "
        f"{len(raw['health'])} health check(s) -> {out}"
    )
    for row in records:
        print(
            f"  status={row.get('status')} tracked={row.get('trackedDownloadState')}/"
            f"{row.get('trackedDownloadStatus')} protocol={row.get('protocol')} "
            f"client={row.get('downloadClient')} timeleft={row.get('timeleft')}"
        )
    print("Review the file before committing: grep it for anything you recognise.")


if __name__ == "__main__":
    main()
