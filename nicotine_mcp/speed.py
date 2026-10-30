# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finds fast uploaders by measuring them.

The upload speed a user reports says little about what they actually deliver.
In a measurement of 8 uploaders, one reporting 8.3 MB/s delivered 96 KB/s, while
one reporting 1.9 MB/s delivered 1.7 MB/s. So instead of trusting reports:

- ``SpeedTracker`` remembers how fast each uploader really was, for ranking
- ``race_downloads`` starts a file from several uploaders at once, and keeps
  the one that turns out to be the fastest
"""

from __future__ import annotations

import json
import os
import time

from threading import Lock

from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio

SPEEDS_FILE_NAME = "nicotine_mcp_speeds.json"

RACE_ENTRANTS = 4
RACE_SECONDS = 14
RACE_START_TIMEOUT = 30
RACE_POLL_SECONDS = 1.0
RACE_MIN_MEASURE_SECONDS = 2.0

# Uploaders measured below this only get a place in a race when there is nobody else
KNOWN_SLOW_SPEED = 200_000

# How much worse than the best candidate a lossy file can be and still take part in a race
_RACE_QUALITY_TOLERANCE = 0.8

# A transfer starts with a burst as network buffers fill up, which says nothing about
# what follows. Counting it made an uploader look 3x as fast as it turned out to be.
RACE_SETTLE_SECONDS = 4.0
_FINISHED_BONUS = 1e12

_FAILURE_SPEED = 20_000
_AVERAGING_SECONDS = 60.0
_MAX_AGE_SECONDS = 60 * 24 * 3600
_MAX_ENTRIES = 5000


class SpeedTracker:
    """Remembers how fast each uploader really was, across restarts."""

    def __init__(self, store_path=None, clock=time.time):

        self.store_path = store_path
        self._clock = clock
        self._lock = Lock()
        self._entries = {}  # Username -> [bytes per second, last updated at]
        self._is_dirty = False
        self._load()

    def _load(self):

        if not self.store_path or not os.path.exists(self.store_path):
            return

        try:
            with open(self.store_path, encoding="utf-8") as file_handle:
                entries = json.load(file_handle)

            oldest = self._clock() - _MAX_AGE_SECONDS
            self._entries = {
                username: [float(entry[0]), float(entry[1])]
                for username, entry in entries.items() if entry[1] >= oldest
            }

        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            # Only a cache of measurements, it fills up again by itself
            self._entries = {}

    def save(self):

        with self._lock:
            if not self._is_dirty or not self.store_path:
                return

            if len(self._entries) > _MAX_ENTRIES:
                newest = sorted(self._entries.items(), key=lambda item: -item[1][1])[:_MAX_ENTRIES]
                self._entries = dict(newest)

            temporary_path = self.store_path + ".tmp"

            with open(temporary_path, "w", encoding="utf-8") as file_handle:
                json.dump(self._entries, file_handle)

            os.replace(temporary_path, self.store_path)
            self._is_dirty = False

    def record(self, username, bytes_per_second, seconds):
        """Adds a measurement. Longer measurements count for more, and recent ones
        outweigh old ones, since a connection can be busy one day and free the next."""

        if bytes_per_second < 0 or seconds <= 0:
            return

        with self._lock:
            entry = self._entries.get(username)

            if entry is None:
                speed = bytes_per_second
            else:
                weight = min(1.0, seconds / _AVERAGING_SECONDS)
                speed = entry[0] * (1.0 - weight) + bytes_per_second * weight

            self._entries[username] = [speed, self._clock()]
            self._is_dirty = True

    def record_failure(self, username):
        """Somebody who doesn't deliver at all is no better than somebody very slow."""

        with self._lock:
            entry = self._entries.get(username)
            speed = _FAILURE_SPEED if entry is None else max(_FAILURE_SPEED, entry[0] / 2)

            self._entries[username] = [speed, self._clock()]
            self._is_dirty = True

    def estimate(self, username):
        """Returns the expected speed in bytes per second, 0 when not measured yet."""

        with self._lock:
            entry = self._entries.get(username)
            return round(entry[0]) if entry is not None else 0

    def fastest(self, limit=10):
        """Returns the fastest uploaders measured, for the dashboard."""

        with self._lock:
            ordered = sorted(self._entries.items(), key=lambda item: -item[1][0])[:limit]
            return [{"username": username, "speed_bytes": round(entry[0])} for username, entry in ordered]

    def __len__(self):

        with self._lock:
            return len(self._entries)


def pick_entrants(candidates, speeds, quality, limit=RACE_ENTRANTS):
    """Picks who gets to race from candidates that are ranked best first, each with a different uploader.

    Speed decides a race and nothing else, so only those with the quality of the best one
    take part: a 128 kbps file must not beat a FLAC by being small and fast. ``speeds`` maps
    usernames to what they were measured at, and ``quality`` returns a number for a candidate.
    """

    if not candidates:
        return []

    minimum_quality = quality(candidates[0]) * _RACE_QUALITY_TOLERANCE
    alike = [candidate for candidate in candidates if quality(candidate) >= minimum_quality]

    def preference(candidate):
        speed = speeds.get(candidate["username"], 0)
        return 2 if 0 < speed < KNOWN_SLOW_SPEED else (1 if speed == 0 else 0)

    # The sort is stable, so within these groups the ranking still decides
    return sorted(alike, key=preference)[:limit]


