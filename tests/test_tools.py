# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import tempfile

from unittest import TestCase

import anyio

from nicotine_mcp.catalog import CatalogError
from nicotine_mcp.catalog import filter_releases
from nicotine_mcp.jobs import JobManager
from nicotine_mcp.library import LibraryIndex
from nicotine_mcp.matching import Release
from nicotine_mcp.mcp_compat import ToolError
from nicotine_mcp.server import build_server
from nicotine_mcp.sourcing import ReleaseSourcer

from tests.test_jobs import FakeCatalog
from tests.test_jobs import FakeRunner
from tests.test_jobs import album_files
from tests.test_jobs import make_release
from tests.test_jobs import tracks_of

MB_LABEL = "musicbrainz:bite"
MB_ARTIST = "musicbrainz:phase-fatale"


class ToolCatalog(FakeCatalog):

    def __init__(self):

        super().__init__()
        self.albums = []
        self.releases = {}
        self.track_error = None
        self.also_label_id = None
        self.also_artist_id = None
        self.include_appearances = False

    def find_track_albums(self, _artist, _title, limit=25):

        if self.track_error:
            raise self.track_error

        return self.albums[:limit]

    def find_albums(self, _artist, _title, limit=10):

        if self.track_error:
            raise self.track_error

        return {"albums": [release for _number, release in sorted(self.releases.items())][:limit], "warnings": []}

    def find_labels(self, name, _source="all"):
        return {"labels": [{"label_id": MB_LABEL, "name": name}], "warnings": []}

    def find_artists(self, name, _source="all"):
        return {"artists": [{"artist_id": MB_ARTIST, "name": name}], "warnings": []}

    def artist_releases(self, artist_id, also_artist_id=None, include_appearances=False, **filters):

        self.also_artist_id = also_artist_id
        self.include_appearances = include_appearances
        releases = filter_releases(list(self.releases.values()), **filters)
        return {"artist_id": artist_id, "num_editions_listed": len(self.releases), "num_releases": len(self.releases),
                "num_selected": len(releases), "is_truncated": False, "releases": releases}

    def label_releases(self, label_id, also_label_id=None, **filters):

        self.also_label_id = also_label_id
        releases = filter_releases(list(self.releases.values()), **filters)
        return {"label_id": label_id, "num_editions_listed": len(self.releases), "num_releases": len(self.releases),
                "num_selected": len(releases), "is_truncated": False, "releases": releases}


class ToolRunner(FakeRunner):

    def __init__(self):

        super().__init__()
        self.dashboard_url = "http://127.0.0.1:8585/"
        self.automation.rank_tracks = self.rank_tracks

    def rank_tracks(self, token, _prefs, _limit):

        tracks = []

        for username, folder_path, files, _score in self.automation.network.get(self.automation.searches[token], []):
            tracks.append({"username": username, "folder_path": folder_path, "basename": files[0][0]})

        return {"tracks": tracks}


class ToolTest(TestCase):

    def setUp(self):

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)

        self.runner = ToolRunner()
        self.automation = self.runner.automation
        self.catalog = ToolCatalog()
        self.runner.catalog = self.catalog
        self.runner.library = LibraryIndex([os.path.join(self.folder, "library")])
        self.runner.sourcer = ReleaseSourcer(self.runner, self.catalog)
        self.runner.sourcer.race_seconds = 0
        self.runner.sourcer.race_poll_seconds = 0
        self.runner.sourcer.race_min_measure_seconds = 0
        self.runner.sourcer.race_settle_seconds = 0
        self.runner.jobs = JobManager(self.runner, self.runner.sourcer, library=self.runner.library,
                                      is_destination_ok=lambda _path: True)
        self.server = build_server(self.runner)

    def call(self, tool_name, **arguments):
        """Calls a tool the way a client would, returning (is_error, data)."""

        try:
            result = anyio.run(self.server.call_tool, tool_name, arguments)

        except ToolError as error:
            return True, str(error)

        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))

        if structured is not None and not is_error:
            return False, structured

        text = " ".join(getattr(item, "text", "") for item in result.content)

        try:
            return is_error, json.loads(text)

        except ValueError:
            return is_error, text

    def ok(self, tool_name, **arguments):

        is_error, data = self.call(tool_name, **arguments)
        self.assertFalse(is_error, data)
        return data

    def add_album(self, number=1, kind="album"):

        self.catalog.albums.append({"release_id": f"musicbrainz:{number}", "title": f"Album {number}", "kind": kind})

    def share_album(self, number=1, username="alice", files=None, query="Barker Album 1", folder=None):

        release = make_release(number)
        self.automation.network.setdefault(query, []).append((
            username, folder or f"Music\\Barker\\Album {number}",
            files if files is not None else album_files(release), 90
        ))

    def test_all_tools_are_registered(self):

        names = {tool.name for tool in anyio.run(self.server.list_tools)}

        self.assertLessEqual({
            "find_track_albums", "download_album_for_track", "download_release", "find_label", "get_label_releases",
            "start_label_job", "find_artist", "get_artist_releases", "start_artist_job", "get_job", "list_jobs",
            "start_job_downloads", "pause_job", "resume_job", "pause_all_downloads", "resume_all_downloads",
            "retry_job_missing", "cancel_job", "get_status", "download_best_album", "find_album", "start_release_job"
        }, names)


