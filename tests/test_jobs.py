# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import tempfile

from collections import deque
from unittest import TestCase

import anyio

from nicotine_mcp.catalog import Catalog
from nicotine_mcp.catalog import CatalogError
from nicotine_mcp.jobs import ItemState
from nicotine_mcp.jobs import JobManager
from nicotine_mcp.jobs import JobStatus
from nicotine_mcp.jobs import is_destination_available
from nicotine_mcp.job_models import release_folder_name
from nicotine_mcp.library import LibraryIndex
from nicotine_mcp.matching import Release
from nicotine_mcp.matching import Track
from nicotine_mcp.matching import Verdict
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.ranking import is_file_wanted
from nicotine_mcp.sourcing import ReleaseSourcer


def make_release(number, artist="Barker", catalog_numbers=()):

    return Release(f"musicbrainz:{number}", "musicbrainz", artist, f"Album {number}",
                   catalog_numbers=list(catalog_numbers), track_count=2)


def tracks_of(release):
    return [Track(f"{release.title} Song One"), Track(f"{release.title} Song Two")]


def album_files(release, extension="flac", length=0):
    return [(f"0{index + 1} {track.title}.{extension}", length) for index, track in enumerate(tracks_of(release))]


class FakeCatalog:

    is_on_label = staticmethod(Catalog.is_on_label)

    def __init__(self):

        self.num_lookups = 0
        self.fail_with = None
        self.label_ids = []

    def release_details(self, release_id):

        self.num_lookups += 1

        if self.fail_with:
            raise self.fail_with

        release = make_release(release_id.partition(":")[2])
        release.tracks = tracks_of(release)
        release.label_ids = self.label_ids
        return release


class FakeAutomation:
    """Plays the Soulseek network: `network` maps a search query to folders shared by users."""

    def __init__(self):

        self.connected = True
        self.network = {}
        self.searches = {}
        self.downloads = {}
        self.queries = []
        self.default_folder = "/downloads"
        self.probes = {}
        self.discarded = []
        self.refused_probes = set()
        self.uploader_speeds = {}  # Bytes per poll of a race
        self.never_active = set()
        self.measured = {}
        self.enqueued_files = []

    def is_connected(self):
        return self.connected

    def get_status(self):
        return {"download_folder": self.default_folder}

    def start_search(self, query, _mode="global", _username=None):

        if not self.connected:
            raise RuntimeError("Not connected")

        self.queries.append(query)
        token = len(self.queries)
        self.searches[token] = query
        return token

    def stop_search(self, _token):
        return True

    def rank_albums(self, token, _prefs, _limit):

        albums = []

        for username, folder_path, files, score in self.network.get(self.searches[token], []):
            albums.append({
                "username": username, "folder_path": folder_path, "folder_name": folder_path.rpartition("\\")[-1],
                "formats": ["flac"], "total_size": "1 MB", "has_free_slot": True, "queue_length": 0,
                "upload_speed": "1 MB/s", "score": score,
                "files": [{"basename": basename, "length_seconds": length} for basename, length in files]
            })

        return {"search": {"num_users_responded": len(albums)}, "num_matching_folders": len(albums), "albums": albums}

    def enqueue_probe(self, username, folder_path, basename, size=0, _download_folder=None):

        if username in self.refused_probes:
            raise RuntimeError("user is offline")

        virtual_path = f"{folder_path}\\{basename}"
        self.probes[(username, virtual_path)] = {
            "state": "queued" if username in self.never_active else "active",
            "bytes_transferred": 0, "size_bytes": size
        }

    def get_download(self, username, virtual_path):

        probe = self.probes.get((username, virtual_path))

        if probe is not None and probe["state"] == "active":
            probe["bytes_transferred"] += self.uploader_speeds.get(username, 1000)

        return dict(probe) if probe is not None else None

    def record_speed(self, username, bytes_per_second, _seconds):
        self.measured[username] = bytes_per_second

    def record_speed_failure(self, username):
        self.measured[username] = -1

    def get_uploader_speed(self, username):
        return self.measured.get(username, 0)

    def enqueue_files(self, username, folder_path, files, destination):

        self.enqueued_files.append((username, folder_path, [item["basename"] for item in files], destination))
        self.downloads[(username, folder_path)] = [
            {"virtual_path": f"{folder_path}\\{item['basename']}", "state": "active", "bytes_transferred": 0,
             "size_bytes": item["size"], "will_retry": False}
            for item in files
        ]

    def discard_download(self, username, virtual_path):

        self.discarded.append((username, virtual_path))
        self.probes.pop((username, virtual_path), None)
        key = (username, virtual_path.rpartition("\\")[0])

        if key in self.downloads:
            self.downloads[key] = [item for item in self.downloads[key] if item["virtual_path"] != virtual_path]

    def get_downloads(self, _state, username, folder_path, _limit):
        return {"downloads": list(self.downloads.get((username, folder_path), []))}

    def cancel_folder_downloads(self, username, folder_path, remove=True):

        if remove:
            self.downloads.pop((username, folder_path), None)
            return

        for download in self.downloads.get((username, folder_path), []):
            if download["state"] != "finished":
                download["state"] = "paused"

    def retry_download(self, username, virtual_path):

        for download in self.downloads[username, virtual_path.rpartition("\\")[0]]:
            if download["virtual_path"] == virtual_path:
                download["state"] = "active"

    def set_transfer_state(self, username, folder_path, state, bytes_transferred=None, will_retry=False):

        for download in self.downloads[(username, folder_path)]:
            download["state"] = state
            download["will_retry"] = will_retry

            if bytes_transferred is not None:
                download["bytes_transferred"] = bytes_transferred