class Entrant:
    """One uploader taking part in a race, for one file."""

    __slots__ = ("username", "virtual_path", "data", "state", "started_at", "started_bytes", "settled_at",
                 "settled_bytes", "ended_at", "transferred", "speed")

    def __init__(self, username, virtual_path, data=None):

        self.username = username
        self.virtual_path = virtual_path
        self.data = data
        self.state = "waiting"
        self.started_at = None
        self.started_bytes = 0
        self.settled_at = None
        self.settled_bytes = 0
        self.ended_at = None
        self.transferred = 0
        self.speed = 0.0

    def describe(self):
        return {"username": self.username, "state": self.state, "speed_bytes": round(self.speed)}


def _update_entrant(entrant, download, now, settle_seconds):

    if entrant.state in {"failed", "finished"}:
        return

    if download is None:
        entrant.state = "failed"
        return

    state = download["state"]
    transferred = download["bytes_transferred"]

    if state in {"active", "finished"} and entrant.started_at is None:
        entrant.started_at = now

        # Already finished when first seen: all of it arrived since the last look
        entrant.started_bytes = 0 if state == "finished" else transferred

    if state == "active":
        entrant.state = "active"
        entrant.transferred = transferred

        if entrant.settled_at is None and now - entrant.started_at >= settle_seconds:
            entrant.settled_at = now
            entrant.settled_bytes = transferred

    elif state == "finished":
        entrant.state = "finished"
        entrant.ended_at = now
        entrant.transferred = download["size_bytes"] or transferred

    elif state == "failed":
        entrant.state = "failed"


def _measure(entrant, now, min_seconds):
    """Returns (speed, whether it was measured for long enough)."""

    if entrant.started_at is None or entrant.state == "failed":
        return 0.0, False

    if entrant.state == "finished":
        # Finished within a poll interval: that can only mean the file is small and the uploader fast
        seconds = entrant.ended_at - entrant.started_at
        return (entrant.transferred - entrant.started_bytes) / max(seconds, 1.0), True

    if entrant.settled_at is None:
        return 0.0, False

    seconds = now - entrant.settled_at
    return (entrant.transferred - entrant.settled_bytes) / max(seconds, 0.001), seconds >= min_seconds


async def race_downloads(runner, entrants, start_timeout=RACE_START_TIMEOUT, race_seconds=RACE_SECONDS,
                         poll_seconds=RACE_POLL_SECONDS, min_measure_seconds=RACE_MIN_MEASURE_SECONDS,
                         settle_seconds=RACE_SETTLE_SECONDS, clock=time.monotonic):
    """Picks the fastest of several downloads that have been enqueued, and removes the
    others along with their partial files. Returns the winner, or None if none got going.

    The race ends a while after the first transfer starts, since waiting for everybody
    could take forever. Ties go to the earlier entrant, so order them by preference.
    """

    automation = runner.automation
    started = clock()
    race_started = None

    while True:
        now = clock()

        for entrant in entrants:
            download = await runner.call_async(automation.get_download, entrant.username, entrant.virtual_path)
            _update_entrant(entrant, download, now, settle_seconds)

        running = [entrant for entrant in entrants if entrant.started_at is not None and entrant.state != "failed"]

        if running and race_started is None:
            race_started = now

        undecided = [entrant for entrant in entrants if entrant.state == "waiting"]
        is_finished_race = any(entrant.state == "finished" for entrant in entrants)

        if race_started is None:
            if not undecided or now - started >= start_timeout:
                break

        elif (now - race_started >= race_seconds or is_finished_race
              or (not undecided and len(running) == 1)):
            break

        await anyio.sleep(poll_seconds)

    winner = _pick_winner(entrants, clock(), min_measure_seconds)
    await _finish_race(runner, entrants, winner, clock())
    return winner


def _pick_winner(entrants, now, min_measure_seconds):

    winner = None
    best_score = -1.0

    for entrant in entrants:
        entrant.speed, is_measured = _measure(entrant, now, min_measure_seconds)

        if entrant.started_at is None or entrant.state == "failed":
            continue

        # A transfer that only just started has shown nothing yet, it only wins if nobody else is running
        score = entrant.speed if is_measured else -0.5

        # Whoever has already delivered wins, removing a finished file isn't an option and it would stay behind
        if entrant.state == "finished":
            score += _FINISHED_BONUS

        if score > best_score:
            best_score = score
            winner = entrant

    return winner


async def _finish_race(runner, entrants, winner, now):
    """Records what was measured, and removes all but the winner."""

    automation = runner.automation

    for entrant in entrants:
        if entrant.settled_at is not None or entrant.state == "finished":
            seconds = (entrant.ended_at or now) - (entrant.settled_at or entrant.started_at)
            await runner.call_async(automation.record_speed, entrant.username, entrant.speed, seconds)

        elif entrant.state == "failed":
            await runner.call_async(automation.record_speed_failure, entrant.username)

        if entrant is not winner:
            try:
                await runner.call_async(automation.discard_download, entrant.username, entrant.virtual_path)

            except ToolError:
                pass
