"""Capture redacted InfiniDysk responses as test fixtures.

Usage (ideally while something is streaming):
    uv run python scripts/capture-infinidysk.py <label>      # e.g. streaming, idle

Reads INFINIDYSK_URL (frontend, :3000), INFINIDYSK_API_KEY, and optionally
INFINIDYSK_BACKEND_URL (:8080) and INFINIDYSK_INTERNAL_KEY (FRONTEND_BACKEND_API_KEY) from the
environment or from .dev-config/infinidysk.env. Writes tests/fixtures/infinidysk/<label>.json
in the same shape as constructed.json: provider usage, overview stats (1h, window+detail),
the nzbdav_* metrics, and ~12 s of WebSocket frames (topics ls + ar).

Only read-only requests are made (the same allowlist MediaHub uses). Redacted:

* provider hosts, nicknames and metrics keys -> provider<N>.example / "Provider N" / key-N,
  everywhere they appear (metrics labels, breakers, failure reasons, failover rows);
* what was streamed: file names, parent folders and paths -> "File N.<ext>", "Folder N",
  /content/file-N.<ext>; client IPs -> 192.0.2.N; indexer names -> "Indexer N".

Technical values (states, counters, sizes, timings, user agents) are kept as captured.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mediahub.integrations.infinidysk.integration import (  # noqa: E402
    OVERVIEW_PARAMS,
    assert_read_only,
    ws_url,
)

ENV_FILE = ROOT / ".dev-config" / "infinidysk.env"
OUT_DIR = ROOT / "tests" / "fixtures" / "infinidysk"
WS_SECONDS = 12


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values.setdefault(key.strip(), value.strip().strip("\"'"))
    if not values.get("INFINIDYSK_URL") or not values.get("INFINIDYSK_API_KEY"):
        sys.exit(f"Set INFINIDYSK_URL and INFINIDYSK_API_KEY (environment or {ENV_FILE})")
    return values


class Redactor:
    def __init__(self) -> None:
        self.replacements: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def add(self, original: Any, kind: str, template: str) -> str | None:
        if not isinstance(original, str) or not original.strip():
            return None
        if original not in self.replacements:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            suffix = Path(original).suffix if kind in ("file", "path") else ""
            suffix = suffix if 1 < len(suffix) <= 5 and suffix[1:].isalnum() else ""
            self.replacements[original] = template.format(n=self._counts[kind]) + suffix
        return self.replacements[original]

    def learn(self, usage: Any, overview: Any, frames: list[dict[str, Any]]) -> None:
        for p in usage.get("providers", []) if isinstance(usage, dict) else []:
            self.add(p.get("providerId"), "key", "key-{n}")
            self.add(p.get("host"), "host", "provider{n}.example")
            self.add(p.get("nickname"), "nick", "Provider {n}")
        for row in overview.get("providers", []) if isinstance(overview, dict) else []:
            self.add(row.get("provider"), "key", "key-{n}")
            self.add(row.get("nickname"), "nick", "Provider {n}")
        for kind in ("indexers", "indexerApiUsage"):
            for row in overview.get(kind, []) if isinstance(overview, dict) else []:
                self.add(row.get("name"), "indexer", "Indexer {n}")
        for f in frames:
            if f.get("Topic") != "ar":
                continue
            for read in json.loads(f["Message"]).get("reads", []):
                self.add(read.get("path"), "path", "/content/file-{n}")
                self.add(read.get("fileName"), "file", "File {n}")
                self.add(read.get("parentDirectoryName"), "folder", "Folder {n}")
                self.add(read.get("clientIp"), "ip", "192.0.2.{n}")
                for p in read.get("providers", []):
                    self.add(p.get("host"), "host", "provider{n}.example")
                    self.add(p.get("nickname"), "nick", "Provider {n}")

    def text(self, value: str) -> str:
        # Whole words only (a nickname "Backup" must not turn "BackupOnly" into "Provider 2Only"),
        # longest first so a path is replaced before the file name inside it.
        for original in sorted(self.replacements, key=len, reverse=True):
            pattern = rf"(?<![\w.]){re.escape(original)}(?![\w])"
            value = re.sub(pattern, lambda _m, o=original: self.replacements[o], value)
        # Bare IPv4 addresses (not version numbers like "Emby/4.9.1.90", not placeholders).
        return re.sub(
            r"(?<![\w/.])(?!192\.0\.2\.)(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", "192.0.2.0", value
        )

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


async def capture_ws(backend_url: str, key: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    async with connect(ws_url(backend_url), open_timeout=10, max_size=4 * 1024 * 1024) as ws:
        await ws.send(key)
        await ws.send(json.dumps({"sub": ["ls", "ar"]}))
        try:
            async with asyncio.timeout(WS_SECONDS):
                async for raw in ws:
                    frame = json.loads(raw)
                    # Connecting replays every state topic; keep only the subscribed ones.
                    if frame.get("Topic") in ("ls", "ar"):
                        frames.append(frame)
        except TimeoutError:
            pass
    return frames


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z0-9-]+", sys.argv[1]):
        sys.exit("Usage: capture-infinidysk.py <label>   (lowercase, digits, dashes)")
    label = sys.argv[1]
    env = load_env()
    url = env["INFINIDYSK_URL"].rstrip("/")
    backend = env.get("INFINIDYSK_BACKEND_URL", "").rstrip("/")
    headers = {"x-api-key": env["INFINIDYSK_API_KEY"]}

    def get(client: httpx.Client, path: str, params: dict[str, str] | None, *, back: bool) -> Any:
        assert_read_only(path, params, backend=back)
        response = client.get(path, params=params)
        response.raise_for_status()
        return response if back else response.json()

    with httpx.Client(base_url=url, headers=headers, timeout=30) as client:
        usage = get(client, "/api/get-provider-usage", None, back=False)
        overview = get(client, "/api/get-overview-stats", OVERVIEW_PARAMS, back=False)
    metrics, health, frames = "", {}, []
    if backend:
        with httpx.Client(base_url=backend, headers=headers, timeout=15) as client:
            metrics = "\n".join(
                line
                for line in get(client, "/metrics", None, back=True).text.splitlines()
                if line.startswith(("nzbdav_", "# HELP nzbdav_", "# TYPE nzbdav_"))
            )
            for path in ("/health", "/ready"):
                assert_read_only(path, None, backend=True)
                r = client.get(path)
                health[path] = {"status": r.status_code, "body": r.text[:200]}
        if env.get("INFINIDYSK_INTERNAL_KEY"):
            print(f"Recording the live feed for {WS_SECONDS} s…")
            frames = asyncio.run(capture_ws(backend, env["INFINIDYSK_INTERNAL_KEY"]))

    redact = Redactor()
    redact.learn(usage, overview, frames)
    fixture = {
        "_note": f"Captured with scripts/capture-infinidysk.py ({label}); redacted.",
        "provider_usage": redact(usage),
        "overview": redact(overview),
        "metrics": redact.text(metrics),
        "backend_health": health,
        "ws_frames": [
            {"Topic": f.get("Topic"), "Message": json.dumps(redact(json.loads(f["Message"])))}
            for f in frames
            if isinstance(f.get("Message"), str)
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{label}.json"
    out.write_text(json.dumps(fixture, indent=2) + "\n")
    topics: dict[str, int] = {}
    for f in frames:
        topics[str(f.get("Topic"))] = topics.get(str(f.get("Topic")), 0) + 1
    print(
        f"{len(usage.get('providers', []))} provider(s), overview sections "
        f"{overview.get('includedSections')}, {metrics.count(chr(10)) + bool(metrics)} metric "
        f"line(s), frames {topics or 'none'} -> {out}"
    )
    print("Review the file before committing: grep it for anything you recognise.")


if __name__ == "__main__":
    main()