class FakeRunner:

    def __init__(self):

        self.automation = FakeAutomation()
        self.recent_log = deque(maxlen=10)
        self.listings = {}
        self.unresponsive = set()
        self.refuse_download = set()
        self.started_downloads = []

    async def call_async(self, func, *args, **kwargs):

        try:
            return func(*args, **kwargs)

        except (KeyError, ValueError, RuntimeError) as error:
            raise ToolError(str(error)) from error

    async def wait_for_search_results(self, _token, _wait_seconds):
        return {}

    async def request_folder_listing(self, username, folder_path):

        if username in self.unresponsive:
            return {"username": username, "folder_path": folder_path, "error": "timed out"}

        for _query, folders in self.automation.network.items():
            for folder in folders:
                if folder[0] == username and folder[1] == folder_path:
                    files = self.listings.get((username, folder_path), folder[2])
                    return {"username": username, "folder_path": folder_path, "files": [
                        {"basename": basename, "size": 1000, "length": length} for basename, length in files
                    ]}

        return {"username": username, "folder_path": folder_path, "error": "no such folder"}

    async def request_folder_download(self, username, folder_path, download_folder=None, _audio_only=False,
                                      formats=None):

        if username in self.refuse_download:
            raise ToolError("user is offline")

        for key in [key for key in self.automation.probes if key[0] == username]:
            del self.automation.probes[key]

        self.started_downloads.append((username, folder_path, download_folder))
        listing = await self.request_folder_listing(username, folder_path)
        files = [item for item in listing["files"] if is_file_wanted(item["basename"], formats)]
        self.automation.downloads[(username, folder_path)] = [
            {"virtual_path": f"{folder_path}\\{item['basename']}", "state": "queued", "bytes_transferred": 0,
             "size_bytes": item["size"], "will_retry": False}
            for item in files
        ]
        return {"download_folder": f"{download_folder}/{folder_path.rpartition(chr(92))[-1]}",
                "num_files": len(files)}


class JobTestCase(TestCase):

    def setUp(self):

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.store_path = os.path.join(self.folder, "jobs.json")
        self.now = 1000.0
        self.runner = FakeRunner()
        self.automation = self.runner.automation
        self.catalog = FakeCatalog()
        self.destination_ok = True
        self.manager = self.make_manager()

    def make_manager(self, library=None):

        sourcer = ReleaseSourcer(self.runner, self.catalog)
        sourcer.race_start_timeout = 0
        sourcer.race_seconds = 0
        sourcer.race_poll_seconds = 0
        sourcer.race_min_measure_seconds = 0
        sourcer.race_settle_seconds = 0
        return JobManager(self.runner, sourcer, library=library, store_path=self.store_path, clock=lambda: self.now,
                          is_destination_ok=lambda _path: self.destination_ok)

    def share(self, query, username, folder_path, release, score=90, files=None):
        self.automation.network.setdefault(query, []).append(
            (username, folder_path, files if files is not None else album_files(release), score)
        )

    def share_release(self, number, username="alice", score=90):

        release = make_release(number)
        self.share(f"Barker Album {number}", username, f"Music\\Barker\\Album {number}", release, score)
        return release

    def create(self, releases, mode="download", **options):

        options.setdefault("search_interval", 0)
        return self.manager.create_job("BITE", releases, label_id="musicbrainz:bite", mode=mode, **options)["job_id"]

    def steps(self, count=1):

        for _ in range(count):
            anyio.run(self.manager.step)

    def item_states(self, job_id):
        return [item.state for item in self.manager.jobs[job_id].items]