class AlbumForTrackTest(ToolTest):

    def test_downloads_the_verified_album(self):

        self.add_album()
        self.share_album()

        result = self.ok("download_album_for_track", artist="Barker", title="Album 1 Song One")

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "catalog")
        self.assertEqual(result["source"]["username"], "alice")
        self.assertEqual(result["source"]["match"]["verdict"], "complete")
        self.assertEqual(self.runner.started_downloads[0][:2], ("alice", "Music\\Barker\\Album 1"))

        # It stays watched, so a slow transfer can move to another uploader
        job = self.runner.jobs.get_job(result["job_id"])
        self.assertEqual(job["releases"][0]["state"], "downloading")
        self.assertEqual(job["releases"][0]["uploader"], "alice")
        self.assertEqual(job["status"], "running")

    def test_skips_wrong_album_with_the_right_name(self):

        self.add_album()
        self.share_album(username="impostor", files=[("01 Something Else.flac", 0), ("02 Not It.flac", 0)])
        self.share_album(username="alice")

        result = self.ok("download_album_for_track", artist="Barker", title="Album 1 Song One")

        self.assertEqual(result["source"]["username"], "alice")
        self.assertEqual(len(self.runner.started_downloads), 1)

    def test_next_uploader_when_transfer_does_not_start(self):

        self.add_album()
        self.share_album(username="stuck")
        self.share_album(username="alice")
        self.automation.never_active.add("stuck")

        result = self.ok("download_album_for_track", artist="Barker", title="Album 1 Song One")

        self.assertEqual(result["source"]["username"], "alice")
        self.assertEqual({entry["username"]: entry["state"] for entry in result["attempts"][0]["race"]},
                         {"stuck": "waiting", "alice": "active"})

    def test_already_owned(self):

        album_folder = os.path.join(self.folder, "library", "Barker", "Album 1")
        os.makedirs(album_folder)

        for basename, _length in album_files(make_release(1)):
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        self.add_album()
        self.share_album()

        result = self.ok("download_album_for_track", artist="Barker", title="Album 1 Song One")

        self.assertTrue(result["already_owned"])
        self.assertEqual(result["local_path"], album_folder)
        self.assertEqual(self.runner.started_downloads, [])

    def test_kinds_filter(self):

        self.add_album(1, kind="single")
        self.add_album(2, kind="album")
        self.share_album(1)
        self.share_album(2, username="bob", query="Barker Album 2")

        result = self.ok("download_album_for_track", artist="Barker", title="Song", kinds=["album"])

        self.assertEqual(result["release"]["title"], "Album 2")

    def test_specific_release(self):

        self.share_album(2, query="Barker Album 2")

        result = self.ok("download_album_for_track", artist="Barker", title="Song", release_id="musicbrainz:2")

        self.assertEqual(result["release"]["title"], "Album 2")
        self.assertTrue(result["success"])

    def test_falls_back_to_folder_when_catalog_is_down(self):

        self.catalog.track_error = CatalogError("musicbrainz.org returned HTTP 503")
        self.automation.network["Barker Song"] = [
            ("alice", "Music\\Barker - Utility", [("01 Song.flac", 0), ("02 Other.flac", 0), ("03 Third.flac", 0)], 90)
        ]

        result = self.ok("download_album_for_track", artist="Barker", title="Song")

        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "source_folder")
        self.assertIn("HTTP 503", result["warnings"][0])

    def test_falls_back_to_folder_when_catalog_does_not_know_the_song(self):

        self.automation.network["Barker Song"] = [
            ("alice", "Music\\Barker - Utility", [("01 Song.flac", 0), ("02 Other.flac", 0)], 90)
        ]

        result = self.ok("download_album_for_track", artist="Barker", title="Song")

        self.assertEqual(result["method"], "source_folder")
        self.assertEqual(result["source"]["folder_path"], "Music\\Barker - Utility")

    def test_flac_only(self):

        self.add_album()
        self.share_album(username="mp3user", files=album_files(make_release(1), "mp3"))
        self.share_album(username="both", files=[
            *album_files(make_release(1), "mp3"), *album_files(make_release(1), "flac")
        ])

        result = self.ok("download_album_for_track", artist="Barker", title="Album 1 Song One", flac_only=True)

        self.assertEqual(result["source"]["username"], "both")
        downloads = self.automation.downloads["both", "Music\\Barker\\Album 1"]
        self.assertEqual({item["virtual_path"].rpartition(".")[-1] for item in downloads}, {"flac"})

        # It stays FLAC only if the job has to move to somebody else later on
        self.assertEqual(self.runner.jobs.jobs[result["job_id"]].formats, ["flac"])

    def test_flac_only_without_a_flac_copy(self):

        self.add_album()
        self.share_album(files=album_files(make_release(1), "mp3"))

        result = self.ok("download_release", release_id="musicbrainz:1", flac_only=True)

        self.assertFalse(result["success"])
        self.assertEqual(self.runner.started_downloads, [])

    def test_flac_only_label_job(self):

        self.catalog.releases[1] = make_release(1)
        job_id = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE", flac_only=True)["job_id"]

        self.assertEqual(self.runner.jobs.jobs[job_id].formats, ["flac"])
        self.assertEqual(self.ok("start_job_downloads", job_id=job_id, flac_only=True)["formats"], ["flac"])

    def test_never_downloads_a_dump_folder(self):

        self.automation.network["Barker Song"] = [
            ("alice", "Music\\Misc", [("01 Song.flac", 0), ("02 Other.flac", 0)], 90),
            ("bob", "Shared\\Loose Tracks", [(f"Someone {index} - Song.mp3", 0) for index in range(30)], 80)
        ]

        result = self.ok("download_album_for_track", artist="Barker", title="Song")

        self.assertFalse(result["success"])
        self.assertEqual(self.runner.started_downloads, [])
        self.assertEqual(len(result["rejected_folders"]), 2)
        self.assertIn("error", result)


