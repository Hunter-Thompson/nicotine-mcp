# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""MCP server exposing Nicotine+ search and download tools to AI agents.

Requires the ``mcp`` Python package (https://pypi.org/project/mcp/).
"""

from __future__ import annotations

import sys
import time

from inspect import cleandoc
from typing import Annotated
from typing import Literal

from pydantic import Field

from pynicotine.core import core

from nicotine_mcp import DEFAULT_UI_PORT
from nicotine_mcp.automation import Automation
from nicotine_mcp.automation import DownloadState
from nicotine_mcp.automation import make_preferences
from nicotine_mcp.ranking import DEFAULT_FORMATS
from nicotine_mcp.mcp_compat import MCPServerClass
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.runner import CoreRunner

SERVER_NAME = "nicotine-plus"
SERVER_INSTRUCTIONS = """Nicotine+ gives you access to the Soulseek peer-to-peer network to search for and
download music.

Typical workflows:

- Single track: call download_best_track(query) and you are done. It searches, ranks
  the results (free upload slot and fast uploader first, then audio quality), downloads
  the best copy and falls back to the next candidate if the transfer does not start.
- Album: call download_best_album(query). It picks the best shared folder, fetches the
  full folder listing from the uploader and downloads every file in it.
- Manual: search_tracks / search_albums return ranked candidates with scores and
  reasons. Pick one and call download_file or download_album with the username and
  virtual path. Re-run get_search_results with the same token later, more results
  usually keep arriving for a while.
- Track progress with get_downloads or block with wait_for_downloads. Finished files
  carry a local_path.

Notes: queries work best as "artist title" or "artist album" without punctuation.
Virtual paths use backslashes and belong to a specific user. Formats are file
extensions ordered by preference ("flac", "mp3", or "lossless", "lossy", "any").
Downloads land in the configured Nicotine+ download folder."""


def _described(annotation, description):
    """Shorthand for a tool parameter annotation with a description."""
    return Annotated[annotation, Field(description=description)]


Formats = _described(
    list[str] | None,
    "Allowed audio formats (file extensions) ordered by preference, e.g. ['flac', 'mp3']. "
    "Also accepts 'lossless', 'lossy' or 'any'. Defaults to ['flac', 'mp3']."
)
Prefer = _described(
    Literal["balanced", "quality", "availability"],
    "What to optimize for: 'balanced', 'quality' (lossless/high bitrate first) or "
    "'availability' (free slot and fast uploader first)."
)
MinBitrate = _described(int, "Minimum bitrate in kbps for lossy files (0 = no minimum). Lossless always passes.")
RequireFreeSlot = _described(bool, "Only consider uploaders that currently have a free upload slot.")
MaxQueue = _described(int | None, "Ignore uploaders with more than this many files queued (None = no limit).")
MinLength = _described(int, "Minimum track length in seconds when known (0 = no minimum).")
MaxLength = _described(int, "Maximum track length in seconds when known (0 = no maximum).")
WaitSeconds = _described(float, "How long to wait for search results before ranking (1-60 seconds).")
SearchMode = _described(
    Literal["global", "user", "buddies", "rooms"],
    "Where to search: the whole network (global), a single user's shares (user), buddies or joined rooms."
)
SearchUser = _described(str | None, "Username whose shares to search, required when mode is 'user'.")
DownloadFolder = _described(str | None, "Local folder to save into. Defaults to the Nicotine+ download folder.")
AudioOnly = _described(bool, "Skip non-audio files (cover art, cue/log files) when downloading a folder.")
TrackQuery = _described(str, "Search terms, e.g. 'artist title'. Prefix a word with - to exclude it.")
AlbumQuery = _described(str, "Search terms, e.g. 'artist album'. Prefix a word with - to exclude it.")
GroupBy = _described(Literal["track", "album"], "Return ranked tracks or ranked folders.")
Username = _described(str, "Uploader username from a search result.")
VirtualPath = _described(str, "Virtual path of the file from a search result (uses backslashes).")
FolderPath = _described(str, "Virtual folder path from a search result (uses backslashes).")
MaxAttempts = _described(int, "How many candidates to try before giving up.")
WaitForTrackStart = _described(int, "How long to wait for a transfer to start before trying the next candidate.")
WaitForAlbumStart = _described(
    int, "How long to wait for the first file to start transferring before trying the next folder."
)
StateFilter = _described(
    Literal["all", "queued", "active", "paused", "finished", "failed"], "Filter by download state."
)
OptionalUsername = _described(str | None, "Only downloads from this user.")
OptionalFolderPath = _described(str | None, "Only downloads inside this remote folder.")
Limit = _described(int, "Maximum number of entries to return.")
RemoveEntry = _described(bool, "Remove the entry from the download list instead of pausing it.")
MaxPerUser = _described(int, "Maximum number of results per uploader in the ranked list (0 = unlimited).")


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
        timeout_seconds: _described(int, "How long to wait for the login to complete.") = 20
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
        token: _described(int, "Search token returned by a previous search."),
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
        """Searches for a track, downloads the best copy, and falls back to the next candidate when the
        transfer does not start in time (e.g. the uploader is queued or unreachable).

        Returns the chosen track, its download entry and a log of attempts. The download continues in
        the background, use wait_for_downloads to block until it finishes.
        """

        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, min_length, max_length)
        token = await run_search(query, mode, username, wait_seconds)
        ranked = await runner.call_async(automation.rank_tracks, token, prefs, max(1, max_attempts))
        attempts = []

        if not ranked["tracks"]:
            raise ToolError(f"No results matching the filters for '{query}' "
                            f"({ranked['search']['num_files']} files from "
                            f"{ranked['search']['num_users_responded']} users seen)")

        for candidate in ranked["tracks"]:
            candidate_username = candidate["username"]
            virtual_path = candidate["virtual_path"]

            await runner.call_async(
                automation.download_file, candidate_username, virtual_path, download_folder=download_folder
            )
            download = await runner.wait_for_download_start(candidate_username, virtual_path, wait_for_start_seconds)
            attempts.append({"candidate": candidate, "download": download})

            if download["state"] in {DownloadState.ACTIVE, DownloadState.FINISHED}:
                return {"success": True, "token": token, "track": candidate, "download": download,
                        "attempts": attempts}

            try:
                await runner.call_async(automation.cancel_download, candidate_username, virtual_path, True)
            except ToolError:
                pass

        return {"success": False, "token": token, "track": None, "download": None, "attempts": attempts,
                "error": "None of the candidates started transferring in time. Try again later, raise "
                         "wait_for_start_seconds or max_attempts, or pick a result manually."}

    @tool()
    async def download_album(
        username: Username,
        folder_path: FolderPath,
        download_folder: DownloadFolder = None,
        audio_only: AudioOnly = False
    ) -> dict:
        """Downloads an entire folder (album) from a user into a subfolder named after it.

        Requests the full folder listing from the uploader first, so files that did not match the
        search are included too. Falls back to the files seen in search results if the user does
        not respond.
        """

        return await runner.request_folder_download(username, folder_path, download_folder, audio_only)

    @tool()
    async def download_best_album(
        query: AlbumQuery,
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
        """Searches for an album, downloads the best shared folder, and falls back to the next folder
        when nothing starts transferring in time.

        Returns the chosen folder, the enqueued files and a log of attempts. Downloads continue in
        the background, use wait_for_downloads with the folder_path to block until they finish.
        """

        prefs = preferences(formats, prefer, min_bitrate, require_free_slot, max_queue, 0, 0)
        token = await run_search(query, mode, username, wait_seconds)
        ranked = await runner.call_async(automation.rank_albums, token, prefs, max(1, max_attempts))
        attempts = []

        if not ranked["albums"]:
            raise ToolError(f"No folders matching the filters for '{query}' "
                            f"({ranked['search']['num_files']} files from "
                            f"{ranked['search']['num_users_responded']} users seen)")

        for folder in ranked["albums"]:
            folder_username = folder["username"]
            folder_path = folder["folder_path"]

            try:
                enqueued = await runner.request_folder_download(folder_username, folder_path, download_folder,
                                                                audio_only)
            except ToolError as error:
                attempts.append({"album": folder, "error": str(error)})
                continue

            outcome = await runner.wait_for_folder_start(folder_username, folder_path, wait_for_start_seconds)
            attempts.append({"album": folder, "enqueued": enqueued, "outcome": outcome})

            if outcome["started"]:
                return {"success": True, "token": token, "album": folder, "download": enqueued,
                        "downloads": outcome["downloads"], "attempts": attempts}

            await runner.call_async(automation.cancel_folder_downloads, folder_username, folder_path, True)

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
        virtual_paths: _described(list[str] | None, "Only wait for these virtual paths.") = None,
        folder_path: OptionalFolderPath = None,
        timeout_seconds: _described(int, "Give up waiting after this many seconds (max 600).") = 120
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

    return server


class MCPApplication:

    def __init__(self, isolated_mode=False, http_port=None, http_host="127.0.0.1", ui_port=DEFAULT_UI_PORT,
                 ui_host="127.0.0.1", open_ui=False):

        self.isolated_mode = isolated_mode
        self.http_port = http_port
        self.http_host = http_host
        self.ui_port = ui_port
        self.ui_host = ui_host
        self.open_ui = open_ui

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

        dashboard = self._start_dashboard(runner)
        server = build_server(runner)
        exit_code = 0

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
            if dashboard is not None:
                dashboard.stop()

            runner.stop()

        return exit_code

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