class PreviewTest(JobTestCase):

    def test_preview_never_downloads(self):

        found = self.share_release(1)
        job_id = self.create([found, make_release(2)], mode="preview")

        self.steps(3)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED, ItemState.NOT_FOUND])
        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.PREVIEWED)
        self.assertEqual(self.runner.started_downloads, [])

    def test_describe_job_counts(self):

        job_id = self.create([self.share_release(1), make_release(2)], mode="preview")
        self.steps(3)

        job = self.manager.get_job(job_id)

        self.assertEqual(job["num_available"], 1)
        self.assertEqual(job["counts"], {ItemState.SOURCED: 1, ItemState.NOT_FOUND: 1})
        self.assertEqual(job["releases"][0]["uploader"], "alice")
        self.assertEqual(self.manager.get_job(job_id, state=ItemState.NOT_FOUND)["num_listed"], 1)
        self.assertEqual(self.manager.get_job(job_id, state=ItemState.NOT_FOUND)["releases"][0]["title"], "Album 2")

    def test_wrong_album_is_not_accepted(self):
        """The search finds a folder with the right name, but its tracks are from something else."""

        release = make_release(1)
        self.share("Barker Album 1", "alice", "Music\\Barker\\Album 1", release, files=[
            ("01 Totally Different.flac", 0), ("02 Nothing Alike.flac", 0)
        ])
        job_id = self.create([release], mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.NOT_FOUND])

    def test_full_listing_is_used_over_search_results(self):
        """Searches only return matching files, the listing shows the whole folder."""

        release = make_release(1)
        self.share("Barker Album 1", "alice", "Music\\Barker\\Album 1", release, files=album_files(release)[:1])
        self.runner.listings["alice", "Music\\Barker\\Album 1"] = album_files(release)
        job_id = self.create([release], mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])
        self.assertTrue(self.manager.jobs[job_id].items[0].sources[0]["listing_verified"])

    def test_partial_copy(self):

        release = make_release(1)
        self.share("Barker Album 1", "alice", "Music\\Barker\\Album 1", release, files=album_files(release)[:1])
        job_id = self.create([release], mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.PARTIAL])
        details = self.manager.get_job(job_id)["releases"][0]["best_partial_copy"]
        self.assertEqual((details["num_matched"], details["num_expected"]), (1, 2))

    def test_uses_search_results_when_peer_does_not_answer(self):

        release = self.share_release(1)
        self.runner.unresponsive.add("alice")
        job_id = self.create([release], mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])
        self.assertFalse(self.manager.jobs[job_id].items[0].sources[0]["listing_verified"])

    def test_best_available_copy_comes_first(self):

        release = self.share_release(1, "slow", score=50)
        self.share_release(1, "fast", score=95)
        job_id = self.create([release], mode="preview")

        self.steps(1)

        sources = self.manager.jobs[job_id].items[0].sources
        self.assertEqual([source["username"] for source in sources], ["fast", "slow"])

    def test_catalog_number_query_finds_it(self):

        release = make_release(1, catalog_numbers=["BITE028"])
        self.share("BITE028", "alice", "Music\\[BITE028]", release)
        job_id = self.create([release], mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])
        self.assertEqual(self.automation.queries, ["Barker Album 1", "BITE028"])

    def test_tracklist_failure_still_searches(self):

        self.catalog.fail_with = CatalogError("musicbrainz.org returned HTTP 503")
        job_id = self.create([self.share_release(1)], mode="preview")

        self.steps(1)

        # Without a tracklist the folder name decides
        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])
        self.assertEqual(self.manager.jobs[job_id].items[0].sources[0]["match"]["verdict"], Verdict.PROBABLE)

    def test_not_on_label(self):

        self.catalog.label_ids = ["309"]
        job_id = self.manager.create_job("Ostgut Ton", [self.share_release(1)], label_id="discogs:178535",
                                         mode="preview", search_interval=0)["job_id"]
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.NOT_ON_LABEL])
        self.assertEqual(self.automation.queries, [])

    def test_owned_release_is_not_searched(self):

        album_folder = os.path.join(self.folder, "library", "Barker", "Album 1")
        os.makedirs(album_folder)
        release = self.share_release(1)

        for basename, _length in album_files(release):
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        self.manager = self.make_manager(library=LibraryIndex([os.path.join(self.folder, "library")]))
        job_id = self.create([release])

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.OWNED])
        self.assertEqual(self.manager.jobs[job_id].items[0].local_path, album_folder)
        self.assertEqual(self.automation.queries, [])

    def test_skip_owned_can_be_turned_off(self):

        album_folder = os.path.join(self.folder, "library", "Barker", "Album 1")
        os.makedirs(album_folder)
        release = self.share_release(1)

        for basename, _length in album_files(release):
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        self.manager = self.make_manager(library=LibraryIndex([os.path.join(self.folder, "library")]))
        job_id = self.create([release], skip_owned=False, mode="preview")

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])


