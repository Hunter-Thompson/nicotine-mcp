# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""What a job and its releases consist of, and how they are described to agents."""

from __future__ import annotations

import os

from dataclasses import dataclass
from dataclasses import field

from nicotine_mcp.matching import is_various_artists
from nicotine_mcp.matching import Release

_UNSAFE_FOLDER_CHARACTERS = str.maketrans({character: "_" for character in '/\\:*?"<>|'})
_MOUNT_PARENTS = {os.sep, os.path.join(os.sep, "Volumes"), os.path.join(os.sep, "mnt"), os.path.join(os.sep, "media")}


class ItemState:

    PENDING = "pending"  # Waiting to be searched for
    SOURCED = "sourced"  # Found, waiting to be downloaded
    DOWNLOADING = "downloading"
    FINISHED = "finished"
    OWNED = "owned"  # Already on disk
    PARTIAL = "partial"  # Only incomplete copies found
    NOT_FOUND = "not_found"
    FAILED = "failed"  # Found, but no uploader delivered it
    NOT_ON_LABEL = "not_on_label"  # The catalog says this is not on the label after all
    CANCELLED = "cancelled"

    MISSING = {PARTIAL, NOT_FOUND}
    DONE = {FINISHED, OWNED, PARTIAL, NOT_FOUND, FAILED, NOT_ON_LABEL, CANCELLED}
    ALL = (PENDING, SOURCED, DOWNLOADING, FINISHED, OWNED, PARTIAL, NOT_FOUND, FAILED, NOT_ON_LABEL, CANCELLED)


class PauseMode:

    AFTER_ALBUM = "after_album"  # Albums that have started finish, nothing new starts
    NOW = "now"  # Transfers stop right away, and continue from where they were on resuming

    ALL = (AFTER_ALBUM, NOW)


class JobStatus:

    RUNNING = "running"
    PAUSED = "paused"
    PREVIEWED = "previewed"  # Preview done, waiting for start_job_downloads
    WAITING_RETRY = "waiting_retry"  # All done except for missing releases, which are retried later
    FINISHED = "finished"
    CANCELLED = "cancelled"

    ACTIVE = {RUNNING, WAITING_RETRY}


@dataclass
class JobItem:

    release: dict
    state: str = ItemState.PENDING
    sources: list = field(default_factory=list)
    partial_sources: list = field(default_factory=list)
    tried_sources: list = field(default_factory=list)
    current_source: dict | None = None
    is_queued: bool = False  # Requested, but the uploader hasn't got to us yet
    search_rounds: int = 0
    download_folder: str | None = None
    started_at: float | None = None
    last_progress_at: float | None = None
    last_bytes: int = 0
    current_speed: int = 0
    last_update_at: float | None = None
    speed_check_at: float | None = None
    speed_check_bytes: int = 0
    done_files: list = field(default_factory=list)  # From uploaders we have since moved on from
    last_race: list = field(default_factory=list)
    num_files: int = 0
    num_files_finished: int = 0
    progress_percent: float = 0.0
    local_path: str | None = None
    note: str | None = None


@dataclass
class Job:

    job_id: str
    name: str
    label_id: str | None
    mode: str
    artist_id: str | None = None
    pause_mode: str | None = None
    transfers_paused: bool = False  # We stopped transfers that have to be started again
    paused_by_all: bool = False  # Paused by pause_all(), so resume_all() continues it
    status: str = JobStatus.RUNNING
    created_at: float = 0.0
    finished_at: float | None = None
    next_retry_at: float | None = None
    retry_rounds: int = 0
    blocked_reason: str | None = None
    download_folder: str | None = None
    group_by: str = "job"  # Subfolders: one named after the job, or one per artist or label
    formats: list | None = None
    prefer: str = "balanced"
    min_bitrate: int = 0
    audio_only: bool = False
    accept_partial: bool = False
    skip_owned: bool = True
    wait_seconds: float = 8
    search_interval: float = 10
    max_active_releases: int = 3
    max_per_uploader: int = 1
    start_timeout: int = 900
    min_speed: int = 300_000
    retry_missing_hours: float = 24
    max_retry_rounds: int = 7
    filters: dict = field(default_factory=dict)
    num_in_catalog: int = 0
    items: list = field(default_factory=list)


def clean_folder_name(name):
    return (name or "").translate(_UNSAFE_FOLDER_CHARACTERS).strip(" .") or "Unknown"


def release_folder_name(release, group_by):
    """The subfolder a release is filed in: its artist, or its label when asked for and
    known, and always the label for a compilation."""

    artist = release.primary_artist or release.artist
    label = release.labels[0] if release.labels else ""

    if group_by == "label" and label:
        return clean_folder_name(label)

    if is_various_artists(artist):
        return clean_folder_name(label or "Various Artists")

    return clean_folder_name(artist)


def is_destination_available(path):
    """Checks that a download folder is on a disk that is present. Without this,
    files would go to the system disk while an external drive is unplugged."""

    current = os.path.abspath(path)

    while not os.path.isdir(current):
        parent = os.path.dirname(current)

        if parent == current:
            return False

        current = parent

    return current not in _MOUNT_PARENTS


def describe_spine(item):
    """The little the dashboard needs to know to draw a release on a label's shelf."""

    release = item.release
    tracks = release.get("tracks")
    source = item.current_source or (item.sources[0] if item.sources else None)

    return {
        "state": item.state,
        "title": release.get("title"),
        "artist": release.get("artist"),
        "year": release.get("year"),
        "kind": release.get("kind"),
        "catalog_number": (release.get("catalog_numbers") or [None])[0],
        "track_count": len(tracks) if tracks else release.get("track_count"),
        "progress_percent": item.progress_percent,
        "is_queued": item.is_queued,
        "uploader": source["username"] if source else None
    }


def describe_item(item, include_sources=False):
    """What an agent or the dashboard sees of a release in a job."""

    release = Release.from_dict(item.release)
    data = {
        **release.summary(),
        "state": item.state,
        "is_queued": item.is_queued,
        "note": item.note,
        "num_sources": len(item.sources),
        "num_files": item.num_files,
        "num_files_finished": item.num_files_finished,
        "progress_percent": item.progress_percent,
        "speed_bytes": item.current_speed if item.state == ItemState.DOWNLOADING else 0,
        "local_path": item.local_path
    }

    if item.current_source:
        data["uploader"] = item.current_source["username"]
        data["folder_path"] = item.current_source["folder_path"]

    elif item.sources:
        data["uploader"] = item.sources[0]["username"]
        data["folder_path"] = item.sources[0]["folder_path"]

    if include_sources:
        data["last_race"] = item.last_race
        data["sources"] = item.sources
        data["partial_sources"] = item.partial_sources

    elif item.state == ItemState.PARTIAL and item.partial_sources:
        match = item.partial_sources[0]["match"]
        data["best_partial_copy"] = {
            "username": item.partial_sources[0]["username"],
            "folder_path": item.partial_sources[0]["folder_path"],
            "num_matched": match["num_matched"],
            "num_expected": match["num_expected"],
            "missing_tracks": match["missing_tracks"][:10]
        }

    return data
