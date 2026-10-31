# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finds the shared folders that really contain a release known from a catalog.

Search results only include the files that matched the query, so the most
promising folders have their full listing requested from the uploader before
they are compared with the release's tracklist.
"""

from __future__ import annotations

from nicotine_mcp.automation import make_preferences
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.matching import Verdict
from nicotine_mcp.matching import build_search_queries
from nicotine_mcp.matching import match_folder
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import is_audio_extension
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.ranking import is_lossless_extension
from nicotine_mcp.speed import Entrant
from nicotine_mcp.speed import RACE_MIN_MEASURE_SECONDS
from nicotine_mcp.speed import RACE_POLL_SECONDS
from nicotine_mcp.speed import RACE_SECONDS
from nicotine_mcp.speed import RACE_SETTLE_SECONDS
from nicotine_mcp.speed import RACE_START_TIMEOUT
from nicotine_mcp.speed import pick_entrants
from nicotine_mcp.speed import race_downloads

MAX_LISTINGS_PER_SEARCH = 8
MAX_SOURCES = 8
MAX_FOLDERS_PER_SEARCH = 40
ENOUGH_SOURCES = 3


def _source_order(source):
    return Verdict.ORDER[source["match"]["verdict"]], -source["rank_score"]


def _folder_quality(source):
    return 2 if any(is_lossless_extension(extension) for extension in source.get("formats") or []) else 1


def pick_probe_file(files):
    """Picks the file to race on, from dicts with a basename and size. The
    largest audio file won't finish before there has been time to measure."""

    audio_files = [item for item in files if is_audio_extension(get_extension(item["basename"]))]

    if not audio_files:
        return None

    largest = max(audio_files, key=lambda item: item["size"])
    return {"basename": largest["basename"], "size": largest["size"]}