class DownloadTest(JobTestCase):

    def finish(self, username, folder_path):
        self.automation.set_transfer_state(username, folder_path, "finished", bytes_transferred=1000)

    def test_full_cycle(self):

        job_id = self.create([self.share_release(1)])

        self.steps(1)
        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])

        self.steps(1)
        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])
        self.assertEqual(self.runner.started_downloads,
                         [("alice", "Music\\Barker\\Album 1", os.path.join("/downloads", "BITE"))])

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "active", bytes_transferred=500)
        self.steps(1)
        item = self.manager.jobs[job_id].items[0]
        self.assertEqual(item.progress_percent, 50.0)

        self.finish("alice", "Music\\Barker\\Album 1")
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.FINISHED])
        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.FINISHED)
        self.assertEqual(item.num_files_finished, 2)
        self.assertEqual(item.local_path, "/downloads/BITE/Album 1")

    def test_custom_download_folder(self):

        self.create([self.share_release(1)], download_folder="/elsewhere")
        self.steps(2)

        self.assertEqual(self.runner.started_downloads[0][2], "/elsewhere")

    def test_label_name_cannot_escape_the_download_folder(self):

        job_id = self.manager.create_job("../../etc/Bad:Name", [self.share_release(1)], mode="download",
                                         search_interval=0)["job_id"]
        self.steps(2)

        self.assertEqual(self.manager.jobs[job_id].download_folder, os.path.join("/downloads", "_.._etc_Bad_Name"))

    def test_limits_releases_at_a_time(self):

        job_id = self.create([self.share_release(number, f"user{number}") for number in range(1, 6)],
                             max_active_releases=2, max_per_uploader=5)
        self.steps(8)

        self.assertEqual(self.item_states(job_id).count(ItemState.DOWNLOADING), 2)

    def test_limits_releases_per_uploader(self):

        job_id = self.create([self.share_release(1), self.share_release(2)], max_active_releases=5, max_per_uploader=1)
        self.steps(4)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING, ItemState.SOURCED])

        self.finish("alice", "Music\\Barker\\Album 1")
        self.steps(2)

        self.assertEqual(self.item_states(job_id), [ItemState.FINISHED, ItemState.DOWNLOADING])

    def test_does_not_search_far_ahead(self):

        self.create([self.share_release(number, f"user{number}") for number in range(1, 20)], max_active_releases=1)

        # The first one starts, and what is searched stops at the backlog limit
        self.steps(10)

        self.assertLessEqual(len(self.automation.queries), 3)

    def test_falls_back_to_next_uploader_when_refused(self):

        release = self.share_release(1, "gone", score=95)
        self.share_release(1, "here", score=50)
        self.automation.refused_probes.add("gone")
        job_id = self.create([release])

        self.steps(2)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])
        self.assertEqual(self.manager.jobs[job_id].items[0].current_source["username"], "here")

    def enter_queue(self, job_id):

        self.steps(2)
        return self.manager.jobs[job_id].items[0]

    def test_queued_uploader_gets_the_release_instead_of_failing(self):

        self.automation.never_active.add("alice")
        job_id = self.create([self.share_release(1)])

        item = self.enter_queue(job_id)

        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertTrue(item.is_queued)
        self.assertEqual(item.current_source["username"], "alice")
        self.assertIn("queue", item.note)

    def test_free_uploader_is_preferred_over_a_queue(self):

        release = self.share_release(1, "busy", score=95)
        self.share_release(1, "free", score=50)
        self.automation.never_active.add("busy")
        job_id = self.create([release])

        self.steps(2)
        item = self.manager.jobs[job_id].items[0]

        self.assertEqual(item.current_source["username"], "free")
        self.assertFalse(item.is_queued)

    def test_queue_does_not_use_up_active_releases(self):

        self.automation.never_active.add("alice")
        queued = self.share_release(1, "alice")
        free = self.share_release(2, "bob")
        job_id = self.create([queued, free], max_active_releases=1)

        self.steps(4)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING, ItemState.DOWNLOADING])

    def test_queue_outlasts_the_stall_timeout_and_then_moves_on(self):

        self.automation.never_active.add("alice")
        job_id = self.create([self.share_release(1)], start_timeout=900)
        item = self.enter_queue(job_id)
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "queued", bytes_transferred=0)

        self.now += 901
        self.steps(1)
        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertTrue(item.is_queued)

        self.now += self.manager.QUEUE_TIMEOUT
        self.steps(1)
        self.assertFalse(item.is_queued)
        self.assertNotEqual(item.state, ItemState.DOWNLOADING)

    def test_leaving_the_queue_starts_the_normal_checks(self):

        self.automation.never_active.add("alice")
        job_id = self.create([self.share_release(1)], start_timeout=900)
        item = self.enter_queue(job_id)
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "queued", bytes_transferred=0)

        self.now += 3000
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "active", bytes_transferred=10)
        self.steps(1)

        self.assertFalse(item.is_queued)
        self.assertIsNone(item.note)
        self.assertEqual(item.last_progress_at, self.now)

    def test_queue_survives_a_restart(self):

        self.automation.never_active.add("alice")
        job_id = self.create([self.share_release(1)])
        self.enter_queue(job_id)
        self.manager._save()

        self.assertTrue(self.make_manager().jobs[job_id].items[0].is_queued)

    def test_releases_waiting_for_a_busy_uploader_do_not_stop_the_search(self):

        self.automation.never_active.add("alice")
        releases = [self.share_release(number) for number in range(1, 5)]
        other = self.share_release(5, "bob")
        job_id = self.create([*releases, other], max_active_releases=1)

        self.steps(12)
        states = self.item_states(job_id)

        self.assertEqual(states[0], ItemState.DOWNLOADING)
        self.assertEqual(states[1:4], [ItemState.SOURCED] * 3)
        self.assertEqual(states[4], ItemState.DOWNLOADING)

    def test_folder_without_the_tracks_is_not_finished(self):

        release = self.share_release(1, "short", score=95)
        self.share_release(1, "whole", score=50)
        job_id = self.create([release])
        self.steps(2)
        item = self.manager.jobs[job_id].items[0]
        folder = "Music\\Barker\\Album 1"
        self.assertEqual(item.current_source["username"], "short")

        self.automation.downloads["short", folder] = [
            {"virtual_path": f"{folder}\\{name}", "state": "finished", "bytes_transferred": 10, "size_bytes": 10,
             "will_retry": False} for name in ("cover.jpg", "info.nfo", "01 Song One.flac")
        ]
        self.steps(2)

        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertEqual(item.current_source["username"], "whole")

    def test_mp3_does_not_count_as_a_track_when_only_flac_is_wanted(self):

        release = self.share_release(1)
        job_id = self.create([release], formats=["flac"])
        self.steps(2)
        item = self.manager.jobs[job_id].items[0]
        folder = "Music\\Barker\\Album 1"

        self.automation.downloads["alice", folder] = [
            {"virtual_path": f"{folder}\\{name}", "state": "finished", "bytes_transferred": 10, "size_bytes": 10,
             "will_retry": False} for name in ("01 Song One.mp3", "02 Song Two.mp3")
        ]
        self.steps(1)

        self.assertNotEqual(item.state, ItemState.FINISHED)

    def test_partial_copies_can_finish_when_asked_for(self):

        job_id = self.create([self.share_release(1)], accept_partial=True)
        self.steps(2)
        item = self.manager.jobs[job_id].items[0]
        folder = "Music\\Barker\\Album 1"

        self.automation.downloads["alice", folder] = [
            {"virtual_path": f"{folder}\\01 Song One.flac", "state": "finished", "bytes_transferred": 10,
             "size_bytes": 10, "will_retry": False}
        ]
        self.steps(1)

        self.assertEqual(item.state, ItemState.FINISHED)

    def test_source_in_another_format_is_not_used_by_a_flac_only_job(self):

        job_id = self.create([self.share_release(1)], mode="preview")
        self.steps(1)
        job = self.manager.jobs[job_id]
        job.formats = ["flac"]
        job.mode = "download"
        job.items[0].sources[0]["formats"] = ["mp3"]

        self.assertEqual(self.manager._eligible_sources(job, job.items[0]), ([], False))

    def test_flac_only_chosen_during_a_search_discards_what_it_found(self):

        release = self.share_release(1)
        job_id = self.create([release], mode="preview")
        job = self.manager.jobs[job_id]
        original = self.manager.sourcer.find_sources

        async def find_sources(*args, **kwargs):

            result = await original(*args, **kwargs)
            job.formats = ["flac"]
            return result

        self.manager.sourcer.find_sources = find_sources
        self.steps(1)

        self.assertEqual(job.items[0].state, ItemState.PENDING)
        self.assertEqual(job.items[0].sources, [])

    def test_stalled_transfer_moves_to_next_uploader(self):

        release = self.share_release(1, "stuck", score=95)
        self.share_release(1, "here", score=50)
        job_id = self.create([release], start_timeout=900)

        self.steps(2)
        self.assertEqual(self.manager.jobs[job_id].items[0].current_source["username"], "stuck")

        self.now += 899
        self.steps(1)
        self.assertEqual(self.manager.jobs[job_id].items[0].current_source["username"], "stuck")

        self.now += 2
        self.steps(2)

        self.assertEqual(self.manager.jobs[job_id].items[0].current_source["username"], "here")
        self.assertEqual(self.automation.downloads["stuck", "Music\\Barker\\Album 1"], [])

    def test_progress_resets_the_stall_timer(self):

        job_id = self.create([self.share_release(1)], start_timeout=900)
        self.steps(2)

        self.now += 800
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "active", bytes_transferred=10)
        self.steps(1)

        self.now += 800
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_paused_by_user_does_not_count_as_stalled(self):

        job_id = self.create([self.share_release(1)], start_timeout=900)
        self.steps(2)

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "paused")
        self.now += 5000
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_disconnect_does_not_count_as_stalled(self):

        job_id = self.create([self.share_release(1)], start_timeout=900)
        self.steps(2)

        self.automation.connected = False
        self.now += 5000
        self.steps(1)
        self.automation.connected = True
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_failures_the_core_retries_are_not_final(self):

        job_id = self.create([self.share_release(1)])
        self.steps(2)

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "failed", will_retry=True)
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_all_files_failed_moves_on_and_searches_again(self):

        job_id = self.create([self.share_release(1)], retry_missing_hours=0)
        self.steps(2)

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "failed")
        self.steps(4)

        # Only one uploader: searched a second time, tried already, so it gives up
        self.assertEqual(self.item_states(job_id), [ItemState.FAILED])
        self.assertEqual(len(self.runner.started_downloads), 1)
        self.assertEqual(self.automation.queries, ["Barker Album 1", "Barker Album 1"])

    def test_partly_failed_album_is_reported_not_restarted(self):

        job_id = self.create([self.share_release(1)], retry_missing_hours=0)
        self.steps(2)

        downloads = self.automation.downloads["alice", "Music\\Barker\\Album 1"]
        downloads[0].update(state="finished", bytes_transferred=1000)
        downloads[1].update(state="failed")
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.FAILED])
        self.assertIn("1 of 2 files did not arrive", self.manager.jobs[job_id].items[0].note)
        self.assertEqual(len(self.runner.started_downloads), 1)

    def test_removed_downloads_are_respected(self):

        job_id = self.create([self.share_release(1)])
        self.steps(2)

        self.automation.downloads["alice", "Music\\Barker\\Album 1"] = []
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.CANCELLED])
        self.assertEqual(len(self.runner.started_downloads), 1)

    def test_missing_disk_blocks_downloads(self):

        job_id = self.create([self.share_release(1)])
        self.steps(1)

        self.destination_ok = False
        self.steps(3)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])
        self.assertIn("is the disk connected", self.manager.get_job(job_id)["blocked_reason"])

        self.destination_ok = True
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])
        self.assertIsNone(self.manager.get_job(job_id)["blocked_reason"])

    def test_not_connected_blocks_searching(self):

        self.automation.connected = False
        job_id = self.create([self.share_release(1)])

        self.steps(2)

        self.assertEqual(self.automation.queries, [])
        self.assertEqual(self.item_states(job_id), [ItemState.PENDING])
        self.assertIn("not connected", self.manager.get_job(job_id)["blocked_reason"])

    def test_preview_then_download_reuses_what_was_found(self):

        job_id = self.create([self.share_release(1), make_release(2)], mode="preview")
        self.steps(3)
        num_queries = len(self.automation.queries)

        self.manager.start_downloads(job_id)
        self.steps(1)

        self.assertEqual(len(self.automation.queries), num_queries)
        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING, ItemState.NOT_FOUND])

    def test_accept_partial_after_preview(self):

        release = make_release(1)
        self.share("Barker Album 1", "alice", "Music\\Barker\\Album 1", release, files=album_files(release)[:1])
        job_id = self.create([release], mode="preview")
        self.steps(1)
        self.assertEqual(self.item_states(job_id), [ItemState.PARTIAL])

        self.manager.start_downloads(job_id, accept_partial=True)
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])


