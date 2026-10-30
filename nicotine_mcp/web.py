# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local web dashboard for the human in the loop.

Serves a single page that shows connection status, downloads (with
progress, speed and queue position), collection jobs, recent searches with
their ranked results, and recent log messages. Downloads can be paused,
retried or removed from the page, and jobs can be controlled.

The dashboard binds to localhost only. Requests must carry a local
``Host`` header (protection against DNS rebinding), and state changing
requests must carry the ``X-Requested-With: nicotine-mcp`` header, which
browsers only send after a CORS preflight that this server never grants
(protection against cross-site request forgery).

Only the Python standard library is used.
"""

from __future__ import annotations

import json

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from importlib import resources
from threading import Thread
from urllib.parse import parse_qs
from urllib.parse import urlparse

from nicotine_mcp.automation import make_preferences
from nicotine_mcp.job_models import PauseMode
from nicotine_mcp.mcp_compat import anyio
from nicotine_mcp.portable import export_running

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
REQUEST_HEADER = "X-Requested-With"
REQUEST_HEADER_VALUE = "nicotine-mcp"
MAX_BODY_SIZE = 64 * 1024
CALL_TIMEOUT = 15


class DashboardServer:
    """Serves the dashboard in a background thread."""

    def __init__(self, runner, host="127.0.0.1", port=8585):

        self.runner = runner
        self.host = host
        self.port = port
        self._server = None
        self._thread = None

    @property
    def url(self):

        host = self.host

        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"

        return f"http://{host}:{self.port}/"

    def start(self):

        handler_class = type("BoundDashboardHandler", (DashboardHandler,), {"runner": self.runner})

        self._server = ThreadingHTTPServer((self.host, self.port), handler_class)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

        self._thread = Thread(target=self._server.serve_forever, name="DashboardServer", daemon=True)
        self._thread.start()

    def stop(self):

        if self._server is None:
            return

        self._server.shutdown()
        self._server.server_close()
        self._server = None


def load_index_html():
    return resources.files("nicotine_mcp").joinpath("static/index.html").read_bytes()


class DashboardHandler(BaseHTTPRequestHandler):

    runner = None  # Set on a subclass by DashboardServer
    server_version = "nicotine-mcp"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        # Keep stderr free of access logs
        pass

    # Helpers #

    def _call(self, func, *args, **kwargs):
        return self.runner.call(func, *args, **kwargs).result(CALL_TIMEOUT)

    def _send(self, status, body, content_type):

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status, message):
        self._send_json({"error": message}, status=status)

    def _is_local_request(self):
        """Rejects requests whose Host or Origin header is not local."""

        host = (self.headers.get("Host") or "").strip().lower()
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"

        if hostname not in LOCAL_HOSTNAMES:
            return False

        origin = self.headers.get("Origin")

        if origin:
            origin_host = (urlparse(origin).hostname or "").lower()

            if origin_host not in LOCAL_HOSTNAMES and f"[{origin_host}]" not in LOCAL_HOSTNAMES:
                return False

        return True

    def _read_json_body(self):

        length = int(self.headers.get("Content-Length") or 0)

        if length <= 0:
            return {}

        if length > MAX_BODY_SIZE:
            raise ValueError("Request body too large")

        data = json.loads(self.rfile.read(length).decode("utf-8"))

        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")

        return data

    # Routes #

    def do_GET(self):  # pylint: disable=invalid-name

        if not self._is_local_request():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Only local requests are allowed")
            return

        url = urlparse(self.path)
        params = parse_qs(url.query)

        try:
            if url.path in {"/", "/index.html"}:
                self._send(HTTPStatus.OK, load_index_html(), "text/html; charset=utf-8")

            elif url.path == "/api/state":
                self._send_json(self._get_state())

            elif url.path.startswith("/api/search/"):
                self._send_json(self._get_search_results(url.path.rpartition("/")[-1], params))

            elif url.path.startswith("/api/jobs/"):
                self._send_json(self._get_job(url.path.rpartition("/")[-1], params))

            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")

        except (KeyError, ValueError) as error:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))

        except Exception as error:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self):  # pylint: disable=invalid-name

        if not self._is_local_request():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Only local requests are allowed")
            return

        if self.headers.get(REQUEST_HEADER) != REQUEST_HEADER_VALUE:
            self._send_error_json(HTTPStatus.FORBIDDEN, f"Missing {REQUEST_HEADER} header")
            return

        url = urlparse(self.path)
        automation = self.runner.automation

        try:
            data = self._read_json_body()

            if url.path.startswith("/api/downloads/"):
                result = self._post_download_action(url.path.rpartition("/")[-1], data)

            elif url.path == "/api/searches/stop":
                result = {"stopped": self._call(automation.stop_search, int(data["token"]))}

            elif url.path == "/api/export":
                result = export_running(self.runner, data.get("path"))

            elif url.path.startswith("/api/jobs/"):
                result = self._post_job_action(url.path.rpartition("/")[-1], data)

            else:
                result = None

            if result is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                return

            self._send_json(result)

        except (KeyError, ValueError, RuntimeError, TypeError) as error:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))

        except Exception as error:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def _post_download_action(self, action, data):

        automation = self.runner.automation
        jobs = self.runner.jobs

        if action == "cancel":
            return self._call(
                automation.cancel_download, data["username"], data["virtual_path"], bool(data.get("remove", True))
            )

        if action == "retry":
            return self._call(automation.retry_download, data["username"], data["virtual_path"])

        if action == "clear-finished":
            return self._call(automation.clear_finished_downloads)

        if action == "pause-all":
            # Jobs first, so none of them starts another album while the downloads are being paused
            jobs_paused = jobs.pause_all(PauseMode.NOW) if jobs is not None else []
            downloads = self._call(automation.pause_all_downloads)
            return {**downloads, "num_jobs_paused": len(jobs_paused), "jobs_paused": jobs_paused}

        if action == "resume-all":
            downloads = self._call(automation.resume_all_downloads)
            jobs_resumed = jobs.resume_all() if jobs is not None else []
            return {**downloads, "num_jobs_resumed": len(jobs_resumed), "jobs_resumed": jobs_resumed}

        return None

    def _post_job_action(self, action, data):

        jobs = self.runner.jobs
        job_id = data["job_id"]

        if action == "pause":
            return jobs.pause_job(job_id, PauseMode.NOW if data.get("now") else PauseMode.AFTER_ALBUM)

        if action == "skip-release":
            return anyio.run(jobs.skip_release, job_id, data["release_id"])

        if action == "prioritize-release":
            return jobs.prioritize_release(job_id, data["release_id"])

        if action == "resume":
            return jobs.resume_job(job_id)

        if action == "start-downloads":
            return jobs.start_downloads(
                job_id, bool(data.get("accept_partial", False)), bool(data.get("flac_only", False))
            )

        if action == "retry-missing":
            return jobs.retry_missing(job_id)

        if action == "cancel":
            return anyio.run(jobs.cancel_job, job_id)

        if action == "remove":
            return jobs.remove_job(job_id)

        raise KeyError(f"Unknown job action '{action}'")

    def do_OPTIONS(self):  # pylint: disable=invalid-name
        # No CORS: cross-origin pages never get to talk to the dashboard
        self._send_error_json(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")

    # Data #

    def _get_state(self):

        state = self._call(self.runner.automation.get_dashboard_state)
        state["recent_log"] = list(self.runner.recent_log)[-40:]
        state["dashboard_url"] = self.runner.dashboard_url
        jobs = getattr(self.runner, "jobs", None)
        state["jobs"] = jobs.list_jobs(include_shelf=True) if jobs is not None else []
        return state

    def _get_job(self, job_id, params):

        if getattr(self.runner, "jobs", None) is None:
            raise KeyError("Jobs are not available")

        state = params.get("state", [None])[0]
        limit = int(params.get("limit", ["500"])[0])
        return self.runner.jobs.get_job(job_id, state=state, limit=limit)

    def _get_search_results(self, token, params):

        group = params.get("group", ["track"])[0]
        limit = int(params.get("limit", ["15"])[0])
        formats = params.get("formats", ["any"])[0]
        prefs = make_preferences(formats=formats.split(","), max_per_user=0)
        automation = self.runner.automation

        if group == "album":
            return self._call(automation.rank_albums, int(token), prefs, limit)

        return self._call(automation.rank_tracks, int(token), prefs, limit)