class ReleaseSourcer:

    def __init__(self, runner, catalog):

        self.runner = runner
        self.catalog = catalog
        self.race_start_timeout = RACE_START_TIMEOUT
        self.race_seconds = RACE_SECONDS
        self.race_poll_seconds = RACE_POLL_SECONDS
        self.race_min_measure_seconds = RACE_MIN_MEASURE_SECONDS
        self.race_settle_seconds = RACE_SETTLE_SECONDS

    async def load_tracklist(self, release):
        """Returns the release with its tracklist. The rest of what the listing knew
        stays, since it was merged from all editions of the release."""

        if release.tracks is not None:
            return release

        details = await anyio.to_thread.run_sync(self.catalog.release_details, release.release_id)

        if not release.title:
            # Only its ID was known, as in a job started from a list of release IDs
            release.title = details.title
            release.artist = details.artist
            release.primary_artist = details.primary_artist
            release.year = details.year
            release.kind = details.kind
            release.formats = details.formats

        release.tracks = details.tracks
        release.track_count = len(details.tracks) or release.track_count
        release.label_ids = details.label_ids or release.label_ids

        for number in details.catalog_numbers:
            if number not in release.catalog_numbers:
                release.catalog_numbers.append(number)

        return release

    async def _list_folder(self, folder, release, sources, formats=None):
        """Requests the full listing of a folder and compares it with the release. Only files in
        the requested formats count: a FLAC album is not there because its MP3s are."""

        username = folder["username"]
        folder_path = folder["folder_path"]
        is_verified = False
        wanted_files = [item for item in folder["files"] if is_file_wanted(item["basename"], formats)]
        files = [(item["basename"], item["length_seconds"] or 0) for item in wanted_files]
        probe_file = pick_probe_file(
            [{"basename": item["basename"], "size": item.get("size_bytes", 0)} for item in wanted_files]
        )
        folder_formats = folder["formats"]

        try:
            listing = await self.runner.request_folder_listing(username, folder_path)

            if "error" not in listing:
                wanted_files = [item for item in listing["files"] if is_file_wanted(item["basename"], formats)]
                files = [(item["basename"], item["length"]) for item in wanted_files]
                probe_file = pick_probe_file(wanted_files)
                folder_formats = sorted({
                    get_extension(item["basename"]) for item in listing["files"]
                    if is_audio_extension(get_extension(item["basename"]))
                })
                is_verified = True

        except (ToolError, TimeoutError, RuntimeError):
            # Not every peer answers these requests, the files seen in the search still say something
            pass

        sources.append({
            "username": username,
            "folder_path": folder_path,
            "folder_name": folder["folder_name"],
            "formats": folder_formats,
            "total_size": folder["total_size"],
            "has_free_slot": folder["has_free_slot"],
            "queue_length": folder["queue_length"],
            "upload_speed": folder["upload_speed"],
            "rank_score": folder["score"],
            "measured_speed": folder.get("measured_speed"),
            "listing_verified": is_verified,
            "probe_file": probe_file,
            "match": match_folder(release, folder_path, files).to_dict()
        })

    async def _get_probe_file(self, source):
        """What is to be raced on. Sources found by an earlier version don't say, or it may be gone."""

        if source.get("probe_file"):
            return source["probe_file"]

        try:
            listing = await self.runner.request_folder_listing(source["username"], source["folder_path"])

        except (ToolError, TimeoutError, RuntimeError):
            return None

        return pick_probe_file(listing["files"]) if "error" not in listing else None

    async def pick_sources(self, sources):
        """Picks who races next from sources ranked best first, one per uploader."""

        automation = self.runner.automation
        by_uploader = {}

        for source in sources:
            by_uploader.setdefault(source["username"], source)

        speeds = {
            username: await self.runner.call_async(automation.get_uploader_speed, username) for username in by_uploader
        }
        return pick_entrants(list(by_uploader.values()), speeds, _folder_quality)

    async def race_sources(self, sources, download_folder=None, start_timeout=None):
        """Starts a file from each of several folders, one per uploader, and keeps the fastest.

        Only that one file is requested from each, so uploaders aren't left with a long queue
        of files that get cancelled. Returns (the winning source, the race's results). The
        rest of the folder still has to be enqueued.
        """

        entrants = []
        results = []

        for source in sources:
            probe_file = await self._get_probe_file(source)

            if probe_file is None:
                results.append({"username": source["username"], "state": "failed", "speed_bytes": 0})
                continue

            try:
                await self.runner.call_async(
                    self.runner.automation.enqueue_probe, source["username"], source["folder_path"],
                    probe_file["basename"], probe_file["size"], download_folder
                )

            except ToolError as error:
                results.append({"username": source["username"], "state": "failed", "speed_bytes": 0,
                                "error": str(error)})
                continue

            virtual_path = "\\".join([source["folder_path"], probe_file["basename"]])
            entrants.append(Entrant(source["username"], virtual_path, source))

        if not entrants:
            return None, results

        winner = await race_downloads(
            self.runner, entrants, start_timeout=self.race_start_timeout if start_timeout is None else start_timeout,
            race_seconds=self.race_seconds,
            poll_seconds=self.race_poll_seconds, min_measure_seconds=self.race_min_measure_seconds,
            settle_seconds=self.race_settle_seconds
        )
        results.extend(entrant.describe() for entrant in entrants)

        return (winner.data if winner is not None else None), results

    async def find_sources(self, release, formats=None, prefer="balanced", min_bitrate=0, wait_seconds=8,
                           before_search=None):
        """Searches for a release. Returns the folders that contain it (``sources``, best
        first), those that contain part of it (``partial``), and what was searched.

        ``before_search`` is awaited before each search, to allow spacing them out.
        """

        prefs = make_preferences(
            formats=formats if formats is not None else ["flac", "mp3"], prefer=prefer, min_bitrate=min_bitrate,
            max_per_user=0
        )
        automation = self.runner.automation
        found = {}
        searches = []

        for query in build_search_queries(release):
            if before_search is not None:
                await before_search()

            token = await self.runner.call_async(automation.start_search, query)
            await self.runner.wait_for_search_results(token, wait_seconds)
            ranked = await self.runner.call_async(automation.rank_albums, token, prefs, MAX_FOLDERS_PER_SEARCH)
            await self.runner.call_async(automation.stop_search, token)

            searches.append({
                "query": query,
                "token": token,
                "num_users_responded": ranked["search"]["num_users_responded"],
                "num_folders": ranked["num_matching_folders"]
            })

            # Search results are incomplete, this only decides which folders are worth asking about
            candidates = []

            for folder in ranked["albums"]:
                if (folder["username"], folder["folder_path"]) in found:
                    continue

                files = [(item["basename"], item["length_seconds"] or 0) for item in folder["files"]
                         if is_file_wanted(item["basename"], formats)]
                match = match_folder(release, folder["folder_path"], files)

                if match.verdict != Verdict.MISMATCH or match.title_in_path or match.catalog_number_in_path:
                    candidates.append((match.score, folder["score"], folder))

            candidates.sort(key=lambda item: (-item[0], -item[1]))
            sources = []

            async with anyio.create_task_group() as task_group:
                for _score, _rank_score, folder in candidates[:MAX_LISTINGS_PER_SEARCH]:
                    task_group.start_soon(self._list_folder, folder, release, sources, formats)

            for source in sources:
                found[(source["username"], source["folder_path"])] = source

            num_acceptable = sum(
                1 for source in found.values() if source["match"]["verdict"] in Verdict.ACCEPTABLE
            )

            if num_acceptable >= ENOUGH_SOURCES:
                break

        ordered = sorted(found.values(), key=_source_order)

        return {
            "sources": [item for item in ordered if item["match"]["verdict"] in Verdict.ACCEPTABLE][:MAX_SOURCES],
            "partial": [item for item in ordered if item["match"]["verdict"] == Verdict.PARTIAL][:MAX_SOURCES],
            "searches": searches
        }