class RetryTest(JobTestCase):

    def test_missing_releases_are_searched_again_later(self):

        job_id = self.create([make_release(1)], retry_missing_hours=24, max_retry_rounds=2)
        self.steps(1)

        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.WAITING_RETRY)
        num_queries = len(self.automation.queries)

        self.now += 23 * 3600
        self.steps(1)
        self.assertEqual(len(self.automation.queries), num_queries)

        # Someone shared it in the meantime
        self.share_release(1)
        self.now += 2 * 3600
        self.steps(2)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_gives_up_after_too_many_rounds(self):

        job_id = self.create([make_release(1)], retry_missing_hours=1, max_retry_rounds=2)

        for _ in range(2):
            self.steps(1)
            self.assertEqual(self.manager.jobs[job_id].status, JobStatus.WAITING_RETRY)
            self.now += 3601
            self.steps(1)

        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.FINISHED)

    def test_retry_missing_right_now(self):

        job_id = self.create([make_release(1)])
        self.steps(1)
        self.share_release(1)

        self.assertEqual(self.manager.retry_missing(job_id)["num_retried"], 1)
        self.steps(2)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])


class ControlTest(JobTestCase):

    def test_pause_and_resume(self):

        job_id = self.create([self.share_release(1)])

        self.manager.pause_job(job_id)
        self.steps(2)
        self.assertEqual(self.automation.queries, [])

        self.manager.resume_job(job_id)
        self.steps(1)
        self.assertEqual(len(self.automation.queries), 1)

    def test_pause_all_and_resume_all(self):

        job_a = self.create([self.share_release(1)])
        job_b = self.manager.create_job(
            "Other", [self.share_release(2)], label_id="musicbrainz:other", mode="download", search_interval=0
        )["job_id"]
        self.manager.pause_job(job_b)  # Paused by hand, so resume_all() leaves it alone

        self.assertEqual(self.manager.pause_all(), [job_a])

        job = self.manager.describe_job(job_a)
        self.assertEqual(job["status"], JobStatus.PAUSED)
        self.assertEqual(job["pause_mode"], "now")
        self.assertTrue(job["paused_by_all"])
        self.assertFalse(self.manager.describe_job(job_b)["paused_by_all"])

        self.steps(2)
        self.assertEqual(self.automation.queries, [])

        # Remembered across a restart
        self.assertTrue(self.make_manager().jobs[job_a].paused_by_all)

        self.assertEqual(self.manager.resume_all(), [job_a])
        self.assertEqual(self.manager.describe_job(job_a)["status"], JobStatus.RUNNING)
        self.assertFalse(self.manager.describe_job(job_a)["paused_by_all"])
        self.assertEqual(self.manager.describe_job(job_b)["status"], JobStatus.PAUSED)
        self.assertEqual(self.manager.resume_all(), [])

        self.steps(1)
        self.assertEqual(len(self.automation.queries), 1)

        # Pausing a job by hand after pause_all() takes it out of resume_all()
        self.assertEqual(self.manager.pause_all(), [job_a])
        self.manager.pause_job(job_a)
        self.assertEqual(self.manager.resume_all(), [])

        with self.assertRaises(ValueError):
            self.manager.pause_all(mode="later")

    def test_pause_after_album_finishes_what_has_started(self):

        job_id = self.create([self.share_release(1), self.share_release(2, "bob")], max_active_releases=1)
        self.steps(3)
        self.assertEqual(self.item_states(job_id)[0], ItemState.DOWNLOADING)

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "active", bytes_transferred=10)
        self.steps(1)
        self.manager.pause_job(job_id)
        self.steps(2)
        self.assertEqual(self.automation.downloads["alice", "Music\\Barker\\Album 1"][0]["state"], "active")

        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "finished", bytes_transferred=1000)
        self.steps(3)

        self.assertEqual(self.item_states(job_id)[0], ItemState.FINISHED)
        self.assertNotEqual(self.item_states(job_id)[1], ItemState.DOWNLOADING)

    def test_pause_after_album_leaves_queues_and_resuming_lines_up_again(self):

        self.automation.never_active.add("alice")
        job_id = self.create([self.share_release(1)])
        self.steps(2)
        item = self.manager.jobs[job_id].items[0]
        folder = "Music\\Barker\\Album 1"
        self.automation.set_transfer_state("alice", folder, "queued", bytes_transferred=0)
        self.assertTrue(item.is_queued)

        self.manager.pause_job(job_id)
        self.steps(1)

        self.assertEqual(item.state, ItemState.SOURCED)
        self.assertFalse(item.is_queued)
        self.assertEqual(self.automation.downloads["alice", folder], [])

        self.manager.resume_job(job_id)
        self.steps(1)

        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertTrue(item.is_queued)

    def test_pause_now_stops_transfers_and_resuming_continues_them(self):

        job_id = self.create([self.share_release(1)], start_timeout=900)
        self.steps(2)
        item = self.manager.jobs[job_id].items[0]
        folder = "Music\\Barker\\Album 1"
        self.automation.set_transfer_state("alice", folder, "active", bytes_transferred=100)
        self.steps(1)

        self.manager.pause_job(job_id, "now")
        self.steps(1)

        self.assertEqual({download["state"] for download in self.automation.downloads["alice", folder]}, {"paused"})
        self.assertEqual(item.state, ItemState.DOWNLOADING)

        # Having been paused for long doesn't make the uploader unreliable
        self.now += 5000
        self.steps(1)
        self.manager.resume_job(job_id)
        self.steps(1)

        self.assertEqual({download["state"] for download in self.automation.downloads["alice", folder]}, {"active"})
        self.assertEqual(item.current_source["username"], "alice")
        self.assertFalse(self.manager.jobs[job_id].transfers_paused)

    def test_unknown_way_to_pause_is_refused(self):

        job_id = self.create([self.share_release(1)])

        with self.assertRaises(ValueError):
            self.manager.pause_job(job_id, "soon")

    def test_skip_removes_unfinished_downloads(self):

        release = self.share_release(1)
        job_id = self.create([release])
        self.steps(2)

        anyio.run(self.manager.skip_release, job_id, release.release_id)
        item = self.manager.jobs[job_id].items[0]

        self.assertEqual(item.state, ItemState.CANCELLED)
        self.assertEqual(item.note, "skipped")
        self.assertEqual(self.automation.downloads["alice", "Music\\Barker\\Album 1"], [])

    def test_finished_release_cannot_be_skipped(self):

        release = self.share_release(1)
        job_id = self.create([release])
        self.steps(2)
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "finished", bytes_transferred=1000)
        self.steps(1)

        with self.assertRaises(ValueError):
            anyio.run(self.manager.skip_release, job_id, release.release_id)

    def test_prioritize_moves_a_release_to_the_front(self):

        first = self.share_release(1)
        second = self.share_release(2, "bob")
        job_id = self.create([first, second])

        self.manager.prioritize_release(job_id, second.release_id)
        self.steps(1)
        items = self.manager.jobs[job_id].items

        self.assertEqual(items[0].release["release_id"], second.release_id)
        self.assertEqual(items[0].state, ItemState.SOURCED)
        self.assertEqual(items[1].state, ItemState.PENDING)

    def test_prioritize_brings_back_what_was_skipped(self):

        release = self.share_release(1)
        job_id = self.create([release])
        anyio.run(self.manager.skip_release, job_id, release.release_id)

        self.manager.prioritize_release(job_id, release.release_id)
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])

    def test_paused_job_keeps_tracking_transfers(self):

        job_id = self.create([self.share_release(1)])
        self.steps(2)

        self.manager.pause_job(job_id)
        self.automation.set_transfer_state("alice", "Music\\Barker\\Album 1", "finished", bytes_transferred=1000)
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.FINISHED])
        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.PAUSED)

    def test_cancel_removes_only_unfinished_downloads(self):

        job_id = self.create([self.share_release(1), make_release(2)])
        self.steps(2)

        downloads = self.automation.downloads["alice", "Music\\Barker\\Album 1"]
        downloads[0].update(state="finished", bytes_transferred=1000)

        anyio.run(self.manager.cancel_job, job_id)

        remaining = self.automation.downloads["alice", "Music\\Barker\\Album 1"]
        self.assertEqual([item["state"] for item in remaining], ["finished"])
        self.assertEqual(self.manager.jobs[job_id].status, JobStatus.CANCELLED)
        self.assertEqual(self.item_states(job_id), [ItemState.CANCELLED, ItemState.NOT_FOUND])

    def test_remove_job(self):

        job_id = self.create([make_release(1)], mode="preview")

        with self.assertRaisesRegex(ValueError, "Cancel it first"):
            self.manager.remove_job(job_id)

        self.steps(1)
        self.assertTrue(self.manager.remove_job(job_id)["removed"])
        self.assertNotIn(job_id, self.make_manager().jobs)

    def test_cancelled_job_cannot_be_resumed(self):

        job_id = self.create([make_release(1)])
        anyio.run(self.manager.cancel_job, job_id)

        for action in (self.manager.resume_job, self.manager.start_downloads, self.manager.retry_missing):
            with self.assertRaises(ValueError):
                action(job_id)

    def test_one_running_job_per_label(self):

        self.create([make_release(1)])

        with self.assertRaisesRegex(ValueError, "already working on this label"):
            self.create([make_release(2)])

    def test_unknown_job(self):

        with self.assertRaises(KeyError):
            self.manager.get_job("nope")

    def test_no_releases(self):

        with self.assertRaisesRegex(ValueError, "No releases"):
            self.create([])

    def test_invalid_mode(self):

        with self.assertRaisesRegex(ValueError, "mode"):
            self.create([make_release(1)], mode="yolo")

    def test_invalid_state_filter(self):

        job_id = self.create([make_release(1)])

        with self.assertRaisesRegex(ValueError, "Unknown state"):
            self.manager.get_job(job_id, state="nope")