class ArtistToolsTest(ToolTest):

    def setUp(self):

        super().setUp()

        for number in range(1, 4):
            release = make_release(number)
            release.tracks = tracks_of(release)
            self.catalog.releases[number] = release

    def test_find_artist(self):
        self.assertEqual(self.ok("find_artist", name="Phase Fatale")["artists"][0]["artist_id"], MB_ARTIST)

    def test_get_artist_releases_marks_what_is_owned(self):

        listing = self.ok("get_artist_releases", artist_id=MB_ARTIST, also_artist_id="discogs:9")

        self.assertEqual(listing["num_selected"], 3)
        self.assertEqual(listing["num_owned"], 0)
        self.assertEqual(self.catalog.also_artist_id, "discogs:9")

    def test_job_is_a_preview_by_default(self):

        job = self.ok("start_artist_job", artist_id=MB_ARTIST, artist_name="Phase Fatale")

        self.assertEqual(job["mode"], "preview")
        self.assertEqual(job["artist_id"], MB_ARTIST)
        self.assertIsNone(job["label_id"])
        self.assertEqual(job["num_releases"], 3)
        self.assertFalse(self.catalog.include_appearances)

    def test_job_can_be_flac_only_with_appearances(self):

        job = self.ok("start_artist_job", artist_id=MB_ARTIST, artist_name="Phase Fatale", mode="download",
                      flac_only=True, include_appearances=True)

        self.assertEqual(job["formats"], ["flac"])
        self.assertTrue(self.catalog.include_appearances)
        self.assertTrue(self.runner.jobs.jobs[job["job_id"]].filters["include_appearances"])

    def test_same_artist_is_not_collected_twice(self):

        self.ok("start_artist_job", artist_id=MB_ARTIST, artist_name="Phase Fatale")
        is_error, message = self.call("start_artist_job", artist_id=MB_ARTIST, artist_name="Phase Fatale")

        self.assertTrue(is_error)
        self.assertIn("already working on this artist", message)

    def test_filters_narrow_the_job_and_it_says_so(self):

        job = self.ok("start_artist_job", artist_id=MB_ARTIST, artist_name="Phase Fatale", max_releases=2)

        self.assertEqual(job["num_releases"], 2)
        self.assertIn("2 of the 3", job["note"])

    def test_discogs_artist_id_is_not_mistaken_for_a_label(self):

        job_id = self.ok("start_artist_job", artist_id="discogs:9", artist_name="Phase Fatale")["job_id"]

        self.assertIsNone(self.runner.jobs.jobs[job_id].label_id)
        self.assertEqual(self.runner.jobs.jobs[job_id].artist_id, "discogs:9")


