# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runs the Nicotine+ core in a background thread.

The MCP server (or any other front end) owns the main thread, while the
Nicotine+ main loop runs here. Every call into the core is marshalled to
the core thread through ``events.invoke_main_thread()``.
"""

from __future__ import annotations

import os
import sys
import time

from collections import deque
from concurrent.futures import Future
from threading import Event
from threading import Thread

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import LogLevel
from pynicotine.logfacility import log

from nicotine_mcp.automation import Automation
from nicotine_mcp.automation import DownloadState
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.mcp_compat import anyio

# Components that must not run in MCP mode: "cli" reads stdin (reserved for the MCP
# stdio transport), and "signal_handler" must be installed from the main thread.
CORE_COMPONENTS = {
    "error_handler", "portmapper", "network_thread", "shares", "users", "notifications",
    "network_filter", "now_playing", "statistics", "port_checker", "update_checker", "search",
    "downloads", "uploads", "interests", "userbrowse", "userinfo", "buddies", "chatrooms",
    "privatechat", "pluginhandler"
}


class CoreRunner:
    """Runs the Nicotine+ core main loop in a background thread."""

    MIN_RESULT_WAIT = 3.0
    RESULT_SETTLE_SECONDS = 2.5
    POLL_INTERVAL = 0.5
    STARTUP_TIMEOUT = 60

    def __init__(self, isolated_mode=False):

        self.isolated_mode = isolated_mode
        self.automation = None
        self.recent_log = deque(maxlen=100)
        self.dashboard_url = None
        self.ready = Event()
        self.stopped = Event()
        self.startup_error = None
        self._thread = None

    def start(self):

        self._thread = Thread(target=self._run, name="NicotineCore", daemon=True)
        self._thread.start()

        if not self.ready.wait(self.STARTUP_TIMEOUT):
            raise RuntimeError("Timed out starting the Nicotine+ core")

        if self.startup_error is not None:
            raise self.startup_error

    def stop(self, timeout=15):

        if self._thread is None or not self._thread.is_alive():
            return

        if self.ready.is_set() and not self.stopped.is_set():
            try:
                self.call(core.quit).result(timeout)
            except Exception:
                pass

        self._thread.join(timeout)

    def _apply_environment_credentials(self):
        """Allow providing credentials without touching the config file."""

        username = os.environ.get("NICOTINE_USERNAME")
        password = os.environ.get("NICOTINE_PASSWORD")

        if username and password:
            config.sections["server"]["login"] = username
            config.sections["server"]["passw"] = password

    def _run(self):

        try:
            core.init_components(enabled_components=CORE_COMPONENTS, isolated_mode=self.isolated_mode)

            for log_level in (LogLevel.DOWNLOAD, LogLevel.UPLOAD):
                log.add_log_level(log_level, is_permanent=False)

            for event_name, callback in (
                ("log-message", self._log_message),
                ("setup", self._setup),
                ("invalid-password", self._invalid_credentials),
                ("invalid-username", self._invalid_credentials)
            ):
                events.connect(event_name, callback)

            self._apply_environment_credentials()
            core.start()
            self.automation = Automation()

        except Exception as error:
            self.startup_error = error
            self.ready.set()
            return

        self.ready.set()

        if config.sections["server"]["auto_connect_startup"] and not config.need_config():
            core.connect()

        # Main loop, process events from threads
        while events.process_thread_events():
            time.sleep(0.05)

        config.write_configuration()
        self.stopped.set()

    # Core thread helpers #

    def call(self, func, *args, **kwargs):
        """Schedules a call in the core thread and returns a Future with the result."""

        future = Future()

        def wrapper():
            try:
                future.set_result(func(*args, **kwargs))

            except BaseException as error:  # pylint: disable=broad-exception-caught
                future.set_exception(error)

        if self.stopped.is_set():
            future.set_exception(RuntimeError("Nicotine+ core has stopped"))
            return future

        events.invoke_main_thread(wrapper)
        return future

    async def call_async(self, func, *args, timeout=60, **kwargs):

        future = self.call(func, *args, **kwargs)

        try:
            return await anyio.to_thread.run_sync(future.result, timeout)

        except (KeyError, ValueError, RuntimeError) as error:
            raise ToolError(str(error)) from error

    # Waiting helpers used by tools #

    async def wait_for_search_results(self, token, wait_seconds):
        """Waits for search results to arrive, returning early once they settle."""

        wait_seconds = min(60.0, max(1.0, float(wait_seconds or 10)))
        started = time.monotonic()
        min_wait = min(wait_seconds, self.MIN_RESULT_WAIT)

        while True:
            await anyio.sleep(self.POLL_INTERVAL)

            summary = await self.call_async(self.automation.get_search_summary, token)
            elapsed = time.monotonic() - started

            if elapsed >= wait_seconds:
                return summary

            since_last = summary["seconds_since_last_result"]

            if (summary["num_files"] > 0 and elapsed >= min_wait
                    and since_last is not None and since_last >= self.RESULT_SETTLE_SECONDS):
                return summary

    async def wait_for_download_start(self, username, virtual_path, timeout):
        """Waits until a download is transferring, finished or failed."""

        timeout = max(1.0, float(timeout))
        started = time.monotonic()

        while True:
            download = await self.call_async(self.automation.get_download, username, virtual_path)

            if download is None:
                return {"state": DownloadState.FAILED, "status": "removed", "username": username,
                        "virtual_path": virtual_path}

            if download["state"] in {DownloadState.ACTIVE, DownloadState.FINISHED, DownloadState.FAILED}:
                return download

            if time.monotonic() - started >= timeout:
                download["timed_out"] = True
                return download

            await anyio.sleep(1.0)

    async def wait_for_folder_start(self, username, folder_path, timeout):
        """Waits until at least one download of a folder is transferring."""

        timeout = max(1.0, float(timeout))
        started = time.monotonic()

        while True:
            result = await self.call_async(self.automation.get_downloads, "all", username, folder_path, 1000)
            downloads = result["downloads"]
            states = {download["state"] for download in downloads}

            if not downloads:
                return {"started": False, "reason": "no downloads found", "downloads": []}

            if states & {DownloadState.ACTIVE, DownloadState.FINISHED}:
                return {"started": True, "downloads": downloads}

            if states <= {DownloadState.FAILED}:
                return {"started": False, "reason": "all downloads failed", "downloads": downloads}

            if time.monotonic() - started >= timeout:
                return {"started": False, "reason": "timed out waiting for the transfer to start",
                        "downloads": downloads}

            await anyio.sleep(1.0)

    async def request_folder_download(self, username, folder_path, download_folder=None, audio_only=False):
        """Requests folder contents and enqueues them, falling back to known
        search results if the user does not respond."""

        future = Future()

        await self.call_async(
            self.automation.request_folder_download, username, folder_path, future.set_result,
            download_folder=download_folder, audio_only=audio_only
        )

        result = await anyio.to_thread.run_sync(future.result, Automation.FOLDER_REQUEST_TIMEOUT + 10)

        if "error" not in result:
            return result

        try:
            fallback = await self.call_async(
                self.automation.download_folder_from_search, username, folder_path,
                download_folder=download_folder, audio_only=audio_only
            )
        except ToolError as error:
            raise ToolError(
                f"Could not download folder '{folder_path}' from user '{username}': {result['error']}"
            ) from error

        fallback["warning"] = (
            f"{result['error']}; downloaded only the files seen in search results, the folder may be incomplete"
        )
        return fallback

    # Core events #

    def _log_message(self, timestamp_format, msg, _title, _level):

        self.recent_log.append(msg)

        # stdout is reserved for the MCP stdio transport, log to stderr instead
        if timestamp_format:
            msg = f"[{time.strftime(timestamp_format)}] {msg}"

        try:
            print(msg, file=sys.stderr, flush=True)
        except OSError:
            pass

    @staticmethod
    def _setup():
        log.add("No Soulseek username/password configured. Set NICOTINE_USERNAME and NICOTINE_PASSWORD "
                "environment variables, or configure an account in Nicotine+ first.")

    @staticmethod
    def _invalid_credentials(*_args):
        log.add("The Soulseek server rejected the configured username or password.")
        config.sections["server"]["passw"] = ""
