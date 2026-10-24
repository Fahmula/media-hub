"""Live feed from InfiniDysk's backend WebSocket (``/ws``).

Protocol (InfiniDysk ``backend/Websocket/WebsocketManager.cs``): the first text frame is the
internal ``FRONTEND_BACKEND_API_KEY`` (a wrong key is closed with 1008 "Unauthorized" within
5 s), then ``{"sub": [topics]}``. Every frame after that is
``{"Topic": "<name>", "Message": "<string>"}``. On connect InfiniDysk replays the last message
of *every* state topic, subscribed or not, and some of those aren't JSON (``cxs`` is
``key|...``), so frames are filtered by topic first. We subscribe to ``ls`` (live tiles,
every 5 s; JSON) and ``ar`` (active reads as JSON, sent only when something changed).

InfiniDysk drops a read 15 s after its last byte, but players buffer ahead: seen live, Emby
(STRM, ffmpeg) buffered ~850 MB in the first minute and the read vanished while playback went
on. So dropped reads stay listed as idle for ``IDLE_LINGER_S`` after their last activity,
until the same file is read again (InfiniDysk then starts a new read id).

The feed only runs while someone is looking (started by full polls, stopped by idle ones)
and reconnects with backoff. Nothing here is ever sent upstream except the key and the
subscription.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from mediahub.integrations.http import USER_AGENT
from mediahub.integrations.infinidysk.mapper import LiveState, map_read
from mediahub.logging import get_logger

log = get_logger(__name__)

TOPICS = ("ls", "ar")
TILES_MAX_AGE_S = 15.0  # ``ls`` comes every 5 s; older means the feed is stuck
SPEED_WINDOW_S = 10.0
IDLE_READ_S = 5.0  # no bytes for this long: report 0 B/s rather than the last rate
IDLE_LINGER_S = 600.0
MAX_FRAME_BYTES = 4 * 1024 * 1024
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)


def describe_close(code: int | None, reason: str | None) -> str:
    if code == 1008:
        return "Internal key rejected by InfiniDysk"
    if code is None:
        return "Connection closed"
    return f"Connection closed ({code}{f': {reason}' if reason else ''})"


class LiveFeed:
    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key
        self._task: asyncio.Task[None] | None = None
        self.connected = False
        self.error: str | None = None
        self._tiles: dict[str, Any] | None = None
        self._tiles_at = 0.0
        self._reads: list[dict[str, Any]] = []
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        # Reads InfiniDysk dropped: id -> (last raw read, wall-clock time it was dropped).
        self._idle: dict[str, tuple[dict[str, Any], float]] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="infinidysk-live-feed")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reset()

    def _reset(self) -> None:
        self.connected = False
        self._tiles = None
        self._reads = []
        self._samples.clear()
        self._idle.clear()

    def state(self) -> LiveState:
        now = time.monotonic()
        tiles = self._tiles if now - self._tiles_at <= TILES_MAX_AGE_S else None
        reads = [map_read(raw, self._speed(raw, now)) for raw in self._reads]
        wall = time.time()
        self._idle = {
            read_id: (raw, dropped_at)
            for read_id, (raw, dropped_at) in self._idle.items()
            if wall - _last_activity(raw, dropped_at) <= IDLE_LINGER_S
        }
        reads += [map_read(raw, 0.0, idle=True) for raw, _ in self._idle.values()]
        return LiveState(connected=self.connected, error=self.error, tiles=tiles, reads=reads)

    # ----- connection --------------------------------------------------------------------
    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                async with ws_connect(
                    self._url,
                    open_timeout=10,
                    close_timeout=2,
                    max_size=MAX_FRAME_BYTES,
                    user_agent_header=USER_AGENT,
                ) as ws:
                    await ws.send(self._key)
                    await ws.send(json.dumps({"sub": list(TOPICS)}))
                    async for frame in ws:
                        if not self.connected:  # the key was accepted: frames only follow then
                            self.connected, self.error, failures = True, None, 0
                        self.handle(frame)
                    # Closed cleanly by InfiniDysk (e.g. it restarted).
                    self.error = describe_close(ws.close_code, ws.close_reason)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                rcvd = exc.rcvd
                self.error = describe_close(
                    rcvd.code if rcvd else None, rcvd.reason if rcvd else None
                )
            except InvalidStatus as exc:
                self.error = f"WebSocket refused (HTTP {exc.response.status_code})"
            except (InvalidHandshake, OSError, TimeoutError):
                self.error = "Could not open the WebSocket on the backend URL"
            except Exception:
                log.exception("infinidysk_feed_failed")
                self.error = "Live feed failed (see MediaHub logs)"
            self._reset()
            delay = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    # ----- frames ------------------------------------------------------------------------
    def handle(self, frame: str | bytes) -> None:
        try:
            envelope = json.loads(frame)
            topic, message = envelope.get("Topic"), envelope.get("Message")
            if topic not in TOPICS or not isinstance(message, str):
                return
            payload = json.loads(message)
        except (ValueError, AttributeError):
            return
        if not isinstance(payload, dict):
            return
        if topic == "ls":
            self._tiles, self._tiles_at = payload, time.monotonic()
        elif topic == "ar" and isinstance(payload.get("reads"), list):
            self._set_reads([r for r in payload["reads"] if isinstance(r, dict)])

    def _set_reads(self, reads: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        seen = set()
        for raw in reads:
            read_id = str(raw.get("id", ""))
            seen.add(read_id)
            bytes_read = raw.get("bytesRead")
            if isinstance(bytes_read, int | float):
                samples = self._samples.setdefault(read_id, deque())
                samples.append((now, float(bytes_read)))
                while samples and now - samples[0][0] > SPEED_WINDOW_S:
                    samples.popleft()
        for gone in set(self._samples) - seen:
            del self._samples[gone]
        wall = time.time()
        for raw in self._reads:
            if str(raw.get("id", "")) not in seen:
                self._idle[str(raw.get("id", ""))] = (raw, wall)
        reading = {_file_key(raw) for raw in reads}
        self._idle = {
            read_id: entry
            for read_id, entry in self._idle.items()
            if read_id not in seen and _file_key(entry[0]) not in reading
        }
        self._reads = reads

    def _speed(self, raw: dict[str, Any], now: float) -> float | None:
        last = raw.get("lastActivityAt")
        if isinstance(last, int | float):
            idle = time.time() - last / 1000
            if idle > IDLE_READ_S:
                return 0.0
        samples = [
            s for s in self._samples.get(str(raw.get("id", "")), ()) if now - s[0] <= SPEED_WINDOW_S
        ]
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < 1:
            return None
        (t0, b0), (t1, b1) = samples[0], samples[-1]
        return max(0.0, (b1 - b0) / (t1 - t0))


def _file_key(raw: dict[str, Any]) -> object:
    return raw.get("path") or raw.get("fileName") or raw.get("id")


def _last_activity(raw: dict[str, Any], default: float) -> float:
    last = raw.get("lastActivityAt")
    return last / 1000 if isinstance(last, int | float) and last > 0 else default