class ReleaseListToolsTest(ToolTest):

    def setUp(self):

        super().setUp()

        for number in range(1, 4):
            release = make_release(number)
            release.tracks = tracks_of(release)
            self.catalog.releases[number] = release

    def test_find_album_lists_matches_with_what_is_owned(self):

        result = self.ok("find_album", artist="Barker", title="Album 1")

        self.assertEqual([album["release_id"] for album in result["albums"]], [
            "musicbrainz:1", "musicbrainz:2", "musicbrainz:3"
        ])
        self.assertIsNone(result["albums"][0]["owned_at"])
        self.assertEqual(result["num_owned"], 0)
        self.assertEqual(result["warnings"], [])

    def test_find_album_needs_both_names(self):

        self.catalog.track_error = ValueError("Both an artist and a title are required")
        is_error, message = self.call("find_album", artist="", title="Album 1")

        self.assertTrue(is_error)
        self.assertIn("Both an artist and a title", message)

    def test_start_release_job_collects_each_release_once(self):

        job = self.ok("start_release_job", name="soul-less body music", mode="download", flac_only=True, releases=[
            {"release_id": "musicbrainz:1", "artist": "Barker", "title": "Album 1"},
            {"release_id": "musicbrainz:1"},
            {"release_id": "discogs:7", "artist": "Barker", "title": "Album 1"},
            {"release_id": "musicbrainz:2", "artist": "Barker", "title": "Album 2"},
            {"release_id": "musicbrainz:3"}
        ])

        self.assertEqual(job["num_releases"], 3)
        self.assertEqual(job["num_duplicates_left_out"], 2)
        self.assertEqual(job["mode"], "download")
        self.assertEqual(job["formats"], ["flac"])
        self.assertIsNone(job["label_id"])
        self.assertIsNone(job["artist_id"])

        items = self.runner.jobs.jobs[job["job_id"]].items

        self.assertEqual([item.release["title"] for item in items], ["Album 1", "Album 2", ""])
        self.assertEqual(items[2].release["source"], "musicbrainz")
        self.assertEqual(self.runner.jobs.jobs[job["job_id"]].group_by, "artist")

    def test_start_release_job_rejects_unknown_ids_and_empty_lists(self):

        is_error, message = self.call("start_release_job", name="x", releases=[{"release_id": "spotify:1"}])

        self.assertTrue(is_error)
        self.assertIn("Invalid release ID 'spotify:1'", message)

        is_error, message = self.call("start_release_job", name="x", releases=[])

        self.assertTrue(is_error)
        self.assertIn("No releases given", message)


