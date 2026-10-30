# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil

from collections import deque
from concurrent.futures import Future
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest import TestCase

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.slskmessages import UserStatus

from nicotine_mcp.automation import Automation
from nicotine_mcp.automation import DownloadState
from nicotine_mcp.jobs import JobManager
from nicotine_mcp.matching import Release
from nicotine_mcp.web import DashboardServer
from nicotine_mcp.web import REQUEST_HEADER
from nicotine_mcp.web import REQUEST_HEADER_VALUE

from tests.test_automation import make_response

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")


class FakeRunner:
    """Runs calls synchronously (tests are single threaded apart from the HTTP server)."""

    def __init__(self, automation):

        self.automation = automation
        self.recent_log = deque(["Starting", "Logged in"], maxlen=10)
        self.dashboard_url = None
        self.jobs = None

    def call(self, func, *args, **kwargs):

        future = Future()

        try:
            future.set_result(func(*args, **kwargs))
        except BaseException as error:  # pylint: disable=broad-exception-caught
            future.set_exception(error)

        return future


class DashboardTest(TestCase):

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        if not os.path.exists(DATA_FOLDER_PATH):
            os.makedirs(DATA_FOLDER_PATH)

        for basename in ("downloads.json", "downloads.json.old"):
            try:
                os.remove(os.path.join(DATA_FOLDER_PATH, basename))
            except OSError:
                pass

        core.init_components(enabled_components={
            "pluginhandler", "search", "shares", "users", "downloads", "uploads", "network_filter", "statistics",
            "notifications", "userbrowse"
        })
        config.sections["transfers"]["downloaddir"] = DATA_FOLDER_PATH
        config.sections["transfers"]["incompletedir"] = DATA_FOLDER_PATH
        config.sections["server"]["login"] = "me"
        config.sections["server"]["passw"] = "secret"

        core.start()
        core.users.login_status = UserStatus.ONLINE
        core.users.login_username = "me"

        self.automation = Automation()
        self.runner = FakeRunner(self.automation)
        self.dashboard = DashboardServer(self.runner, host="127.0.0.1", port=0)
        self.dashboard.start()
        self.runner.dashboard_url = self.dashboard.url

    def tearDown(self):
        self.dashboard.stop()
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    def request(self, method, path, body=None, headers=None, host=None):

        connection = HTTPConnection("127.0.0.1", self.dashboard.port, timeout=10)
        request_headers = {"Host": host or f"127.0.0.1:{self.dashboard.port}"}

        if headers:
            request_headers.update(headers)

        data = None

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        connection.request(method, path, body=data, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()

        return response.status, response.getheader("Content-Type"), payload

    def post(self, path, body, authorized=True, **kwargs):

        headers = {REQUEST_HEADER: REQUEST_HEADER_VALUE} if authorized else {}
        return self.request("POST", path, body=body, headers=headers, **kwargs)

    def test_index_and_state(self):

        self.assertTrue(self.dashboard.url.startswith("http://127.0.0.1:"))

        status, content_type, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Nicotine+ MCP", payload)

        status, content_type, payload = self.request("GET", "/api/state")
        state = json.loads(payload)

        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertTrue(state["status"]["connected"])
        self.assertEqual(state["downloads"], [])
        self.assertEqual(state["searches"], [])
        self.assertEqual(state["recent_log"], ["Starting", "Logged in"])
        self.assertEqual(state["dashboard_url"], self.dashboard.url)

        status, _content_type, _payload = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_local_only(self):

        status, _content_type, payload = self.request("GET", "/api/state", host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertIn("local", json.loads(payload)["error"])

        status, _content_type, _payload = self.request("GET", "/api/state", host="localhost:1234")
        self.assertEqual(status, 200)

        # Cross-origin pages are rejected, same-origin pages are fine
        status, _content_type, _payload = self.request(
            "GET", "/api/state", headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)

        status, _content_type, _payload = self.request(
            "GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{self.dashboard.port}"})
        self.assertEqual(status, 200)

        status, _content_type, _payload = self.request("OPTIONS", "/api/downloads/cancel")
        self.assertEqual(status, 403)

    def test_download_actions(self):

        self.automation.download_file("user", "Music\\a.flac", size=1000)
        self.automation.download_file("user", "Music\\b.flac", size=1000)

        # Missing CSRF header
        status, _content_type, _payload = self.post(
            "/api/downloads/cancel", {"username": "user", "virtual_path": "Music\\a.flac"}, authorized=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 2)

        status, _content_type, payload = self.post(
            "/api/downloads/cancel", {"username": "user", "virtual_path": "Music\\a.flac", "remove": False})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["state"], DownloadState.PAUSED)

        status, _content_type, payload = self.post(
            "/api/downloads/retry", {"username": "user", "virtual_path": "Music\\a.flac"})
        self.assertEqual(status, 200)
        self.assertNotEqual(json.loads(payload)["state"], DownloadState.PAUSED)

        status, _content_type, payload = self.post(
            "/api/downloads/cancel", {"username": "user", "virtual_path": "Music\\a.flac"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["removed"])
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 1)

        # Unknown download and malformed bodies are client errors
        status, _content_type, payload = self.post(
            "/api/downloads/cancel", {"username": "user", "virtual_path": "Music\\a.flac"})
        self.assertEqual(status, 400)
        self.assertIn("No download", json.loads(payload)["error"])

        status, _content_type, _payload = self.post("/api/downloads/retry", {"username": "user"})
        self.assertEqual(status, 400)

        status, _content_type, _payload = self.post("/api/downloads/unknown", {})
        self.assertEqual(status, 404)

        status, _content_type, payload = self.post("/api/downloads/clear-finished", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"num_removed": 0})

    def test_search_results_and_stop(self):

        token = self.automation.start_search("artist title")
        events.emit("file-search-response", make_response(token, "user", [
            ("Music\\Artist\\Album\\Title.flac", 30_000_000, None, 240, 44100, 16),
            ("Music\\Artist\\Album\\Title.mp3", 9_000_000, 320, 240, None, None)
        ]))

        status, _content_type, payload = self.request("GET", f"/api/search/{token}?group=track&limit=5")
        data = json.loads(payload)

        self.assertEqual(status, 200)
        self.assertEqual(data["num_matching_files"], 2)
        self.assertEqual(data["tracks"][0]["format"], "flac")

        status, _content_type, payload = self.request("GET", f"/api/search/{token}?group=track&formats=mp3")
        self.assertEqual(json.loads(payload)["num_matching_files"], 1)

        status, _content_type, payload = self.request("GET", f"/api/search/{token}?group=album")
        self.assertEqual(json.loads(payload)["num_matching_folders"], 1)

        status, _content_type, payload = self.request("GET", "/api/search/999")
        self.assertEqual(status, 400)

        status, _content_type, payload = self.request("GET", "/api/state")
        self.assertEqual(json.loads(payload)["searches"][0]["token"], token)

        status, _content_type, payload = self.post("/api/searches/stop", {"token": token})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["stopped"])
        self.assertFalse(self.automation.searches[token].is_active)

    def test_jobs(self):

        status, _content_type, payload = self.request("GET", "/api/state")
        self.assertEqual(json.loads(payload)["jobs"], [])

        status, _content_type, payload = self.request("GET", "/api/jobs/nope")
        self.assertEqual(status, 400)

        catalog = SimpleNamespace(is_on_label=lambda *_args: True)
        self.runner.jobs = JobManager(self.runner, SimpleNamespace(catalog=catalog))
        job_id = self.runner.jobs.create_job(
            "BITE", [Release("musicbrainz:1", "musicbrainz", "Barker", "Utility")], mode="preview"
        )["job_id"]

        status, _content_type, payload = self.request("GET", "/api/state")
        job = json.loads(payload)["jobs"][0]
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["shelf"][0]["title"], "Utility")
        self.assertEqual(job["shelf"][0]["state"], "pending")

        status, _content_type, payload = self.request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["releases"][0]["title"], "Utility")

        status, _content_type, payload = self.post("/api/jobs/pause", {"job_id": job_id}, authorized=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.runner.jobs.describe_job(job_id)["status"], "running")

        for path, expected_status in (("pause", "paused"), ("resume", "running")):
            status, _content_type, payload = self.post(f"/api/jobs/{path}", {"job_id": job_id})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)["status"], expected_status)

        status, _content_type, payload = self.post("/api/jobs/start-downloads", {"job_id": job_id})
        self.assertEqual(json.loads(payload)["mode"], "download")

        status, _content_type, payload = self.post("/api/jobs/retry-missing", {"job_id": job_id})
        self.assertEqual(json.loads(payload)["num_retried"], 0)

        status, _content_type, payload = self.post("/api/jobs/cancel", {"job_id": job_id})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "cancelled")

        status, _content_type, payload = self.post("/api/jobs/remove", {"job_id": job_id})
        self.assertTrue(json.loads(payload)["removed"])

        status, _content_type, payload = self.post("/api/jobs/resume", {"job_id": "nope"})
        self.assertEqual(status, 400)

        status, _content_type, payload = self.post("/api/jobs/nonsense", {"job_id": "nope"})
        self.assertEqual(status, 400)
