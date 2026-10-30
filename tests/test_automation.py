# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil

from unittest import TestCase

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.slskmessages import FileAttributes
from pynicotine.slskmessages import FileSearchResponse
from pynicotine.slskmessages import FolderContentsResponse
from pynicotine.slskmessages import ServerDisconnect
from pynicotine.slskmessages import UserStatus
from pynicotine.transfers import TransferStatus

from nicotine_mcp.automation import Automation
from nicotine_mcp.automation import DownloadState
from nicotine_mcp.automation import make_preferences

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")


def make_response(token, username, files, free_slots=True, queue=0, speed=1_000_000, addr=("127.0.0.1", 1234)):
    """Builds a FileSearchResponse like the network thread would, from
    (virtual_path, size, bitrate, length, sample_rate, bit_depth) tuples."""

    msg = FileSearchResponse(
        search_username="me", token=token, freeulslots=free_slots, ulspeed=speed, inqueue=queue, shares=[]
    )
    msg.username = username
    msg.addr = addr

    for virtual_path, size, bitrate, length, sample_rate, bit_depth in files:
        attributes = FileAttributes(
            bitrate=bitrate, length=length, sample_rate=sample_rate, bit_depth=bit_depth
        )
        msg.list.append((1, virtual_path, size, None, attributes))

    return msg


class AutomationTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        if not os.path.exists(DATA_FOLDER_PATH):
            os.makedirs(DATA_FOLDER_PATH)

        # Start every test with an empty download list
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
        config.sections["transfers"]["usernamesubfolders"] = False
        config.sections["server"]["login"] = "me"
        config.sections["server"]["passw"] = "secret"

        core.start()
        self.automation = Automation()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    def fake_login(self):
        """Pretend we are logged in, without a network connection."""

        core.users.login_status = UserStatus.ONLINE
        core.users.login_username = "me"

    def fake_logout(self):

        core.users.login_status = UserStatus.OFFLINE
        core.users.login_username = None
        events.emit("server-disconnect", ServerDisconnect())

    def start_search(self, query="artist title", mode="global", username=None):

        token = self.automation.start_search(query, mode=mode, username=username)
        self.assertIn(token, core.search.searches)
        self.assertIn(token, self.automation.searches)
        return token

    # Status #

    def test_status_offline(self):

        status = self.automation.get_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "offline")
        self.assertEqual(status["username"], "me")
        self.assertFalse(status["needs_credentials"])
        self.assertEqual(status["download_folder"], os.path.normpath(DATA_FOLDER_PATH))
        self.assertEqual(status["downloads"], {state: 0 for state in DownloadState.ALL})

    def test_search_requires_connection(self):

        with self.assertRaises(RuntimeError):
            self.automation.start_search("artist title")

        with self.assertRaises(ValueError):
            self.automation.start_search("   ")

        self.fake_login()

        with self.assertRaises(ValueError):
            self.automation.start_search("artist", mode="bogus")

        with self.assertRaises(ValueError):
            self.automation.start_search("artist", mode="user")

    # Searching and ranking #

    def test_collect_and_rank_tracks(self):

        self.fake_login()
        token = self.start_search("artist title")

        events.emit("file-search-response", make_response(token, "fastuser", [
            ("Music\\Artist\\Album\\01 - Title.flac", 30_000_000, None, 240, 44100, 16),
            ("Music\\Artist\\Album\\02 - Other.flac", 30_000_000, None, 240, 44100, 16),  # Not matching
            ("Music\\Artist\\Album\\cover.jpg", 300_000, None, None, None, None)
        ], free_slots=True, speed=5_000_000))

        events.emit("file-search-response", make_response(token, "queueduser", [
            ("Artist - Title.mp3", 9_000_000, 320, 240, None, None)
        ], free_slots=False, queue=12, speed=200_000))

        events.emit("file-search-response", make_response(token, "unknowntoken", [
            ("Artist - Title.mp3", 9_000_000, 320, 240, None, None)
        ], free_slots=True))
        events.emit("file-search-response", make_response(token + 1, "wrongtoken", [
            ("Artist - Title.mp3", 9_000_000, 320, 240, None, None)
        ], free_slots=True))

        summary = self.automation.get_search_summary(token)

        self.assertEqual(summary["query"], "artist title")
        self.assertEqual(summary["num_users_responded"], 3)
        self.assertEqual(summary["num_files"], 3)
        self.assertEqual(summary["formats"], {"mp3": 2, "flac": 1})

        results = self.automation.rank_tracks(token, make_preferences(formats=["flac", "mp3"]))
        tracks = results["tracks"]

        self.assertEqual(results["num_matching_files"], 3)
        self.assertEqual(tracks[0]["username"], "fastuser")
        self.assertEqual(tracks[0]["virtual_path"], "Music\\Artist\\Album\\01 - Title.flac")
        self.assertEqual(tracks[0]["quality"], "16 bit / 44.1 kHz")
        self.assertTrue(tracks[0]["has_free_slot"])
        self.assertEqual(tracks[0]["upload_speed_bytes"], 5_000_000)
        self.assertEqual(tracks[0]["country_code"], None)
        self.assertEqual(tracks[-1]["username"], "queueduser")
        self.assertEqual(tracks[-1]["queue_length"], 12)

        results = self.automation.rank_tracks(token, make_preferences(formats=["mp3"], require_free_slot=True))

        self.assertEqual(results["num_matching_files"], 1)
        self.assertEqual(results["tracks"][0]["username"], "unknowntoken")

        # Unknown token raises
        with self.assertRaises(KeyError):
            self.automation.rank_tracks(token + 1)

    def test_excluded_words(self):

        self.fake_login()
        token = self.start_search("artist title -live")

        events.emit("file-search-response", make_response(token, "user", [
            ("Artist\\Title (Live).mp3", 9_000_000, 320, 240, None, None),
            ("Artist\\Title.mp3", 9_000_000, 320, 240, None, None)
        ]))

        results = self.automation.rank_tracks(token)

        self.assertEqual(results["num_matching_files"], 1)
        self.assertEqual(results["tracks"][0]["basename"], "Title.mp3")

    def test_rank_albums(self):

        self.fake_login()
        token = self.start_search("artist album")

        folder_path = "Music\\Artist\\Album"
        events.emit("file-search-response", make_response(token, "fulluser", [
            (f"{folder_path}\\{i:02d} - Track.flac", 30_000_000, None, 240, 44100, 16) for i in range(1, 11)
        ] + [(f"{folder_path}\\cover.jpg", 300_000, None, None, None, None)]))

        events.emit("file-search-response", make_response(token, "partialuser", [
            (f"{folder_path}\\01 - Track.flac", 30_000_000, None, 240, 44100, 16)
        ]))

        results = self.automation.rank_albums(token)
        albums = results["albums"]

        self.assertEqual(results["num_matching_folders"], 2)
        self.assertEqual(albums[0]["username"], "fulluser")
        self.assertEqual(albums[0]["folder_path"], folder_path)
        self.assertEqual(albums[0]["num_matching_files"], 11)
        self.assertEqual(albums[0]["num_audio_files"], 10)
        self.assertEqual(albums[0]["formats"], ["flac"])
        self.assertEqual(len(albums[0]["files"]), 11)
        self.assertEqual(albums[1]["username"], "partialuser")

    def test_stop_and_prune_searches(self):

        self.fake_login()
        tokens = [self.start_search(f"query {i}") for i in range(Automation.MAX_SEARCHES + 2)]

        self.assertEqual(len(self.automation.searches), Automation.MAX_SEARCHES)
        self.assertNotIn(tokens[0], self.automation.searches)
        self.assertNotIn(tokens[0], core.search.searches)
        self.assertIn(tokens[-1], self.automation.searches)

        self.assertTrue(self.automation.stop_search(tokens[-1]))
        self.assertFalse(self.automation.searches[tokens[-1]].is_active)
        self.assertNotIn(tokens[-1], core.search.searches)
        self.assertFalse(self.automation.stop_search(tokens[0]))

        # Results for stopped searches are ignored
        events.emit("file-search-response", make_response(tokens[-1], "user", [
            ("Artist\\Title.mp3", 9_000_000, 320, 240, None, None)
        ]))
        self.assertEqual(self.automation.get_search_summary(tokens[-1])["num_files"], 0)

    # Downloads #

    def test_download_file_uses_search_metadata(self):

        self.fake_login()
        token = self.start_search("artist title")
        virtual_path = "Music\\Artist\\Title.flac"

        events.emit("file-search-response", make_response(token, "user", [
            (virtual_path, 30_000_000, None, 240, 44100, 16)
        ]))

        download = self.automation.download_file("user", virtual_path)
        transfer = core.downloads.transfers["user" + virtual_path]

        self.assertEqual(transfer.size, 30_000_000)
        self.assertEqual(transfer.file_attributes.sample_rate, 44100)
        self.assertEqual(transfer.folder_path, os.path.normpath(DATA_FOLDER_PATH))
        self.assertEqual(download["username"], "user")
        self.assertEqual(download["virtual_path"], virtual_path)
        self.assertEqual(download["basename"], "Title.flac")
        self.assertEqual(download["size_bytes"], 30_000_000)
        self.assertIn(download["state"], {DownloadState.QUEUED, DownloadState.FAILED})

        # Custom download folder
        custom_folder = os.path.join(DATA_FOLDER_PATH, "custom")
        download = self.automation.download_file("user", "Music\\Artist\\Other.flac", download_folder=custom_folder)

        self.assertEqual(download["download_folder"], custom_folder)

        with self.assertRaises(ValueError):
            self.automation.download_file("", virtual_path)

    def test_download_file_resumes_existing_entry(self):

        self.fake_login()
        virtual_path = "Music\\Artist\\Title.flac"

        self.automation.download_file("user", virtual_path, size=1000)
        transfer = core.downloads.transfers["user" + virtual_path]

        # A paused entry is resumed rather than ignored as a duplicate
        self.automation.cancel_download("user", virtual_path, remove=False)
        self.assertEqual(transfer.status, TransferStatus.PAUSED)

        download = self.automation.download_file("user", virtual_path)
        self.assertNotEqual(download["state"], DownloadState.PAUSED)
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 1)

        # Same for failed entries, e.g. the user went offline earlier
        transfer.status = TransferStatus.USER_LOGGED_OFF
        core.downloads.abort_downloads([transfer], status=TransferStatus.USER_LOGGED_OFF)
        download = self.automation.download_file("user", virtual_path)
        self.assertEqual(download["state"], DownloadState.QUEUED)

        # Queued entries are left alone
        download = self.automation.download_file("user", virtual_path)
        self.assertEqual(download["state"], DownloadState.QUEUED)
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 1)

    def test_download_requires_connection(self):

        with self.assertRaises(RuntimeError):
            self.automation.download_file("user", "Music\\file.mp3")

        with self.assertRaises(RuntimeError):
            self.automation.request_folder_download("user", "Music\\Album", lambda result: None)

    def test_list_cancel_retry_downloads(self):

        self.fake_login()

        for i in range(3):
            self.automation.download_file("user", f"Music\\Album\\{i}.flac", size=1000)

        self.automation.download_file("other", "Music\\Other\\x.flac", size=1000)

        self.assertEqual(self.automation.get_downloads()["num_downloads"], 4)
        self.assertEqual(self.automation.get_downloads(username="user")["num_downloads"], 3)
        self.assertEqual(self.automation.get_downloads(folder_path="Music\\Album")["num_downloads"], 3)
        self.assertEqual(self.automation.get_downloads(limit=2)["num_downloads"], 4)
        self.assertEqual(len(self.automation.get_downloads(limit=2)["downloads"]), 2)

        with self.assertRaises(ValueError):
            self.automation.get_downloads(state="bogus")

        selected = self.automation.select_downloads(virtual_paths=["Music\\Album\\1.flac"])
        self.assertEqual(len(selected), 1)

        # Pause, retry, then remove
        paused = self.automation.cancel_download("user", "Music\\Album\\0.flac", remove=False)
        self.assertEqual(paused["state"], DownloadState.PAUSED)
        self.assertEqual(paused["status"], TransferStatus.PAUSED)

        retried = self.automation.retry_download("user", "Music\\Album\\0.flac")
        self.assertNotEqual(retried["state"], DownloadState.PAUSED)

        removed = self.automation.cancel_download("user", "Music\\Album\\0.flac", remove=True)
        self.assertTrue(removed["removed"])
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 3)

        with self.assertRaises(KeyError):
            self.automation.cancel_download("user", "Music\\Album\\0.flac")

        with self.assertRaises(KeyError):
            self.automation.retry_download("nobody", "nothing")

        result = self.automation.cancel_folder_downloads("user", "Music\\Album")
        self.assertEqual(result["num_downloads"], 2)
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 1)

    def test_folder_download_from_contents_response(self):

        self.fake_login()
        token = self.start_search("artist album")
        folder_path = "Music\\Artist\\Album"

        events.emit("file-search-response", make_response(token, "user", [
            (f"{folder_path}\\01 - Track.flac", 30_000_000, None, 240, 44100, 16)
        ]))

        results = []
        self.automation.request_folder_download("user", folder_path, results.append, audio_only=True)

        self.assertIn("user" + folder_path, self.automation._pending_folders)

        with self.assertRaises(RuntimeError):
            self.automation.request_folder_download("user", folder_path, results.append)

        # Empty responses are ignored (the core retries with a legacy request)
        empty = FolderContentsResponse(directory=folder_path, token=1, shares=None)
        empty.username = "user"
        empty.list = {}
        events.emit("folder-contents-response", empty)
        self.assertEqual(results, [])

        msg = FolderContentsResponse(directory=folder_path, token=1, shares=None)
        msg.username = "user"
        msg.list = {
            folder_path: [
                (1, "02 - Track.flac", 31_000_000, None, FileAttributes(sample_rate=44100, bit_depth=16)),
                (1, "01 - Track.flac", 30_000_000, None, FileAttributes(sample_rate=44100, bit_depth=16)),
                (1, "cover.jpg", 300_000, None, FileAttributes())
            ],
            folder_path + "\\Scans": [
                (1, "back.jpg", 300_000, None, FileAttributes())
            ]
        }
        events.emit("folder-contents-response", msg)

        self.assertEqual(len(results), 1)
        result = results[0]

        self.assertNotIn("error", result)
        self.assertEqual(result["num_files"], 2)
        self.assertEqual(result["skipped_files"], ["cover.jpg"])
        self.assertEqual([item["basename"] for item in result["files"]], ["01 - Track.flac", "02 - Track.flac"])
        self.assertEqual(result["download_folder"], os.path.join(os.path.normpath(DATA_FOLDER_PATH), "Album"))
        self.assertNotIn("user" + folder_path, self.automation._pending_folders)

        transfer = core.downloads.transfers["user" + folder_path + "\\02 - Track.flac"]
        self.assertEqual(transfer.size, 31_000_000)
        self.assertEqual(transfer.folder_path, result["download_folder"])

        # Late responses for the same folder are ignored
        events.emit("folder-contents-response", msg)
        self.assertEqual(len(results), 1)

    def test_folder_download_in_one_format(self):

        self.fake_login()
        folder_path = "Music\\Artist\\Album"
        results = []
        self.automation.request_folder_download("user", folder_path, results.append, formats=["flac"])

        msg = FolderContentsResponse(directory=folder_path, token=1, shares=None)
        msg.username = "user"
        msg.list = {folder_path: [
            (1, "01 - Track.flac", 30_000_000, None, FileAttributes()),
            (1, "01 - Track.mp3", 9_000_000, None, FileAttributes()),
            (1, "cover.jpg", 300_000, None, FileAttributes())
        ]}
        events.emit("folder-contents-response", msg)

        self.assertEqual([item["basename"] for item in results[0]["files"]], ["01 - Track.flac", "cover.jpg"])
        self.assertEqual(results[0]["skipped_files"], ["01 - Track.mp3"])

    def test_folder_listing_downloads_nothing(self):

        self.fake_login()
        folder_path = "Music\\Artist\\Album"
        results = []

        self.automation.request_folder_listing("user", folder_path + "\\", results.append)
        self.assertIn("user" + folder_path, self.automation._pending_folders)

        msg = FolderContentsResponse(directory=folder_path, token=1, shares=None)
        msg.username = "user"
        msg.list = {folder_path: [
            (1, "01 - Track.flac", 30_000_000, None, FileAttributes(length=240)),
            (1, "cover.jpg", 300_000, None, FileAttributes())
        ]}
        events.emit("folder-contents-response", msg)

        self.assertEqual(results[0]["files"], [
            {"basename": "01 - Track.flac", "size": 30_000_000, "length": 240},
            {"basename": "cover.jpg", "size": 300_000, "length": 0}
        ])
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 0)
        self.assertEqual(self.automation._pending_folders, {})

    def test_folder_listing_failure(self):

        self.fake_login()
        results = []

        self.automation.request_folder_listing("user", "Music\\Album", results.append)
        events.emit("folder-contents-failed", "user", "Music\\Album", is_offline=True)

        self.assertEqual(results[0]["error"], "user is offline")
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 0)

    def test_discard_download_removes_partial_files(self):

        self.fake_login()
        virtual_path = "Music\\Album\\01 - Track.flac"
        self.automation.download_file("user", virtual_path, size=1000)

        incomplete_path = core.downloads.get_incomplete_download_file_path("user", virtual_path)
        folder_path, basename = os.path.split(incomplete_path)
        partial_files = [incomplete_path, os.path.join(folder_path, "._" + basename)]
        unrelated_file = os.path.join(folder_path, "INCOMPLETEunrelated.flac")

        for path in (*partial_files, unrelated_file):
            with open(path, "w", encoding="utf-8"):
                pass

        self.addCleanup(os.remove, unrelated_file)

        self.assertTrue(self.automation.discard_download("user", virtual_path))

        self.assertIsNone(self.automation.get_download("user", virtual_path))
        self.assertFalse(any(os.path.exists(path) for path in partial_files))
        self.assertTrue(os.path.exists(unrelated_file))

        self.assertFalse(self.automation.discard_download("user", virtual_path))

    def test_library_folders(self):

        config.sections["transfers"]["shared"] = [("music", "/some/music"), ("other", "/other", "extra")]
        config.sections["transfers"]["buddyshared"] = [("friends", "/friends")]

        self.assertEqual(self.automation.get_library_folders(), [
            os.path.normpath(DATA_FOLDER_PATH), "/some/music", "/other", "/friends"
        ])

    def test_folder_download_failure_and_fallback(self):

        self.fake_login()
        token = self.start_search("artist album")
        folder_path = "Music\\Artist\\Album"

        events.emit("file-search-response", make_response(token, "user", [
            (f"{folder_path}\\01 - Track.flac", 30_000_000, None, 240, 44100, 16),
            (f"{folder_path}\\03 - Track.flac", 30_000_000, None, 240, 44100, 16)
        ]))

        results = []
        self.automation.request_folder_download("user", folder_path, results.append)
        events.emit("folder-contents-failed", "user", folder_path, True)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["error"], "user is offline")
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 0)

        # Fallback to files seen in search results
        fallback = self.automation.download_folder_from_search("user", folder_path)

        self.assertTrue(fallback["partial"])
        self.assertEqual(fallback["num_files"], 2)
        self.assertEqual(self.automation.get_downloads(folder_path=folder_path)["num_downloads"], 2)

        with self.assertRaises(KeyError):
            self.automation.download_folder_from_search("user", "Music\\Unknown")

        # Disconnecting fails pending folder requests
        self.automation.request_folder_download("user", "Music\\Artist\\Other", results.append)
        self.fake_logout()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["error"], "disconnected from server")
        self.assertEqual(self.automation._pending_folders, {})

    def test_dashboard_state_and_clear_finished(self):

        self.fake_login()
        token = self.start_search("artist title")
        events.emit("file-search-response", make_response(token, "user", [
            ("Artist\\Title.mp3", 9_000_000, 320, 240, None, None)
        ]))

        self.automation.download_file("user", "Artist\\Title.mp3")
        self.automation.download_file("user", "Artist\\Other.mp3", size=1000)
        core.downloads.transfers["userArtist\\Other.mp3"].status = TransferStatus.FINISHED

        state = self.automation.get_dashboard_state()

        self.assertTrue(state["status"]["connected"])
        self.assertEqual(len(state["downloads"]), 2)
        self.assertEqual(state["downloads"][-1]["state"], DownloadState.FINISHED)  # Finished sorted last
        self.assertEqual(state["download_speed_bytes"], 0)
        self.assertEqual(state["searches"][0]["token"], token)
        self.assertEqual(state["searches"][0]["num_files"], 1)

        self.assertEqual(self.automation.clear_finished_downloads(), {"num_removed": 1})
        self.assertEqual(self.automation.get_downloads()["num_downloads"], 1)

    def test_pause_and_resume_all_downloads(self):

        self.fake_login()

        for name in ("a", "b", "c", "d"):
            self.automation.download_file("user", f"Music\\{name}.flac", size=1000)

        transfers = core.downloads.transfers
        transfers["userMusic\\c.flac"].status = TransferStatus.FINISHED
        core.downloads.abort_downloads([transfers["userMusic\\d.flac"]], status=TransferStatus.CONNECTION_CLOSED)

        # Only active and queued downloads are paused, finished and failed ones are left alone
        self.assertEqual(self.automation.pause_all_downloads(), {"num_paused": 2})

        states = {transfer.virtual_path: DownloadState.from_status(transfer.status) for transfer in transfers.values()}
        self.assertEqual(states, {
            "Music\\a.flac": DownloadState.PAUSED, "Music\\b.flac": DownloadState.PAUSED,
            "Music\\c.flac": DownloadState.FINISHED, "Music\\d.flac": DownloadState.FAILED
        })
        self.assertEqual(self.automation.pause_all_downloads(), {"num_paused": 0})

        self.assertEqual(self.automation.resume_all_downloads(), {"num_resumed": 2})

        states = {transfer.virtual_path: DownloadState.from_status(transfer.status) for transfer in transfers.values()}
        self.assertEqual(states["Music\\a.flac"], DownloadState.QUEUED)
        self.assertEqual(states["Music\\b.flac"], DownloadState.QUEUED)
        self.assertEqual(states["Music\\d.flac"], DownloadState.FAILED)
        self.assertEqual(self.automation.resume_all_downloads(), {"num_resumed": 0})

    def test_describe_transfer_states(self):

        self.fake_login()
        download = self.automation.download_file("user", "Music\\a.flac", size=1000)
        transfer = core.downloads.transfers["userMusic\\a.flac"]

        self.assertEqual(download["progress_percent"], 0)

        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = 500
        transfer.speed = 100
        transfer.time_left = 5
        download = self.automation.get_download("user", "Music\\a.flac")

        self.assertEqual(download["state"], DownloadState.ACTIVE)
        self.assertEqual(download["progress_percent"], 50.0)
        self.assertEqual(download["speed_bytes"], 100)
        self.assertEqual(download["time_left_seconds"], 5)
        self.assertIsNone(download["local_path"])

        transfer.status = TransferStatus.FINISHED
        download = self.automation.get_download("user", "Music\\a.flac")

        self.assertEqual(download["state"], DownloadState.FINISHED)
        self.assertEqual(download["progress_percent"], 100.0)
        self.assertEqual(download["local_path"], os.path.join(os.path.normpath(DATA_FOLDER_PATH), "a.flac"))

        transfer.status = TransferStatus.CONNECTION_TIMEOUT
        download = self.automation.get_download("user", "Music\\a.flac")

        self.assertEqual(download["state"], DownloadState.FAILED)
        self.assertTrue(download["will_retry"])

        transfer.status = "File not shared."
        download = self.automation.get_download("user", "Music\\a.flac")

        self.assertEqual(download["state"], DownloadState.FAILED)
        self.assertFalse(download["will_retry"])

        self.assertIsNone(self.automation.get_download("nobody", "nothing"))