class LabelToolsTest(ToolTest):

    def add_catalog(self, count=4):

        for number in range(1, count + 1):
            release = make_release(number)
            release.year = 2020 + number
            release.kind = "ep" if number % 2 else "album"
            self.catalog.releases[number] = release

    def test_find_label(self):
        self.assertEqual(self.ok("find_label", name="Bite")["labels"][0]["label_id"], MB_LABEL)

    def test_get_label_releases_marks_owned(self):

        self.add_catalog(2)
        album_folder = os.path.join(self.folder, "library", "Barker", "Album 1")
        os.makedirs(album_folder)

        for basename, _length in album_files(make_release(1)):
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        self.catalog.releases[1].tracks = tracks_of(self.catalog.releases[1])
        result = self.ok("get_label_releases", label_id=MB_LABEL)

        self.assertEqual(result["num_owned"], 1)
        self.assertEqual(result["releases"][0]["owned_at"], album_folder)
        self.assertNotIn("tracks", result["releases"][0])

    def test_get_label_releases_filters(self):

        self.add_catalog()
        result = self.ok("get_label_releases", label_id=MB_LABEL, kinds=["album"])

        self.assertEqual({release["kind"] for release in result["releases"]}, {"album"})
        self.assertEqual(result["num_selected"], 2)

    def test_discogs_listing_comes_with_a_warning(self):

        self.add_catalog(1)
        self.assertIn("only credited", self.ok("get_label_releases", label_id="discogs:178535")["note"])

    def test_job_is_a_preview_unless_told_otherwise(self):

        self.add_catalog()
        job = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE")

        self.assertEqual(job["mode"], "preview")
        self.assertEqual(job["num_releases"], 4)
        self.assertEqual(job["num_releases_in_catalog"], 4)

    def test_sample_spreads_over_the_catalog(self):

        self.add_catalog(10)
        job = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE", max_releases=3, sample=True)
        items = self.runner.jobs.jobs[job["job_id"]].items

        self.assertEqual([item.release["title"] for item in items], ["Album 1", "Album 4", "Album 7"])

        # Nobody should mistake a sample for the whole label
        self.assertEqual((job["num_releases"], job["num_in_catalog"]), (3, 10))
        self.assertIn("3 of the 10", job["note"])

    def test_whole_catalog_has_no_note(self):

        self.add_catalog(4)
        self.assertNotIn("note", self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE"))

    def test_second_catalog_is_passed_on(self):

        self.add_catalog(1)
        self.ok("get_label_releases", label_id=MB_LABEL, also_label_id="discogs:40459")
        self.assertEqual(self.catalog.also_label_id, "discogs:40459")

        self.catalog.also_label_id = None
        self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE", also_label_id="discogs:40459")
        self.assertEqual(self.catalog.also_label_id, "discogs:40459")

    def test_max_releases_without_sample(self):

        self.add_catalog(10)
        job = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE", max_releases=2)
        items = self.runner.jobs.jobs[job["job_id"]].items

        self.assertEqual([item.release["title"] for item in items], ["Album 1", "Album 2"])

    def test_job_controls(self):

        self.add_catalog(1)
        job_id = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE")["job_id"]

        self.assertEqual(self.ok("pause_job", job_id=job_id)["status"], "paused")
        self.assertEqual(self.ok("resume_job", job_id=job_id)["status"], "running")
        self.assertEqual(self.ok("get_job", job_id=job_id)["releases"][0]["state"], "pending")
        self.assertEqual(self.ok("list_jobs")["jobs"][0]["job_id"], job_id)
        self.assertEqual(self.ok("start_job_downloads", job_id=job_id)["mode"], "download")
        self.assertEqual(self.ok("retry_job_missing", job_id=job_id)["num_retried"], 0)
        self.assertEqual(self.ok("cancel_job", job_id=job_id)["status"], "cancelled")

    def test_errors_are_reported_to_the_agent(self):

        self.add_catalog(1)

        for name, arguments in (
            ("get_job", {"job_id": "nope"}),
            ("pause_job", {"job_id": "nope"}),
            ("cancel_job", {"job_id": "nope"}),
            ("start_label_job", {"label_id": MB_LABEL, "label_name": "BITE", "year_from": 2100}),
            ("start_label_job", {"label_id": MB_LABEL, "label_name": "BITE", "kinds": ["lp"]})
        ):
            is_error, data = self.call(name, **arguments)
            self.assertTrue(is_error, (name, data))

    def test_release_type_round_trip(self):
        """What a job stores can be read back as a release with its tracklist."""

        self.add_catalog(1)
        job_id = self.ok("start_label_job", label_id=MB_LABEL, label_name="BITE")["job_id"]
        item = self.runner.jobs.jobs[job_id].items[0]

        self.assertEqual(Release.from_dict(item.release).title, "Album 1")
