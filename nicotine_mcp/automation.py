# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Automation layer on top of the Nicotine+ core.

Collects search responses, ranks them and manages downloads on behalf of
an agent. Every public method must be called from the thread running the
Nicotine+ main loop (see ``nicotine_mcp.runner.CoreRunner.call``).

This module does not depend on the ``mcp`` package, so it can be tested
and reused without it.
"""

from __future__ import annotations

import os
import time

from dataclasses import replace
from operator import itemgetter

import pynicotine
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.slskmessages import TransferRejectReason
from pynicotine.slskmessages import UserStatus
from pynicotine.transfers import TransferStatus
from pynicotine.utils import human_length
from pynicotine.utils import human_size
from pynicotine.utils import human_speed

from nicotine_mcp.ranking import FileCandidate
from nicotine_mcp.ranking import RankingPreferences
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import is_audio_extension
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.ranking import rank_files
from nicotine_mcp.ranking import rank_folders
from nicotine_mcp.ranking import summarize_formats
from nicotine_mcp.speed import SPEEDS_FILE_NAME
from nicotine_mcp.speed import SpeedTracker

SEARCH_MODES = ("global", "user", "buddies", "rooms")


class DownloadState:
    """Coarse download states exposed to agents, derived from TransferStatus."""

    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"

    ALL = (QUEUED, ACTIVE, PAUSED, FINISHED, FAILED)
    TERMINAL = {FINISHED, FAILED}

    # Statuses the core retries automatically after a few minutes
    RETRIED_STATUSES = {
        TransferStatus.CONNECTION_CLOSED,
        TransferStatus.CONNECTION_TIMEOUT,
        TransferStatus.USER_LOGGED_OFF,
        TransferRejectReason.PENDING_SHUTDOWN,
        TransferStatus.DOWNLOAD_FOLDER_ERROR,
        TransferStatus.LOCAL_FILE_ERROR,
        TransferRejectReason.FILE_READ_ERROR
    }

    @classmethod
    def from_status(cls, status):

        if status == TransferStatus.FINISHED:
            return cls.FINISHED

        if status == TransferStatus.TRANSFERRING:
            return cls.ACTIVE

        if status in {TransferStatus.QUEUED, TransferStatus.GETTING_STATUS}:
            return cls.QUEUED

        if status == TransferStatus.PAUSED:
            return cls.PAUSED

        return cls.FAILED


class CollectedSearch:
    """Search results collected for a single search token."""

    __slots__ = ("token", "query", "mode", "username", "started_at", "last_result_at", "num_responses",
                 "num_files", "candidates", "included_words", "excluded_words", "is_active")

    def __init__(self, token, query, mode, username, included_words, excluded_words):

        self.token = token
        self.query = query
        self.mode = mode
        self.username = username
        self.started_at = time.monotonic()
        self.last_result_at = None
        self.num_responses = 0
        self.num_files = 0
        self.candidates = {}
        self.included_words = included_words
        self.excluded_words = excluded_words
        self.is_active = True


class PendingFolder:
    """A folder whose contents we requested from a user, in order to download it."""

    __slots__ = ("username", "folder_path", "download_folder", "audio_only", "callback", "listing_only", "formats",
                 "timer_id")

    def __init__(self, username, folder_path, download_folder, audio_only, callback, listing_only=False,
                 formats=None):

        self.username = username
        self.folder_path = folder_path
        self.download_folder = download_folder
        self.audio_only = audio_only
        self.callback = callback
        self.listing_only = listing_only
        self.formats = formats
        self.timer_id = None

    @property
    def key(self):
        return self.username + self.folder_path


class Automation:

    MAX_SEARCHES = 25
    MAX_RESULTS_PER_SEARCH = 10000
    FOLDER_REQUEST_TIMEOUT = 20
    SPEED_SAMPLE_SECONDS = 3
    SPEED_SAVE_SECONDS = 60

    def __init__(self):

        self.searches = {}
        self.speeds = SpeedTracker(os.path.join(config.data_folder_path, SPEEDS_FILE_NAME))
        self._pending_folders = {}
        self._last_search_token = None
        self._speed_samples = {}
        self._last_speed_sample_at = None
        self._last_speed_save_at = time.monotonic()
        self._speed_timer_id = events.schedule(
            delay=self.SPEED_SAMPLE_SECONDS, callback=self.sample_speeds, repeat=True
        )

        for event_name, callback in (
            ("add-search", self._add_search),
            ("file-search-response", self._file_search_response),
            ("folder-contents-response", self._folder_contents_response),
            ("folder-contents-failed", self._folder_contents_failed),
            ("quit", self._quit),
            ("server-disconnect", self._server_disconnect)
        ):
            events.connect(event_name, callback)

    def _quit(self):

        self.searches.clear()
        events.cancel_scheduled(self._speed_timer_id)
        self.speeds.save()

        for pending in list(self._pending_folders.values()):
            self._finish_pending_folder(pending, error="application is quitting")

    def _server_disconnect(self, _msg):

        for pending in list(self._pending_folders.values()):
            self._finish_pending_folder(pending, error="disconnected from server")

    # Status #

    @staticmethod
    def is_connected():
        return core.users.login_status != UserStatus.OFFLINE

    def get_status(self):

        login_status = core.users.login_status
        status_names = {
            UserStatus.OFFLINE: "offline",
            UserStatus.AWAY: "away",
            UserStatus.ONLINE: "online"
        }
        download_states = {state: 0 for state in DownloadState.ALL}

        for transfer in core.downloads.transfers.values():
            download_states[DownloadState.from_status(transfer.status)] += 1

        return {
            "connected": login_status != UserStatus.OFFLINE,
            "status": status_names.get(login_status, "offline"),
            "username": core.users.login_username or config.sections["server"]["login"] or None,
            "needs_credentials": config.need_config(),
            "download_folder": core.downloads.get_default_download_folder(),
            "incomplete_folder": core.downloads.get_incomplete_download_folder(),
            "downloads": download_states,
            "searches": len(self.searches),
            "version": pynicotine.__version__
        }

    # Searching #

    def start_search(self, query, mode="global", username=None):
        """Starts a search and returns its token."""

        query = (query or "").strip()

        if not query:
            raise ValueError("Search query is empty")

        if mode not in SEARCH_MODES:
            raise ValueError(f"Unknown search mode '{mode}', expected one of {', '.join(SEARCH_MODES)}")

        if mode == "user" and not username:
            raise ValueError("A username is required when searching a user's shares")

        if not self.is_connected():
            raise RuntimeError("Not connected to the Soulseek server. Use the connect tool first.")

        users = [username] if mode == "user" else None

        # The core announces the new search (and its token) through the "add-search" event
        self._last_search_token = None
        core.search.do_search(query, mode, users=users, switch_page=False)
        token = self._last_search_token

        if token is None or token not in core.search.searches:
            raise RuntimeError("The search could not be started")

        search = core.search.searches[token]

        self.searches[token] = CollectedSearch(
            token, query, mode, username, included_words=search.included_words, excluded_words=search.excluded_words
        )
        self._prune_searches()

        log.add_search("MCP: started %s search %s for \"%s\"", (mode, token, query))
        return token

    def _add_search(self, token, _search, _switch_page=True):
        self._last_search_token = token

    def stop_search(self, token):
        """Stops accepting new results for a search. Collected results are kept."""

        search = self.searches.get(token)

        if search is None:
            return False

        if search.is_active:
            core.search.remove_search(token)
            search.is_active = False

        return True

    def _prune_searches(self):

        while len(self.searches) > self.MAX_SEARCHES:
            oldest_token = next(iter(self.searches))
            self.stop_search(oldest_token)
            del self.searches[oldest_token]

    def get_search(self, token):

        search = self.searches.get(token)

        if search is None:
            raise KeyError(f"Unknown search token {token}. Start a new search first.")

        return search

    def get_search_summary(self, token):

        search = self.get_search(token)
        now = time.monotonic()
        seconds_since_last_result = None

        if search.last_result_at is not None:
            seconds_since_last_result = round(now - search.last_result_at, 1)

        return {
            "token": token,
            "query": search.query,
            "mode": search.mode,
            "username": search.username,
            "is_active": search.is_active,
            "elapsed_seconds": round(now - search.started_at, 1),
            "seconds_since_last_result": seconds_since_last_result,
            "num_users_responded": search.num_responses,
            "num_files": len(search.candidates),
            "formats": summarize_formats(search.candidates.values())
        }

    def _file_search_response(self, msg):
        """Peer code 9. Called after the core has validated the response."""

        search = self.searches.get(msg.token)

        if search is None or not search.is_active:
            return

        if not msg.list:
            return

        username = msg.username
        ip_address = msg.addr[0] if msg.addr else None
        country_code = ""

        if ip_address:
            country_code = core.network_filter.get_country_code(ip_address) or ""

        if not country_code:
            country_code = core.users.countries.get(username) or ""

        has_free_slot = bool(msg.freeulslots)
        queue_length = 0 if has_free_slot else (msg.inqueue or 1)
        upload_speed = msg.ulspeed or 0

        search.num_responses += 1
        search.last_result_at = time.monotonic()

        for _code, file_path, size, _ext, file_attributes, *_unused in msg.list:
            if len(search.candidates) >= self.MAX_RESULTS_PER_SEARCH:
                break

            file_path_lower = file_path.lower()

            if any(word in file_path_lower for word in search.excluded_words):
                continue

            if not all(word in file_path_lower for word in search.included_words):
                # Some users send unrelated results
                continue

            candidate = FileCandidate(
                username=username,
                virtual_path=file_path,
                size=size,
                bitrate=file_attributes.bitrate or 0,
                length=file_attributes.length or 0,
                vbr=bool(file_attributes.vbr),
                sample_rate=file_attributes.sample_rate,
                bit_depth=file_attributes.bit_depth,
                has_free_slot=has_free_slot,
                queue_length=queue_length,
                upload_speed=upload_speed,
                measured_speed=self.speeds.estimate(username),
                country_code=country_code,
                file_attributes=file_attributes
            )
            search.candidates[candidate.key] = candidate
            search.num_files += 1

    # Ranking #

    @staticmethod
    def describe_file(candidate):

        return {
            "username": candidate.username,
            "virtual_path": candidate.virtual_path,
            "basename": candidate.basename,
            "folder_path": candidate.folder_path,
            "format": candidate.extension,
            "is_lossless": candidate.is_lossless,
            "quality": candidate.quality_label,
            "bitrate_kbps": candidate.effective_bitrate or None,
            "sample_rate": candidate.sample_rate,
            "bit_depth": candidate.bit_depth,
            "length_seconds": candidate.length or None,
            "length": human_length(candidate.length) if candidate.length else None,
            "size_bytes": candidate.size,
            "size": human_size(candidate.size),
            "has_free_slot": candidate.has_free_slot,
            "queue_length": candidate.queue_length,
            "upload_speed_bytes": candidate.upload_speed,
            "upload_speed": human_speed(candidate.upload_speed) if candidate.upload_speed else None,
            "measured_speed": human_speed(candidate.measured_speed) if candidate.measured_speed else None,
            "country_code": candidate.country_code or None,
            "score": candidate.score,
            "score_details": candidate.score_details
        }

    @classmethod
    def describe_folder(cls, folder, max_files=50):

        files = sorted(folder.files, key=lambda item: item.basename.lower())

        return {
            "username": folder.username,
            "folder_path": folder.folder_path,
            "folder_name": folder.folder_name,
            "num_matching_files": len(folder.files),
            "num_audio_files": len(folder.audio_files),
            "formats": folder.formats,
            "total_size_bytes": folder.total_size,
            "total_size": human_size(folder.total_size),
            "has_free_slot": folder.has_free_slot,
            "queue_length": folder.queue_length,
            "upload_speed_bytes": folder.upload_speed,
            "upload_speed": human_speed(folder.upload_speed) if folder.upload_speed else None,
            "measured_speed": human_speed(folder.measured_speed) if folder.measured_speed else None,
            "country_code": folder.country_code or None,
            "score": folder.score,
            "score_details": folder.score_details,
            "files": [cls.describe_file(candidate) for candidate in files[:max_files]]
        }

    @staticmethod
    def _search_preferences(search, prefs):
        """Adds the query words of a search to the ranking preferences."""

        if prefs is None:
            prefs = RankingPreferences()

        return replace(prefs, query_words=tuple(search.included_words))

    def rank_tracks(self, token, prefs=None, limit=20):

        search = self.get_search(token)
        prefs = self._search_preferences(search, prefs)
        ranked = rank_files(search.candidates.values(), prefs, limit=limit)

        return {
            "search": self.get_search_summary(token),
            "num_matching_files": len(ranked),
            "tracks": [self.describe_file(candidate) for candidate in ranked]
        }

    def rank_albums(self, token, prefs=None, limit=10):

        search = self.get_search(token)
        prefs = self._search_preferences(search, prefs)
        ranked = rank_folders(search.candidates.values(), prefs, limit=limit)

        return {
            "search": self.get_search_summary(token),
            "num_matching_folders": len(ranked),
            "albums": [self.describe_folder(folder) for folder in ranked]
        }

    def find_candidate(self, username, virtual_path):
        """Looks up a file in the collected results of any search, newest first."""

        key = username + virtual_path

        for search in reversed(list(self.searches.values())):
            candidate = search.candidates.get(key)

            if candidate is not None:
                return candidate

        return None

    def find_folder_candidates(self, username, folder_path):
        """Returns the files we know about in a user's folder, from any search."""

        candidates = {}

        for search in self.searches.values():
            for candidate in search.candidates.values():
                if candidate.username == username and candidate.folder_path == folder_path:
                    candidates[candidate.key] = candidate

        return list(candidates.values())

    # Speed measurements #

    def sample_speeds(self):
        """Measures what active transfers really deliver, for ranking uploaders later on."""

        now = time.monotonic()
        seconds = now - self._last_speed_sample_at if self._last_speed_sample_at is not None else 0
        samples = {}
        delivered = {}

        for transfer in core.downloads.transfers.values():
            if transfer.status != TransferStatus.TRANSFERRING:
                continue

            key = transfer.username + transfer.virtual_path
            current_bytes = transfer.current_byte_offset or 0
            samples[key] = current_bytes
            previous_bytes = self._speed_samples.get(key)

            if previous_bytes is not None and current_bytes >= previous_bytes:
                delivered[transfer.username] = delivered.get(transfer.username, 0) + current_bytes - previous_bytes

        if seconds > 0:
            for username, num_bytes in delivered.items():
                self.speeds.record(username, num_bytes / seconds, seconds)

        self._speed_samples = samples
        self._last_speed_sample_at = now

        if now - self._last_speed_save_at >= self.SPEED_SAVE_SECONDS:
            self._last_speed_save_at = now
            self.speeds.save()

    def record_speed(self, username, bytes_per_second, seconds):
        self.speeds.record(username, bytes_per_second, seconds)

    def record_speed_failure(self, username):
        self.speeds.record_failure(username)

    def get_uploader_speed(self, username):
        return self.speeds.estimate(username)

    # Downloads #

    @staticmethod
    def _get_transfer(username, virtual_path):
        return core.downloads.transfers.get(username + virtual_path)

    @classmethod
    def describe_transfer(cls, transfer):

        if transfer is None:
            return None

        size = transfer.size or 0
        bytes_transferred = transfer.current_byte_offset or 0
        state = DownloadState.from_status(transfer.status)
        progress = 100.0 if state == DownloadState.FINISHED else (
            round(bytes_transferred / size * 100, 1) if size else 0.0
        )
        local_path = None

        if state == DownloadState.FINISHED:
            local_path = core.downloads.get_current_download_file_path(transfer)

        return {
            "username": transfer.username,
            "virtual_path": transfer.virtual_path,
            "basename": transfer.virtual_path.rpartition("\\")[-1],
            "state": state,
            "status": transfer.status,
            "will_retry": transfer.status in DownloadState.RETRIED_STATUSES,
            "size_bytes": size,
            "size": human_size(size),
            "bytes_transferred": bytes_transferred,
            "progress_percent": progress,
            "speed_bytes": transfer.speed or 0,
            "speed": human_speed(transfer.speed) if transfer.speed else None,
            "time_left_seconds": transfer.time_left or None,
            "queue_position": transfer.queue_position or 0,
            "download_folder": transfer.folder_path,
            "local_path": local_path
        }

    def get_download(self, username, virtual_path):
        return self.describe_transfer(self._get_transfer(username, virtual_path))

    def download_file(self, username, virtual_path, download_folder=None, size=0, file_attributes=None):
        """Enqueues a single file. Size and attributes are looked up in
        collected search results when not provided."""

        if not username or not virtual_path:
            raise ValueError("Both username and virtual_path are required")

        if not self.is_connected():
            raise RuntimeError("Not connected to the Soulseek server. Use the connect tool first.")

        existing = self._get_transfer(username, virtual_path)

        if (existing is not None and (not download_folder or existing.folder_path == download_folder)
                and DownloadState.from_status(existing.status) in {DownloadState.PAUSED, DownloadState.FAILED}):
            # Already in the download list, resume it instead of ignoring it as a duplicate
            core.downloads.retry_download(existing)
            return self.get_download(username, virtual_path)

        candidate = self.find_candidate(username, virtual_path)

        if candidate is not None:
            size = size or candidate.size
            file_attributes = file_attributes or candidate.file_attributes

        core.downloads.enqueue_download(
            username, virtual_path, folder_path=download_folder or None, size=size, file_attributes=file_attributes
        )
        return self.get_download(username, virtual_path)

    def enqueue_probe(self, username, folder_path, basename, size=0, download_folder=None):
        """Enqueues one file of a folder to the place the whole folder would go,
        so the rest of the folder can follow if this uploader turns out to be fast."""

        destination = core.downloads.get_folder_destination(
            username, folder_path, download_folder_path=download_folder or None
        )
        return self.download_file(username, "\\".join([folder_path, basename]), destination, size=size)

    def enqueue_files(self, username, folder_path, files, destination):
        """Enqueues some files of a folder (dicts with a basename and size) to a specific folder."""

        downloads = [
            self.download_file(username, "\\".join([folder_path, item["basename"]]), destination, size=item["size"])
            for item in files
        ]
        return {"username": username, "folder_path": folder_path, "download_folder": destination,
                "num_files": len(downloads), "files": downloads}

    def _enqueue_folder_files(self, username, folder_path, files, download_folder=None, audio_only=False,
                              formats=None):
        """Enqueues files (basename, size, attributes) of a folder into a
        subfolder named after the remote folder, like the GUI does."""

        destination = core.downloads.get_folder_destination(
            username, folder_path, download_folder_path=download_folder or None
        )
        enqueued = []
        skipped = []

        for basename, size, file_attributes in sorted(files, key=itemgetter(0)):
            is_skipped_extra = audio_only and not is_audio_extension(get_extension(basename))

            if is_skipped_extra or not is_file_wanted(basename, formats):
                skipped.append(basename)
                continue

            virtual_path = "\\".join([folder_path, basename])

            core.downloads.enqueue_download(
                username, virtual_path, folder_path=destination, size=size, file_attributes=file_attributes
            )
            enqueued.append(self.get_download(username, virtual_path))

        return {
            "username": username,
            "folder_path": folder_path,
            "download_folder": destination,
            "num_files": len(enqueued),
            "skipped_files": skipped,
            "files": enqueued
        }

    def download_folder_from_search(self, username, folder_path, download_folder=None, audio_only=False,
                                    formats=None):
        """Enqueues the files of a folder that appeared in search results.

        Used as a fallback when the user does not answer folder content
        requests. The folder may be incomplete, since search responses only
        contain files matching the search term.
        """

        if not self.is_connected():
            raise RuntimeError("Not connected to the Soulseek server. Use the connect tool first.")

        candidates = self.find_folder_candidates(username, folder_path)

        if not candidates:
            raise KeyError(f"No search results known for folder '{folder_path}' of user '{username}'")

        files = [(candidate.basename, candidate.size, candidate.file_attributes) for candidate in candidates]
        result = self._enqueue_folder_files(username, folder_path, files, download_folder, audio_only, formats)
        result["partial"] = True
        return result

    def request_folder_download(self, username, folder_path, callback, download_folder=None, audio_only=False,
                                formats=None):
        """Requests the full contents of a user's folder and enqueues every
        file once the response arrives.

        ``callback(result)`` is invoked in the main loop thread with a dict
        describing enqueued files, or with an ``error`` key on failure.
        """

        self._request_folder(
            PendingFolder(username, folder_path, download_folder, audio_only, callback, formats=formats)
        )

    def request_folder_listing(self, username, folder_path, callback):
        """Requests the full contents of a user's folder without downloading anything.

        ``callback(result)`` is invoked in the main loop thread with a dict
        listing the files, or with an ``error`` key on failure.
        """

        self._request_folder(PendingFolder(username, folder_path, None, False, callback, listing_only=True))

    def _request_folder(self, pending):

        username = pending.username
        folder_path = pending.folder_path

        if not username or not folder_path:
            raise ValueError("Both username and folder_path are required")

        if not self.is_connected():
            raise RuntimeError("Not connected to the Soulseek server. Use the connect tool first.")

        pending.folder_path = folder_path = folder_path.rstrip("\\")

        if pending.key in self._pending_folders:
            raise RuntimeError(f"Folder '{folder_path}' of user '{username}' is already being requested")

        self._pending_folders[pending.key] = pending
        pending.timer_id = events.schedule(
            delay=self.FOLDER_REQUEST_TIMEOUT, callback=self._pending_folder_timeout, callback_args=(pending,)
        )
        core.downloads.request_folder(username, folder_path)

    def _finish_pending_folder(self, pending, files=None, error=None):

        if self._pending_folders.pop(pending.key, None) is None:
            return

        if pending.timer_id is not None:
            events.cancel_scheduled(pending.timer_id)
            pending.timer_id = None

        if error:
            result = {
                "username": pending.username,
                "folder_path": pending.folder_path,
                "error": error
            }
        elif pending.listing_only:
            result = {
                "username": pending.username,
                "folder_path": pending.folder_path,
                "files": [
                    {"basename": basename, "size": size, "length": attributes.length or 0}
                    for basename, size, attributes in files
                ]
            }
        else:
            result = self._enqueue_folder_files(
                pending.username, pending.folder_path, files, pending.download_folder, pending.audio_only,
                pending.formats
            )

        pending.callback(result)

    def _folder_contents_response(self, msg):
        """Peer code 37."""

        if msg.dir is None:
            return

        pending = self._pending_folders.get(msg.username + msg.dir)

        if pending is None:
            return

        files = (msg.list or {}).get(msg.dir)

        if not files:
            # Core retries with a legacy request on empty responses, wait for that
            return

        self._finish_pending_folder(
            pending, files=[(basename, size, attributes) for _code, basename, size, _ext, attributes, *_x in files]
        )

    def _folder_contents_failed(self, username, folder_path, is_offline=False):

        pending = self._pending_folders.get(username + folder_path)

        if pending is None:
            return

        error = "user is offline" if is_offline else "user did not respond to the folder contents request"
        self._finish_pending_folder(pending, error=error)

    def _pending_folder_timeout(self, pending):
        self._finish_pending_folder(pending, error="timed out waiting for folder contents")

    def select_downloads(self, username=None, virtual_paths=None, folder_path=None, states=None):
        """Returns transfers matching the given criteria (all when no criteria given)."""

        selected = []
        folder_prefix = folder_path.rstrip("\\") + "\\" if folder_path else None
        virtual_paths = set(virtual_paths) if virtual_paths else None

        for transfer in core.downloads.transfers.values():
            if username and transfer.username != username:
                continue

            if virtual_paths is not None and transfer.virtual_path not in virtual_paths:
                continue

            if folder_prefix is not None and not transfer.virtual_path.startswith(folder_prefix):
                continue

            if states and DownloadState.from_status(transfer.status) not in states:
                continue

            selected.append(transfer)

        return selected

    @staticmethod
    def get_library_folders():
        """The folders that contain music we already have: downloads and shares."""

        folders = [core.downloads.get_default_download_folder()]

        for option in ("shared", "buddyshared"):
            for share in config.sections["transfers"][option]:
                folders.append(share[1])

        return list(dict.fromkeys(folders))

    def prepare_export(self):
        """Saves what is in memory, and says what an export needs to know."""

        core.downloads._save_transfers()  # pylint: disable=protected-access
        self.speeds.save()

        return {
            "data_folder": config.data_folder_path,
            "download_folder": core.downloads.get_default_download_folder(),
            "transfer_settings": dict(config.sections["transfers"])
        }

    def get_dashboard_state(self):
        """Everything the web dashboard shows, in one call."""

        order = {
            DownloadState.ACTIVE: 0, DownloadState.QUEUED: 1, DownloadState.PAUSED: 2,
            DownloadState.FAILED: 3, DownloadState.FINISHED: 4
        }
        downloads = [self.describe_transfer(transfer) for transfer in core.downloads.transfers.values()]
        downloads.sort(key=lambda item: order[item["state"]])
        total_speed = sum(item["speed_bytes"] for item in downloads if item["state"] == DownloadState.ACTIVE)

        return {
            "status": self.get_status(),
            "download_speed_bytes": total_speed,
            "download_speed": human_speed(total_speed) if total_speed else None,
            "fastest_uploaders": [
                {**item, "speed": human_speed(item["speed_bytes"])} for item in self.speeds.fastest()
            ],
            "num_uploaders_measured": len(self.speeds),
            "downloads": downloads,
            "searches": [self.get_search_summary(token) for token in reversed(list(self.searches))]
        }

    def clear_finished_downloads(self):
        """Removes finished downloads from the list. Files stay on disk."""

        finished = self.select_downloads(states={DownloadState.FINISHED})
        core.downloads.clear_downloads(finished)
        return {"num_removed": len(finished)}

    def get_downloads(self, state="all", username=None, folder_path=None, limit=100):

        if state != "all" and state not in DownloadState.ALL:
            raise ValueError(f"Unknown state '{state}', expected one of all, {', '.join(DownloadState.ALL)}")

        states = None if state == "all" else {state}
        transfers = self.select_downloads(username=username, folder_path=folder_path, states=states)
        limit = max(1, int(limit or 100))

        return {
            "num_downloads": len(transfers),
            "downloads": [self.describe_transfer(transfer) for transfer in transfers[-limit:]]
        }

    def cancel_download(self, username, virtual_path, remove=True):

        transfer = self._get_transfer(username, virtual_path)

        if transfer is None:
            raise KeyError(f"No download of '{virtual_path}' from user '{username}'")

        if remove:
            core.downloads.clear_downloads([transfer])
            return {"username": username, "virtual_path": virtual_path, "removed": True}

        core.downloads.abort_downloads([transfer])
        return self.describe_transfer(transfer)

    def discard_download(self, username, virtual_path):
        """Removes a download along with its partial file. The file's name includes the
        uploader, so nothing can continue it once the download is gone from the list."""

        transfer = self._get_transfer(username, virtual_path)

        if transfer is None:
            return False

        incomplete_path = core.downloads.get_incomplete_download_file_path(username, virtual_path)
        folder_path, basename = os.path.split(incomplete_path)
        core.downloads.clear_downloads([transfer])

        # macOS stores metadata in a "._" file next to it on some external drives
        for path in (incomplete_path, os.path.join(folder_path, "._" + basename)):
            try:
                os.remove(path)

            except OSError:
                pass

        return True

    def cancel_folder_downloads(self, username, folder_path, remove=True):

        transfers = self.select_downloads(username=username, folder_path=folder_path)

        if remove:
            core.downloads.clear_downloads(transfers)
        else:
            core.downloads.abort_downloads(transfers)

        return {"username": username, "folder_path": folder_path, "num_downloads": len(transfers)}

    def retry_download(self, username, virtual_path):

        transfer = self._get_transfer(username, virtual_path)

        if transfer is None:
            raise KeyError(f"No download of '{virtual_path}' from user '{username}'")

        core.downloads.retry_download(transfer)
        return self.describe_transfer(transfer)

    def pause_all_downloads(self):
        """Pauses every active or queued download. Partial files are kept."""

        transfers = self.select_downloads(states={DownloadState.ACTIVE, DownloadState.QUEUED})
        core.downloads.abort_downloads(transfers)
        return {"num_paused": len(transfers)}

    def resume_all_downloads(self):
        """Continues every paused download from where it was."""

        transfers = self.select_downloads(states={DownloadState.PAUSED})

        for transfer in transfers:
            core.downloads.retry_download(transfer)

        return {"num_resumed": len(transfers)}


def make_preferences(formats=None, prefer="balanced", min_bitrate=0, require_free_slot=False, max_queue=None,
                     min_length=0, max_length=0, max_per_user=None):
    """Builds RankingPreferences from loosely typed tool arguments."""

    return RankingPreferences(
        formats=formats if formats is not None else RankingPreferences.formats,
        prefer=prefer or "balanced",
        min_bitrate=min_bitrate or 0,
        require_free_slot=bool(require_free_slot),
        max_queue=max_queue,
        min_length=min_length or 0,
        max_length=max_length or 0,
        max_per_user=max_per_user if max_per_user is not None else RankingPreferences.max_per_user
    )