class PersistenceTest(JobTestCase):

    def test_survives_a_restart(self):

        job_id = self.create([self.share_release(1), self.share_release(2, "bob")], mode="preview")
        self.steps(1)

        restarted = self.make_manager()
        item = restarted.jobs[job_id].items[0]

        self.assertEqual(item.state, ItemState.SOURCED)
        self.assertEqual(item.sources[0]["username"], "alice")
        self.assertEqual(restarted.jobs[job_id].items[1].state, ItemState.PENDING)

        self.manager = restarted
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED, ItemState.SOURCED])

    def test_tracklist_is_kept(self):

        job_id = self.create([self.share_release(1)], mode="preview")
        self.steps(1)

        self.assertEqual(self.catalog.num_lookups, 1)
        self.manager = self.make_manager()
        self.assertEqual(len(self.manager.jobs[job_id].items[0].release["tracks"]), 2)

    def test_downtime_is_not_a_stall(self):

        job_id = self.create([self.share_release(1)], start_timeout=900)
        self.steps(2)

        self.now += 100000
        self.manager = self.make_manager()
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def test_corrupt_file_is_set_aside(self):

        with open(self.store_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("{not json")

        manager = self.make_manager()

        self.assertEqual(manager.jobs, {})
        self.assertTrue(os.path.exists(self.store_path + ".corrupt"))

    def test_file_is_valid_json_after_each_change(self):

        self.create([self.share_release(1)])
        self.steps(2)

        with open(self.store_path, encoding="utf-8") as file_handle:
            self.assertEqual(len(json.load(file_handle)["jobs"]), 1)


class DestinationTest(TestCase):

    def test_existing_folder(self):

        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)

        self.assertTrue(is_destination_available(folder))
        self.assertTrue(is_destination_available(os.path.join(folder, "new", "subfolder")))

    def test_unplugged_drive(self):

        self.assertFalse(is_destination_available("/Volumes/Not Plugged In/Music/downloads/BITE"))
        self.assertFalse(is_destination_available("/nonexistent/folder"))


