# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""MCP tools that work from what music catalogs know: downloading the album a song
is on, and collecting whole record labels and discographies through background jobs."""

from __future__ import annotations

from functools import partial
from typing import Literal

from nicotine_mcp.catalog import CatalogError
from nicotine_mcp.catalog import DISCOGS
from nicotine_mcp.job_models import PauseMode
from nicotine_mcp.matching import assess_album_folder
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.portable import export_running
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.sourcing import pick_probe_file
from nicotine_mcp.tool_params import AcceptPartial
from nicotine_mcp.tool_params import AlsoArtistId
from nicotine_mcp.tool_params import AlsoLabelId
from nicotine_mcp.tool_params import ArtistFilter
from nicotine_mcp.tool_params import ArtistId
from nicotine_mcp.tool_params import AudioOnly
from nicotine_mcp.tool_params import CatalogPrefix
from nicotine_mcp.tool_params import described
from nicotine_mcp.tool_params import DownloadFolder
from nicotine_mcp.tool_params import ExcludeKinds
from nicotine_mcp.tool_params import FlacOnly
from nicotine_mcp.tool_params import Formats
from nicotine_mcp.tool_params import IncludeAppearances
from nicotine_mcp.tool_params import JobDownloadFolder
from nicotine_mcp.tool_params import JobId
from nicotine_mcp.tool_params import JobMode
from nicotine_mcp.tool_params import Kinds
from nicotine_mcp.tool_params import LabelId
from nicotine_mcp.tool_params import Limit
from nicotine_mcp.tool_params import MaxActiveReleases
from nicotine_mcp.tool_params import MaxPerUploader
from nicotine_mcp.tool_params import MaxReleases
from nicotine_mcp.tool_params import MinBitrate
from nicotine_mcp.tool_params import MinSpeedKbps
from nicotine_mcp.tool_params import Prefer
from nicotine_mcp.tool_params import ReleaseId
from nicotine_mcp.tool_params import ReleaseInJob
from nicotine_mcp.tool_params import RetryMissingHours
from nicotine_mcp.tool_params import Sample
from nicotine_mcp.tool_params import SearchIntervalSeconds
from nicotine_mcp.tool_params import SkipOwned
from nicotine_mcp.tool_params import StartTimeoutSeconds
from nicotine_mcp.tool_params import WaitForAlbumStart
from nicotine_mcp.tool_params import WaitSeconds
from nicotine_mcp.tool_params import YearFrom
from nicotine_mcp.tool_params import YearTo


def register_collection_tools(tool, runner, preferences, run_search):
    """Registers the tools on a server, ``tool`` being the decorator that does so."""

    automation = runner.automation

    async def catalog_call(func, *args, **kwargs):
        """Catalog lookups can block for a long time because of rate limits."""

        try:
            return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))

        except (CatalogError, ValueError) as error:
            raise ToolError(str(error)) from error

    async def download_from_sources(sources, wait_for_start_seconds, download_folder, audio_only, formats=None):
        """Races folders in groups until one is transferring, and downloads the fastest.
        Returns (source, enqueued, attempts)."""

        attempts = []
        remaining = list(sources)

        while remaining:
            entrants = await runner.sourcer.pick_sources(remaining)
            remaining = [source for source in remaining if source not in entrants]
            winner, results = await runner.sourcer.race_sources(entrants, download_folder, wait_for_start_seconds)
            attempts.append({"race": results})

            if winner is None:
                continue

            try:
                enqueued = await runner.request_folder_download(
                    winner["username"], winner["folder_path"], download_folder, audio_only, formats
                )

            except ToolError as error:
                attempts.append({"source": winner["username"], "error": str(error)})
                continue

            return winner, enqueued, attempts

        return None, None, attempts

    async def download_verified_release(release_id, formats, prefer, min_bitrate, wait_seconds, wait_for_start_seconds,
                                        download_folder, audio_only, skip_owned, accept_partial):
        """Downloads a catalog release, from a folder checked against its tracklist."""

        release = await catalog_call(runner.catalog.release_details, release_id)

        if skip_owned:
            local_path = await catalog_call(runner.library.find_release, release, formats)

            if local_path:
                return {"success": True, "already_owned": True, "local_path": local_path, "release": release.summary()}

        found = await runner.sourcer.find_sources(
            release, formats=formats, prefer=prefer, min_bitrate=min_bitrate, wait_seconds=wait_seconds
        )
        sources = found["sources"] + (found["partial"] if accept_partial else [])
        result = {"success": False, "release": release.summary(),
                  "tracks": [item.title for item in release.tracks or []],
                  "searches": found["searches"], "partial_copies": found["partial"]}

        if not sources:
            result["error"] = "No shared folder contains this release" + (
                ". Some have part of it, see partial_copies, accept_partial downloads those."
                if found["partial"] else "."
            )
            return result

        source, enqueued, attempts = await download_from_sources(
            sources, wait_for_start_seconds, download_folder, audio_only, formats
        )
        result["attempts"] = attempts

        if source is None:
            result["error"] = "None of the folders started transferring in time."
            return result

        result.update(success=True, source=source, download=enqueued)

        # Keeps a slow transfer from staying slow, it may be a while until anybody looks at it again
        result["job_id"] = runner.jobs.watch_download(
            f"{release.artist} – {release.title}", release, source, sources, enqueued, formats=formats,
            prefer=prefer, min_bitrate=min_bitrate, audio_only=audio_only
        )
        return result

    async def download_source_folder(artist, title, formats, prefer, wait_seconds, wait_for_start_seconds,
                                     download_folder, audio_only):
        """Downloads the folder a song was found in, if that folder looks like one album."""

        prefs = preferences(formats, prefer, 0, False, None, 0, 0, 0)
        token = await run_search(f"{artist} {title}", "global", None, wait_seconds)
        ranked = await runner.call_async(automation.rank_tracks, token, prefs, 30)
        rejected = []
        seen_folders = set()

        for track in ranked["tracks"]:
            key = (track["username"], track["folder_path"])

            if key in seen_folders or len(seen_folders) >= 5:
                continue

            seen_folders.add(key)

            try:
                listing = await runner.request_folder_listing(*key)

            except (ToolError, TimeoutError, RuntimeError) as error:
                rejected.append({"username": key[0], "folder_path": key[1], "reasons": [str(error)]})
                continue

            if "error" in listing:
                rejected.append({"username": key[0], "folder_path": key[1], "reasons": [listing["error"]]})
                continue

            wanted_files = [item for item in listing["files"] if is_file_wanted(item["basename"], formats)]
            files = [(item["basename"], item["length"]) for item in wanted_files]
            assessment = assess_album_folder(key[1], files)

            if not assessment["is_album"]:
                rejected.append({"username": key[0], "folder_path": key[1], "reasons": assessment["reasons"]})
                continue

            source = {"username": key[0], "folder_path": key[1], "assessment": assessment,
                      "probe_file": pick_probe_file(wanted_files)}
            chosen, enqueued, attempts = await download_from_sources(
                [source], wait_for_start_seconds, download_folder, audio_only, formats
            )

            if chosen is not None:
                return {"success": True, "source": chosen, "download": enqueued, "attempts": attempts,
                        "rejected_folders": rejected}

            rejected.append({"username": key[0], "folder_path": key[1], "reasons": ["did not start in time"]})

        return {"success": False, "rejected_folders": rejected}

    # Catalogs #

    @tool()
    async def find_track_albums(
        artist: described(str, "Artist of the song."),
        title: described(str, "Title of the song.")
    ) -> dict:
        """Looks up which releases a song appears on in a music catalog, the most canonical one first
        (exact title before remixes; albums before EPs, singles and compilations; oldest first).

        Pass a release_id to download_album_for_track or download_release to get a specific one.
        """

        return {"albums": await catalog_call(runner.catalog.find_track_albums, artist, title)}

    @tool()
    async def download_album_for_track(
        artist: described(str, "Artist of the song."),
        title: described(str, "Title of the song."),
        release_id: described(str | None, "A specific release from find_track_albums, instead of the best one.") = None,
        kinds: Kinds = None,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        wait_seconds: WaitSeconds = 8,
        wait_for_start_seconds: WaitForAlbumStart = 30,
        download_folder: DownloadFolder = None,
        audio_only: AudioOnly = False,
        skip_owned: SkipOwned = True,
        accept_partial: AcceptPartial = False
    ) -> dict:
        """Downloads the album a song is on.

        Asks a music catalog which album that is, then only accepts a shared folder that matches the
        album's tracklist. If the catalog doesn't know the song or nobody shares its album, falls back
        to the folder the song itself was found in, but only if that folder looks like one album and
        not like a collection of unrelated files. The returned method says which way it went:
        'catalog', or 'source_folder' when the album's identity could not be checked.
        """

        formats = ["flac"] if flac_only else formats
        warnings = []
        albums = []

        if release_id:
            albums = [{"release_id": release_id}]

        else:
            try:
                albums = await anyio.to_thread.run_sync(runner.catalog.find_track_albums, artist, title)

            except CatalogError as error:
                warnings.append(f"Could not ask the catalog: {error}")

            except ValueError as error:
                raise ToolError(str(error)) from error

            if kinds:
                albums = [album for album in albums if album["kind"] in {kind.lower() for kind in kinds}]

        tried = []

        # The runner-up is usually a single or compilation, anything after that is rarely what was asked for
        for album in albums[:2]:
            result = await download_verified_release(
                album["release_id"], formats, prefer, min_bitrate, wait_seconds, wait_for_start_seconds,
                download_folder, audio_only, skip_owned, accept_partial
            )

            if result["success"]:
                return {**result, "method": "catalog", "other_albums": albums[1:6], "warnings": warnings}

            tried.append(result)

        if release_id:
            return {**tried[0], "method": "catalog", "warnings": warnings}

        fallback = await download_source_folder(
            artist, title, formats, prefer, wait_seconds, wait_for_start_seconds, download_folder, audio_only
        )
        result = {**fallback, "method": "source_folder", "catalog_albums": albums[:6], "tried_albums": tried,
                  "warnings": warnings}

        if not fallback["success"]:
            result["error"] = "Could not find the album: no shared folder matches the catalog's tracklists, and " \
                              "the folders this song was found in don't look like albums."

        return result

    @tool()
    async def download_release(
        release_id: ReleaseId,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        wait_seconds: WaitSeconds = 8,
        wait_for_start_seconds: WaitForAlbumStart = 30,
        download_folder: DownloadFolder = None,
        audio_only: AudioOnly = False,
        skip_owned: SkipOwned = True,
        accept_partial: AcceptPartial = False
    ) -> dict:
        """Downloads one release known from a catalog (from find_track_albums or get_label_releases),
        accepting only a shared folder that matches its tracklist."""

        return await download_verified_release(
            release_id, ["flac"] if flac_only else formats, prefer, min_bitrate, wait_seconds,
            wait_for_start_seconds, download_folder, audio_only, skip_owned, accept_partial
        )

    @tool()
    async def find_label(
        name: described(str, "Name of the record label."),
        source: described(Literal["all", "musicbrainz", "discogs"], "Which catalog to ask.") = "all"
    ) -> dict:
        """Finds record labels by name in music catalogs. Many labels share a name, and the same
        label is usually in both catalogs, each lacking releases the other has. Use both: pass the
        MusicBrainz ID as label_id and the Discogs one as also_label_id to the tools that follow.
        Check the description, area and year, or the releases, before settling on one."""

        return await catalog_call(runner.catalog.find_labels, name, source)

    async def describe_listing(listing, limit):
        """Replaces the releases of a listing with what an agent sees of them, including what is on disk."""

        releases = listing.pop("releases")
        listing["releases"] = []

        for release in releases[:max(1, limit)]:
            summary = release.summary()
            summary["owned_at"] = await catalog_call(runner.library.find_release, release)
            listing["releases"].append(summary)

        listing["num_owned"] = sum(1 for release in listing["releases"] if release["owned_at"])
        listing["library_folders_unavailable"] = runner.library.unavailable_folders

    @tool()
    async def get_label_releases(
        label_id: LabelId,
        also_label_id: AlsoLabelId = None,
        kinds: Kinds = None,
        exclude_kinds: ExcludeKinds = None,
        year_from: YearFrom = None,
        year_to: YearTo = None,
        artist: ArtistFilter = None,
        catalog_prefix: CatalogPrefix = None,
        limit: Limit = 200
    ) -> dict:
        """Lists what a record label has released according to its catalog, newest first, with
        editions of the same release merged. Doesn't search Soulseek, and is safe to call freely."""

        listing = await catalog_call(
            runner.catalog.label_releases, label_id, also_label_id=also_label_id, kinds=kinds,
            exclude_kinds=exclude_kinds, year_from=year_from, year_to=year_to, artist=artist,
            catalog_prefix=catalog_prefix
        )
        await describe_listing(listing, limit)

        if label_id.startswith(f"{DISCOGS}:") or (also_label_id or "").startswith(f"{DISCOGS}:"):
            listing["note"] = ("Discogs listings include releases this label was only credited on, and those are only "
                               "removed once a job fetches their tracklists. catalog_prefix can clean them up.")

        return listing

    @tool()
    async def find_artist(
        name: described(str, "Name of the artist."),
        source: described(Literal["all", "musicbrainz", "discogs"], "Which catalog to ask.") = "all"
    ) -> dict:
        """Finds artists by name in music catalogs. Many artists share a name, and the same artist
        is usually in both catalogs, each lacking releases the other has. Use both: pass the
        MusicBrainz ID as artist_id and the Discogs one as also_artist_id to the tools that follow.
        Check the description, area and year, or the releases, before settling on one."""

        return await catalog_call(runner.catalog.find_artists, name, source)

    @tool()
    async def get_artist_releases(
        artist_id: ArtistId,
        also_artist_id: AlsoArtistId = None,
        include_appearances: IncludeAppearances = False,
        kinds: Kinds = None,
        exclude_kinds: ExcludeKinds = None,
        year_from: YearFrom = None,
        year_to: YearTo = None,
        limit: Limit = 200
    ) -> dict:
        """Lists an artist's discography according to its catalog, newest first, with editions of
        the same release merged. Doesn't search Soulseek, and is safe to call freely. Aliases and
        side projects count as artists of their own."""

        listing = await catalog_call(
            runner.catalog.artist_releases, artist_id, also_artist_id=also_artist_id,
            include_appearances=include_appearances, kinds=kinds, exclude_kinds=exclude_kinds,
            year_from=year_from, year_to=year_to
        )
        await describe_listing(listing, limit)

        if kinds and (also_artist_id or artist_id).startswith(f"{DISCOGS}:"):
            listing["note"] = ("Discogs doesn't say what kind most of an artist's releases are, so those count as "
                               "'other' and a filter on kinds leaves them out.")

        return listing

    # Jobs #

    def start_catalog_job(name, listing, max_releases, sample, filters, flac_only, formats, label_id=None,
                          artist_id=None, **options):
        """Starts a job for a listing of a label or an artist."""

        releases = listing["releases"]

        if max_releases and len(releases) > max_releases:
            if sample:
                step = len(releases) / max_releases
                releases = [releases[int(index * step)] for index in range(max_releases)]
            else:
                releases = releases[:max_releases]

        filters = {**filters, "max_releases": max_releases, "sample": sample}

        try:
            job = runner.jobs.create_job(
                name, releases, label_id=label_id, artist_id=artist_id, num_in_catalog=listing["num_releases"],
                filters={key: value for key, value in filters.items() if value},
                formats=["flac"] if flac_only else formats, **options
            )

        except ValueError as error:
            raise ToolError(str(error)) from error

        job["num_releases_in_catalog"] = listing["num_releases"]

        if job["num_releases"] < listing["num_releases"]:
            job["note"] = (f"This job only covers {job['num_releases']} of the {listing['num_releases']} releases "
                           f"in the catalog. Tell the user, so it isn't mistaken for all of them.")

        return job

    @tool()
    async def start_label_job(
        label_id: LabelId,
        label_name: described(str, "Name of the label, used for the job and the download subfolder."),
        also_label_id: AlsoLabelId = None,
        mode: JobMode = "preview",
        kinds: Kinds = None,
        exclude_kinds: ExcludeKinds = None,
        year_from: YearFrom = None,
        year_to: YearTo = None,
        artist: ArtistFilter = None,
        catalog_prefix: CatalogPrefix = None,
        max_releases: MaxReleases = None,
        sample: Sample = False,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        download_folder: JobDownloadFolder = None,
        audio_only: AudioOnly = False,
        skip_owned: SkipOwned = True,
        accept_partial: AcceptPartial = False,
        max_active_releases: MaxActiveReleases = 3,
        max_per_uploader: MaxPerUploader = 1,
        start_timeout_seconds: StartTimeoutSeconds = 900,
        min_speed_kbps: MinSpeedKbps = 300,
        retry_missing_hours: RetryMissingHours = 24,
        search_interval_seconds: SearchIntervalSeconds = 10
    ) -> dict:
        """Starts a background job that works through a label's releases: skips those on disk,
        searches for the rest, and accepts only folders that match the tracklists.

        Returns immediately. A job takes roughly 10-30 seconds per release, so large labels take
        hours. Check on it with get_job. Run a preview first and tell the user what it found, then
        use start_job_downloads, unless they explicitly asked to download right away.
        """

        filters = {"kinds": kinds, "exclude_kinds": exclude_kinds, "year_from": year_from, "year_to": year_to,
                   "artist": artist, "catalog_prefix": catalog_prefix}
        listing = await catalog_call(
            runner.catalog.label_releases, label_id, also_label_id=also_label_id, **filters
        )

        return start_catalog_job(
            label_name, listing, max_releases, sample, filters,
            flac_only, formats, mode=mode, download_folder=download_folder, prefer=prefer, min_bitrate=min_bitrate,
            audio_only=audio_only, accept_partial=accept_partial, skip_owned=skip_owned,
            max_active_releases=max(1, max_active_releases), max_per_uploader=max(1, max_per_uploader),
            start_timeout=max(60, start_timeout_seconds), min_speed=max(0, min_speed_kbps) * 1000,
            retry_missing_hours=max(0, retry_missing_hours), search_interval=max(1, search_interval_seconds),
            label_id=label_id
        )

    @tool()
    async def start_artist_job(
        artist_id: ArtistId,
        artist_name: described(str, "Name of the artist, used for the job and the download subfolder."),
        also_artist_id: AlsoArtistId = None,
        mode: JobMode = "preview",
        include_appearances: IncludeAppearances = False,
        kinds: Kinds = None,
        exclude_kinds: ExcludeKinds = None,
        year_from: YearFrom = None,
        year_to: YearTo = None,
        max_releases: MaxReleases = None,
        sample: Sample = False,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        download_folder: JobDownloadFolder = None,
        audio_only: AudioOnly = False,
        skip_owned: SkipOwned = True,
        accept_partial: AcceptPartial = False,
        max_active_releases: MaxActiveReleases = 3,
        max_per_uploader: MaxPerUploader = 1,
        start_timeout_seconds: StartTimeoutSeconds = 900,
        min_speed_kbps: MinSpeedKbps = 300,
        retry_missing_hours: RetryMissingHours = 24,
        search_interval_seconds: SearchIntervalSeconds = 10
    ) -> dict:
        """Starts a background job that works through an artist's discography: skips what is on disk,
        searches for the rest, and accepts only folders that match the tracklists.

        Returns immediately. A job takes roughly 10-30 seconds per release. Check on it with get_job.
        Run a preview first and tell the user what it found, then use start_job_downloads, unless
        they explicitly asked to download right away.
        """

        filters = {"kinds": kinds, "exclude_kinds": exclude_kinds, "year_from": year_from, "year_to": year_to}
        listing = await catalog_call(
            runner.catalog.artist_releases, artist_id, also_artist_id=also_artist_id,
            include_appearances=include_appearances, **filters
        )

        if include_appearances:
            filters["include_appearances"] = True

        return start_catalog_job(
            artist_name, listing, max_releases, sample, filters,
            flac_only, formats, mode=mode, download_folder=download_folder, prefer=prefer, min_bitrate=min_bitrate,
            audio_only=audio_only, accept_partial=accept_partial, skip_owned=skip_owned,
            max_active_releases=max(1, max_active_releases), max_per_uploader=max(1, max_per_uploader),
            start_timeout=max(60, start_timeout_seconds), min_speed=max(0, min_speed_kbps) * 1000,
            retry_missing_hours=max(0, retry_missing_hours), search_interval=max(1, search_interval_seconds),
            artist_id=artist_id
        )

    def job_call(func, *args, **kwargs):

        try:
            return func(*args, **kwargs)

        except (KeyError, ValueError) as error:
            raise ToolError(str(error.args[0] if error.args else error)) from error

    @tool()
    async def get_job(
        job_id: JobId,
        state: described(
            Literal["pending", "sourced", "downloading", "finished", "owned", "partial", "not_found", "failed",
                    "not_on_label", "cancelled"] | None,
            "Only list releases in this state."
        ) = None,
        limit: Limit = 100,
        include_sources: described(bool, "Include every candidate folder and how it matched.") = False
    ) -> dict:
        """Returns a job's progress, how many releases are in each state and what it found for each."""

        return job_call(runner.jobs.get_job, job_id, state, limit, include_sources)

    @tool()
    async def list_jobs() -> dict:
        """Lists all jobs with their progress, newest first."""

        return {"jobs": runner.jobs.list_jobs()}

    @tool()
    async def start_job_downloads(
        job_id: JobId,
        accept_partial: AcceptPartial = False,
        flac_only: FlacOnly = False
    ) -> dict:
        """Starts downloading what a preview found. Releases it didn't get to yet are searched for too.
        With flac_only, releases only found in other formats are searched for again in FLAC. The
        preview's num_available_flac says how many have a FLAC copy already."""

        return job_call(runner.jobs.start_downloads, job_id, accept_partial, flac_only)

    @tool()
    async def pause_job(
        job_id: JobId,
        now: described(
            bool, "Stop the albums that have started right away, instead of letting them finish. "
                  "They continue from where they were on resuming."
        ) = False
    ) -> dict:
        """Stops a job from searching and starting downloads. By default the albums that have started
        finish first. Albums only waiting in an uploader's queue leave it, and queue again on resuming."""

        return job_call(runner.jobs.pause_job, job_id, PauseMode.NOW if now else PauseMode.AFTER_ALBUM)

    @tool()
    async def resume_job(job_id: JobId) -> dict:
        """Continues a paused job."""

        return job_call(runner.jobs.resume_job, job_id)

    @tool()
    async def skip_release(job_id: JobId, release_id: ReleaseInJob) -> dict:
        """Leaves a release out of a job, and removes its unfinished downloads. Finished files stay on disk."""

        try:
            return await runner.jobs.skip_release(job_id, release_id)

        except (KeyError, ValueError) as error:
            raise ToolError(str(error.args[0] if error.args else error)) from error

    @tool()
    async def prioritize_release(job_id: JobId, release_id: ReleaseInJob) -> dict:
        """Makes a release the next one a job works on. Also brings back one that was skipped or not found."""

        return job_call(runner.jobs.prioritize_release, job_id, release_id)

    @tool()
    async def export_state(
        path: described(
            str | None, "File to write. Defaults to nicotine-mcp-export.json next to the download folder, which "
                        "puts it on the drive. Can't be in a shared folder."
        ) = None
    ) -> dict:
        """Writes all jobs, the download list and shared settings to a file, for another user or Mac with the same
        drive. They run `nicotine-mcp import FILE`. The Soulseek login is not included. This server has to
        stop before theirs starts, or both download the same albums."""

        try:
            return await anyio.to_thread.run_sync(export_running, runner, path)

        except (OSError, ValueError) as error:
            raise ToolError(str(error)) from error

    @tool()
    async def retry_job_missing(job_id: JobId) -> dict:
        """Searches right away for the releases a job didn't find, instead of waiting for its next retry."""

        return job_call(runner.jobs.retry_missing, job_id)

    @tool()
    async def remove_job(job_id: JobId) -> dict:
        """Forgets a finished, cancelled or previewed job. Its files stay on disk."""

        return job_call(runner.jobs.remove_job, job_id)

    @tool()
    async def cancel_job(job_id: JobId) -> dict:
        """Stops a job and removes its unfinished downloads. Finished files stay on disk."""

        try:
            return await runner.jobs.cancel_job(job_id)

        except KeyError as error:
            raise ToolError(str(error.args[0])) from error
