# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""MCP server exposing Nicotine+ search and download tools to AI agents.

Requires the ``mcp`` Python package (https://pypi.org/project/mcp/).
"""

from __future__ import annotations

import os
import signal
import sys
import time

from inspect import cleandoc

from pynicotine.config import config
from pynicotine.core import core

from nicotine_mcp import DEFAULT_UI_PORT
from nicotine_mcp.automation import Automation
from nicotine_mcp.automation import DownloadState
from nicotine_mcp.automation import make_preferences
from nicotine_mcp.job_models import PauseMode
from nicotine_mcp.catalog import Catalog
from nicotine_mcp.collection_tools import register_collection_tools
from nicotine_mcp.jobs import JOBS_FILE_NAME
from nicotine_mcp.jobs import JobManager
from nicotine_mcp.library import LibraryIndex
from nicotine_mcp.ranking import DEFAULT_FORMATS
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.mcp_compat import MCPServerClass
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.runner import CoreRunner
from nicotine_mcp.sourcing import ReleaseSourcer
from nicotine_mcp.sourcing import pick_probe_file
from nicotine_mcp.portable import remove_server_info
from nicotine_mcp.portable import write_server_info
from nicotine_mcp.speed import Entrant
from nicotine_mcp.speed import pick_entrants
from nicotine_mcp.speed import race_downloads
from nicotine_mcp.tool_params import AlbumQuery
from nicotine_mcp.tool_params import AudioOnly
from nicotine_mcp.tool_params import described
from nicotine_mcp.tool_params import DownloadFolder
from nicotine_mcp.tool_params import FlacOnly
from nicotine_mcp.tool_params import FolderPath
from nicotine_mcp.tool_params import Formats
from nicotine_mcp.tool_params import GroupBy
from nicotine_mcp.tool_params import Limit
from nicotine_mcp.tool_params import MaxAttempts
from nicotine_mcp.tool_params import MaxLength
from nicotine_mcp.tool_params import MaxPerUser
from nicotine_mcp.tool_params import MaxQueue
from nicotine_mcp.tool_params import MinBitrate
from nicotine_mcp.tool_params import MinLength
from nicotine_mcp.tool_params import OptionalFolderPath
from nicotine_mcp.tool_params import OptionalUsername
from nicotine_mcp.tool_params import Prefer
from nicotine_mcp.tool_params import RemoveEntry
from nicotine_mcp.tool_params import RequireFreeSlot
from nicotine_mcp.tool_params import SearchMode
from nicotine_mcp.tool_params import SearchUser
from nicotine_mcp.tool_params import StateFilter
from nicotine_mcp.tool_params import TrackQuery
from nicotine_mcp.tool_params import Username
from nicotine_mcp.tool_params import VirtualPath
from nicotine_mcp.tool_params import WaitForAlbumStart
from nicotine_mcp.tool_params import WaitForTrackStart
from nicotine_mcp.tool_params import WaitSeconds

SERVER_NAME = "nicotine-plus"

# More candidates than fit in a race, as some are left out for being slow or of lesser quality
RACE_CANDIDATES = 12
SERVER_INSTRUCTIONS = """Nicotine+ gives you access to the Soulseek peer-to-peer network to search for and
download music.

Typical workflows:

- Single track: call download_best_track(query) and you are done. It searches, ranks
  the results (free upload slot and fast uploader first, then audio quality), starts the
  best few at once, keeps the one that is really the fastest and drops the others.
- Album: call download_best_album(query). It picks the best shared folder, fetches the
  full folder listing from the uploader and downloads every file in it.
- Manual: search_tracks / search_albums return ranked candidates with scores and
  reasons. Pick one and call download_file or download_album with the username and
  virtual path. Re-run get_search_results with the same token later, more results
  usually keep arriving for a while.
- Track progress with get_downloads or block with wait_for_downloads. Finished files
  carry a local_path.
- The album a song is on: call download_album_for_track(artist, title). It asks a music
  catalog which album that is, and only downloads a folder after checking it against the
  album's tracklist. Use find_track_albums first if the user may want a specific release.
- A whole record label: labels are rarely part of shared file names, so don't search for
  them. Call find_label(name), and confirm the right one with the user if several could
  match. The label is usually in both catalogs and each lacks releases the other has, so
  pass one ID as label_id and the other as also_label_id from here on. Check the catalog
  with get_label_releases. Then call start_label_job to run a preview in the background: it finds out
  what is available without downloading anything. Report the result from get_job to the
  user, and call start_job_downloads(job_id) once they agree. Jobs keep running between
  tool calls and survive restarts, there is no need to wait for them.
- An artist's whole discography: works the same way with find_artist, get_artist_releases
  and start_artist_job, passing the artist's ID in both catalogs as artist_id and
  also_artist_id. Compilations they only appear on are left out unless include_appearances
  is set. Aliases and side projects count as artists of their own.
- Controlling a job: pause_job finishes the albums that have started by default, and with
  now=true stops them where they are. skip_release leaves one out, and prioritize_release
  makes one the next to go, or brings it back.
- Handing over to another user or Mac with the same drive: export_state writes everything
  to a file on the drive, and the other user runs `nicotine-mcp import FILE`.

When FLAC is asked for, pass flac_only=true to any download tool. It's strict: nothing else
is downloaded, and a copy in another format doesn't count as already owned.

Notes: queries work best as "artist title" or "artist album" without punctuation.
Virtual paths use backslashes and belong to a specific user. Formats are file
extensions ordered by preference ("flac", "mp3", or "lossless", "lossy", "any").
Downloads land in the configured Nicotine+ download folder."""


def track_quality(track):
    return 10000 if track["is_lossless"] else (track["bitrate_kbps"] or 1)


def build_server(runner):
    """Creates the MCP server and registers all tools."""

    if MCPServerClass is None:
        raise ImportError("The 'mcp' Python package is required to run the MCP server: pip install mcp")

    server = MCPServerClass(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    automation = runner.automation

    def tool():
        """Registers a tool, using its cleaned up docstring as description."""

        def decorator(func):
            return server.tool(description=cleandoc(func.__doc__ or ""))(func)

        return decorator

    def preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, min_length, max_length,
                    max_per_user=3):
        try:
            return make_preferences(
                formats=formats if formats is not None else list(DEFAULT_FORMATS), prefer=prefer,
                min_bitrate=min_bitrate, require_free_slot=require_free_slot, max_queue=max_queue,
                min_length=min_length, max_length=max_length, max_per_user=max_per_user
            )
        except ValueError as error:
            raise ToolError(str(error)) from error

    async def run_search(query, mode, username, wait_seconds):
        token = await runner.call_async(automation.start_search, query, mode, username)
        await runner.wait_for_search_results(token, wait_seconds)
        return token

    # Status #

    def recent_log(limit=10):
        return list(runner.recent_log)[-limit:]

    @tool()
    async def get_status() -> dict:
        """Returns connection status, configured username, download folders, download counts and the
        most recent Nicotine+ log messages (useful to diagnose connection problems)."""

        status = await runner.call_async(automation.get_status)
        status["dashboard_url"] = runner.dashboard_url
        status["recent_log"] = recent_log()
        return status

    @tool()
    async def connect(
        timeout_seconds: described(int, "How long to wait for the login to complete.") = 20
    ) -> dict:
        """Connects to the Soulseek server if not already connected, and waits for the login."""

        status = await runner.call_async(automation.get_status)

        if status["connected"]:
            return status

        if status["needs_credentials"]:
            raise ToolError("No Soulseek username/password configured. Set the NICOTINE_USERNAME and "
                            "NICOTINE_PASSWORD environment variables or configure an account in Nicotine+.")

        await runner.call_async(core.connect)
        started = time.monotonic()

        while time.monotonic() - started < max(1, timeout_seconds):
            await anyio.sleep(0.5)
            status = await runner.call_async(automation.get_status)

            if status["connected"]:
                return status

        raise ToolError("Timed out waiting for the Soulseek login. Check the credentials and network connection. "
                        "Recent log messages: " + " | ".join(recent_log(5)))

    # Searching #

    @tool()
    async def search_tracks(
        query: TrackQuery,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        require_free_slot: RequireFreeSlot = False,
        max_queue: MaxQueue = None,
        min_length: MinLength = 0,
        max_length: MaxLength = 0,
        wait_seconds: WaitSeconds = 10,
        limit: Limit = 20,
        max_per_user: MaxPerUser = 3,
        mode: SearchMode = "global",
        username: SearchUser = None
    ) -> dict:
        """Searches the Soulseek network for individual tracks and returns the best candidates, ranked.

        Each candidate includes the uploader, virtual path, format, quality, size, whether the
        uploader has a free slot, their queue length and upload speed, and a score with details.
        Pass the returned token to get_search_results later to see results that arrived afterwards.
        """

        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, min_length, max_length,
                            max_per_user)
        token = await run_search(query, mode, username, wait_seconds)
        return await runner.call_async(automation.rank_tracks, token, prefs, limit)

    @tool()
    async def search_albums(
        query: AlbumQuery,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        require_free_slot: RequireFreeSlot = False,
        max_queue: MaxQueue = None,
        wait_seconds: WaitSeconds = 12,
        limit: Limit = 10,
        max_per_user: MaxPerUser = 2,
        mode: SearchMode = "global",
        username: SearchUser = None
    ) -> dict:
        """Searches for albums (shared folders) and returns the best folders, ranked.

        Results are grouped by uploader and folder. Folders with more matching audio files, a
        single consistent format, a free upload slot and a fast uploader score higher. Use
        download_album with the username and folder_path of a result to download it entirely.
        """

        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, 0, 0, max_per_user)
        token = await run_search(query, mode, username, wait_seconds)
        return await runner.call_async(automation.rank_albums, token, prefs, limit)

    @tool()
    async def get_search_results(
        token: described(int, "Search token returned by a previous search."),
        group_by: GroupBy = "track",
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        require_free_slot: RequireFreeSlot = False,
        max_queue: MaxQueue = None,
        min_length: MinLength = 0,
        max_length: MaxLength = 0,
        limit: Limit = 20,
        max_per_user: MaxPerUser = 3
    ) -> dict:
        """Re-ranks the results collected so far for an earlier search, with new filters if desired."""

        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, min_length, max_length,
                            max_per_user)

        if group_by == "album":
            return await runner.call_async(automation.rank_albums, token, prefs, limit)

        return await runner.call_async(automation.rank_tracks, token, prefs, limit)

    # Downloads #

    @tool()
    async def download_file(
        username: Username,
        virtual_path: VirtualPath,
        download_folder: DownloadFolder = None
    ) -> dict:
        """Downloads a single file from a user. Returns the download entry, use get_downloads to track it."""

        return await runner.call_async(
            automation.download_file, username, virtual_path, download_folder=download_folder
        )

    @tool()
    async def download_best_track(
        query: TrackQuery,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        require_free_slot: RequireFreeSlot = False,
        max_queue: MaxQueue = None,
        min_length: MinLength = 0,
        max_length: MaxLength = 0,
        wait_seconds: WaitSeconds = 10,
        max_attempts: MaxAttempts = 3,
        wait_for_start_seconds: WaitForTrackStart = 20,
        download_folder: DownloadFolder = None,
        mode: SearchMode = "global",
        username: SearchUser = None
    ) -> dict:
        """Searches for a track and downloads it from the fastest uploader: the best few candidates start
        at once, and after a few seconds of measuring the fastest is kept. What uploaders report about
        their speed is unreliable, so this finds out for real. Falls back to the next few candidates
        when none of them start in time (e.g. the uploaders are queued or unreachable).

        Returns the chosen track, its download entry and a log of races. The download continues in
        the background, use wait_for_downloads to block until it finishes.
        """

        formats = ["flac"] if flac_only else formats
        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, min_length, max_length, 1)
        token = await run_search(query, mode, username, wait_seconds)
        ranked = await runner.call_async(automation.rank_tracks, token, prefs, max(RACE_CANDIDATES, max_attempts))
        attempts = []
        remaining = ranked["tracks"]

        if not remaining:
            raise ToolError(f"No results matching the filters for '{query}' "
                            f"({ranked['search']['num_files']} files from "
                            f"{ranked['search']['num_users_responded']} users seen)")

        while remaining:
            speeds = {
                track["username"]: await runner.call_async(automation.get_uploader_speed, track["username"])
                for track in remaining
            }
            candidates = pick_entrants(remaining, speeds, track_quality)
            remaining = [track for track in remaining if track not in candidates]
            entrants = []

            for candidate in candidates:
                await runner.call_async(
                    automation.download_file, candidate["username"], candidate["virtual_path"],
                    download_folder=download_folder
                )
                entrants.append(Entrant(candidate["username"], candidate["virtual_path"], candidate))

            winner = await race_downloads(runner, entrants, start_timeout=wait_for_start_seconds)
            attempts.append({"race": [entrant.describe() for entrant in entrants]})

            if winner is not None:
                download = await runner.call_async(automation.get_download, winner.username, winner.virtual_path)
                return {"success": True, "token": token, "track": winner.data, "download": download,
                        "attempts": attempts}

        return {"success": False, "token": token, "track": None, "download": None, "attempts": attempts,
                "error": "None of the candidates started transferring in time. Try again later, raise "
                         "wait_for_start_seconds or max_attempts, or pick a result manually."}

    @tool()
    async def download_album(
        username: Username,
        folder_path: FolderPath,
        download_folder: DownloadFolder = None,
        audio_only: AudioOnly = False,
        flac_only: FlacOnly = False
    ) -> dict:
        """Downloads an entire folder (album) from a user into a subfolder named after it.

        Requests the full folder listing from the uploader first, so files that did not match the
        search are included too. Falls back to the files seen in search results if the user does
        not respond.
        """

        return await runner.request_folder_download(
            username, folder_path, download_folder, audio_only, ["flac"] if flac_only else None
        )

    @tool()
    async def download_best_album(
        query: AlbumQuery,
        flac_only: FlacOnly = False,
        formats: Formats = None,
        prefer: Prefer = "balanced",
        min_bitrate: MinBitrate = 0,
        require_free_slot: RequireFreeSlot = False,
        max_queue: MaxQueue = None,
        wait_seconds: WaitSeconds = 12,
        max_attempts: MaxAttempts = 2,
        wait_for_start_seconds: WaitForAlbumStart = 30,
        download_folder: DownloadFolder = None,
        audio_only: AudioOnly = False,
        mode: SearchMode = "global",
        username: SearchUser = None
    ) -> dict:
        """Searches for an album and downloads the best shared folder from the fastest uploader: one
        file from each of the best few folders starts at once, and after a few seconds of measuring the
        rest of the fastest one follows. Falls back to the next few when none of them start in time.

        Returns the chosen folder, the enqueued files and a log of races. Downloads continue in
        the background, use wait_for_downloads with the folder_path to block until they finish.
        """

        formats = ["flac"] if flac_only else formats
        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, 0, 0)
        token = await run_search(query, mode, username, wait_seconds)
        ranked = await runner.call_async(automation.rank_albums, token, prefs, max(RACE_CANDIDATES, max_attempts))
        attempts = []

        if not ranked["albums"]:
            raise ToolError(f"No folders matching the filters for '{query}' "
                            f"({ranked['search']['num_files']} files from "
                            f"{ranked['search']['num_users_responded']} users seen)")

        remaining = [{
            **folder,
            "probe_file": pick_probe_file(
                [{"basename": item["basename"], "size": item["size_bytes"]} for item in folder["files"]
                 if is_file_wanted(item["basename"], formats)]
            )
        } for folder in ranked["albums"]]

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

            return {"success": True, "token": token, "album": winner, "download": enqueued, "attempts": attempts}

        return {"success": False, "token": token, "album": None, "download": None, "attempts": attempts,
                "error": "None of the folders started transferring in time. Try again later, raise "
                         "wait_for_start_seconds or max_attempts, or pick a folder manually."}

    @tool()
    async def get_downloads(
        state: StateFilter = "all",
        username: OptionalUsername = None,
        folder_path: OptionalFolderPath = None,
        limit: Limit = 100
    ) -> dict:
        """Lists downloads with their state, progress, speed, queue position and local path when finished.

        Failed downloads with will_retry=true are retried automatically by Nicotine+ after a few minutes.
        """

        return await runner.call_async(automation.get_downloads, state, username, folder_path, limit)

    @tool()
    async def wait_for_downloads(
        username: OptionalUsername = None,
        virtual_paths: described(list[str] | None, "Only wait for these virtual paths.") = None,
        folder_path: OptionalFolderPath = None,
        timeout_seconds: described(int, "Give up waiting after this many seconds (max 600).") = 120
    ) -> dict:
        """Blocks until the selected downloads (all pending ones by default) are finished or failed,
        or the timeout expires. Returns the final state of each download."""

        timeout = min(600, max(1, int(timeout_seconds)))
        started = time.monotonic()

        while True:
            transfers = await runner.call_async(
                automation.select_downloads, username, virtual_paths, folder_path
            )
            downloads = [Automation.describe_transfer(transfer) for transfer in transfers]
            pending = [item for item in downloads if item["state"] not in DownloadState.TERMINAL]
            timed_out = (time.monotonic() - started) >= timeout

            if not pending or timed_out:
                return {
                    "all_finished": all(item["state"] == DownloadState.FINISHED for item in downloads),
                    "timed_out": bool(pending) and timed_out,
                    "num_finished": sum(1 for item in downloads if item["state"] == DownloadState.FINISHED),
                    "num_failed": sum(1 for item in downloads if item["state"] == DownloadState.FAILED),
                    "num_pending": len(pending),
                    "downloads": downloads
                }

            await anyio.sleep(1.0)

    @tool()
    async def cancel_download(
        username: Username,
        virtual_path: VirtualPath,
        remove: RemoveEntry = True
    ) -> dict:
        """Cancels a download, removing it from the list (default) or pausing it."""

        return await runner.call_async(automation.cancel_download, username, virtual_path, remove)

    @tool()
    async def cancel_album(
        username: Username,
        folder_path: FolderPath,
        remove: RemoveEntry = True
    ) -> dict:
        """Cancels every download inside a user's folder (an album), removing the entries (default) or
        pausing them."""

        return await runner.call_async(automation.cancel_folder_downloads, username, folder_path, remove)

    @tool()
    async def clear_finished_downloads() -> dict:
        """Removes finished downloads from the download list. The files stay on disk."""

        return await runner.call_async(automation.clear_finished_downloads)

    @tool()
    async def retry_download(
        username: Username,
        virtual_path: VirtualPath
    ) -> dict:
        """Retries a failed or paused download."""

        return await runner.call_async(automation.retry_download, username, virtual_path)

    @tool()
    async def pause_all_downloads() -> dict:
        """Pauses everything at once: every active or queued download, and every running job, so
        no new albums start meanwhile. Partial files are kept. resume_all_downloads continues exactly
        this; jobs that were paused one by one before stay paused."""

        jobs = getattr(runner, "jobs", None)
        jobs_paused = jobs.pause_all(PauseMode.NOW) if jobs is not None else []
        downloads = await runner.call_async(automation.pause_all_downloads)
        return {**downloads, "num_jobs_paused": len(jobs_paused), "jobs_paused": jobs_paused}

    @tool()
    async def resume_all_downloads() -> dict:
        """Continues everything that pause_all_downloads stopped: every paused download, from where
        it was, and the jobs it paused."""

        downloads = await runner.call_async(automation.resume_all_downloads)
        jobs = getattr(runner, "jobs", None)
        jobs_resumed = jobs.resume_all() if jobs is not None else []
        return {**downloads, "num_jobs_resumed": len(jobs_resumed), "jobs_resumed": jobs_resumed}

    register_collection_tools(tool, runner, preferences, run_search)

    return server


def _stop_on_signal(_signal_number, _frame):
    raise KeyboardInterrupt


class MCPApplication:

    def __init__(self, isolated_mode=False, http_port=None, http_host="127.0.0.1", ui_port=DEFAULT_UI_PORT,
                 ui_host="127.0.0.1", open_ui=False, library_folders=()):

        self.isolated_mode = isolated_mode
        self.http_port = http_port
        self.http_host = http_host
        self.ui_port = ui_port
        self.ui_host = ui_host
        self.open_ui = open_ui
        self.library_folders = library_folders

    def run(self):

        if MCPServerClass is None or anyio is None:
            print("The 'mcp' Python package is required to run the MCP server. Install it with: pip install mcp",
                  file=sys.stderr)
            return 1

        runner = CoreRunner(isolated_mode=self.isolated_mode)

        try:
            runner.start()

        except Exception as error:
            print(f"Failed to start Nicotine+ core: {error}", file=sys.stderr)
            return 1

        self._start_jobs(runner)
        dashboard = self._start_dashboard(runner)
        server = build_server(runner)
        exit_code = 0

        # The default action ends the process right away, without saving the download list. That
        # includes after the HTTP server has shut down, as it raises the signal it caught again.
        signal.signal(signal.SIGTERM, _stop_on_signal)
        write_server_info(config.data_folder_path, runner.dashboard_url)

        try:
            if self.http_port:
                self._run_http(server)
            else:
                server.run(transport="stdio")

        except KeyboardInterrupt:
            pass

        except Exception as error:
            print(f"MCP server error: {error}", file=sys.stderr)
            exit_code = 1

        finally:
            remove_server_info(config.data_folder_path)

            if dashboard is not None:
                dashboard.stop()

            runner.jobs.stop()
            runner.stop()

        return exit_code

    def _start_jobs(self, runner):

        folders = runner.call(runner.automation.get_library_folders).result(15)

        runner.catalog = Catalog()
        runner.library = LibraryIndex([*folders, *self.library_folders])
        runner.sourcer = ReleaseSourcer(runner, runner.catalog)
        runner.jobs = JobManager(
            runner, runner.sourcer, library=runner.library,
            store_path=os.path.join(config.data_folder_path, JOBS_FILE_NAME)
        )
        runner.jobs.start()

    def _start_dashboard(self, runner):

        if self.ui_port is None:
            return None

        from nicotine_mcp.web import DashboardServer

        dashboard = DashboardServer(runner, host=self.ui_host, port=self.ui_port)

        try:
            dashboard.start()

        except OSError as error:
            print(f"Could not start the dashboard on {self.ui_host}:{self.ui_port}: {error}", file=sys.stderr)
            return None

        runner.dashboard_url = dashboard.url
        print(f"Dashboard: {dashboard.url}", file=sys.stderr)

        if self.open_ui:
            import webbrowser
            webbrowser.open(dashboard.url)

        return dashboard

    def _run_http(self, server):

        print(f"Serving MCP over streamable HTTP on http://{self.http_host}:{self.http_port}/mcp", file=sys.stderr)

        try:
            server.run(transport="streamable-http", host=self.http_host, port=self.http_port)

        except TypeError:
            # mcp 1.x configures host/port through settings
            server.settings.host = self.http_host
            server.settings.port = self.http_port
            server.run(transport="streamable-http")