class LibraryHiddenFilesTest(TestCase):

    def test_macos_sidecar_files_do_not_hide_an_album(self):

        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        release = make_release(1)
        release.tracks = tracks_of(release)
        album = os.path.join(folder, "Barker", "Album 1")
        os.makedirs(album)

        for basename, _length in album_files(release):
            for name in (basename, f"._{basename}"):
                with open(os.path.join(album, name), "wb"):
                    pass

        self.assertEqual(LibraryIndex([folder]).find_release(release), album)


class LibraryTest(TestCase):

    def setUp(self):

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)

    def add_album(self, path, basenames):

        album_folder = os.path.join(self.folder, path)
        os.makedirs(album_folder)

        for basename in basenames:
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        return album_folder

    @staticmethod
    def release():

        release = make_release(1)
        release.tracks = tracks_of(release)
        return release

    def test_finds_complete_album(self):

        path = self.add_album("Barker/Album 1", ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac", "cover.jpg"])
        self.assertEqual(LibraryIndex([self.folder]).find_release(self.release()), path)

    def test_incomplete_album_is_not_owned(self):

        self.add_album("Barker/Album 1", ["01 Album 1 Song One.flac"])
        self.assertIsNone(LibraryIndex([self.folder]).find_release(self.release()))

    def test_unrelated_album_is_not_owned(self):

        self.add_album("Barker/Album 2", ["01 Different.flac", "02 Other.flac"])
        self.assertIsNone(LibraryIndex([self.folder]).find_release(self.release()))

    def test_finds_by_catalog_number(self):

        release = self.release()
        release.title = "Something Not In The Path"
        release.catalog_numbers = ["BITE028"]
        path = self.add_album("[BITE028]", ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac"])

        self.assertEqual(LibraryIndex([self.folder]).find_release(release), path)

    def test_unplugged_drive_is_reported(self):

        missing = os.path.join(self.folder, "missing")
        library = LibraryIndex([missing])

        self.assertIsNone(library.find_release(self.release()))
        self.assertEqual(library.unavailable_folders, [missing])

    def test_rescans_after_a_while(self):

        clock = [0]
        library = LibraryIndex([self.folder], clock=lambda: clock[0])

        self.assertIsNone(library.find_release(self.release()))
        path = self.add_album("Barker/Album 1", ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac"])

        self.assertIsNone(library.find_release(self.release()))

        clock[0] += LibraryIndex.MAX_AGE + 1
        self.assertEqual(library.find_release(self.release()), path)

    def test_invalidate(self):

        library = LibraryIndex([self.folder])

        self.assertIsNone(library.find_release(self.release()))
        path = self.add_album("Barker/Album 1", ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac"])

        library.invalidate()
        self.assertEqual(library.find_release(self.release()), path)


class ReleaseListJobTest(JobTestCase):
    """Jobs started from a list of release IDs, as the albums of a playlist."""

    def create_by_id(self, number, **options):

        options.setdefault("search_interval", 0)
        release = Release(f"musicbrainz:{number}", "musicbrainz", "", "")
        return self.manager.create_job("Playlist", [release], mode="download", **options)["job_id"]

    def test_release_known_only_by_id_is_named_by_the_catalog(self):

        self.share_release(1)
        job_id = self.create_by_id(1)

        self.steps(3)

        job = self.manager.jobs[job_id]
        item = job.items[0]

        self.assertEqual(item.release["title"], "Album 1")
        self.assertEqual(item.release["artist"], "Barker")
        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertIsNone(job.label_id)
        self.assertIsNone(job.artist_id)
        self.assertEqual(self.manager.get_job(job_id)["releases"][0]["title"], "Album 1")

    def test_release_known_only_by_id_is_missing_while_the_catalog_is_down(self):

        self.catalog.fail_with = CatalogError("down")
        job_id = self.create_by_id(1)

        self.steps(1)

        item = self.manager.jobs[job_id].items[0]

        self.assertEqual(item.state, ItemState.NOT_FOUND)
        self.assertIn("catalog could not be asked", item.note)
        self.assertEqual(self.automation.queries, [])

    def test_releases_are_filed_by_artist(self):

        self.share_release(1)
        job_id = self.create_by_id(1, group_by="artist")

        self.steps(3)

        self.assertEqual(self.manager.jobs[job_id].download_folder, "/downloads")
        self.assertEqual(self.runner.started_downloads[-1][2], "/downloads/Barker")
        self.assertEqual(self.manager.jobs[job_id].items[0].download_folder, "/downloads/Barker/Album 1")

    def test_release_folder_names(self):

        barker = Release("musicbrainz:1", "musicbrainz", "Barker & Baumecker", "Album", primary_artist="Barker",
                         labels=["Ostgut Ton"])
        various = Release("musicbrainz:2", "musicbrainz", "Various Artists", "Compilation", labels=["BITE"])
        unknown = Release("musicbrainz:3", "musicbrainz", "Various Artists", "Compilation")

        self.assertEqual(release_folder_name(barker, "artist"), "Barker")
        self.assertEqual(release_folder_name(barker, "label"), "Ostgut Ton")
        self.assertEqual(release_folder_name(various, "artist"), "BITE")
        self.assertEqual(release_folder_name(various, "label"), "BITE")
        self.assertEqual(release_folder_name(unknown, "artist"), "Various Artists")
        self.assertEqual(release_folder_name(unknown, "label"), "Various Artists")
