# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os

from nicotine_mcp.jobs import ItemState
from nicotine_mcp.library import LibraryIndex

from tests.test_jobs import JobTestCase
from tests.test_jobs import album_files
from tests.test_jobs import make_release


class FlacOnlyTest(JobTestCase):

    def share_formats(self, username, extensions, number=1, score=90):

        release = make_release(number)
        files = []

        for extension in extensions:
            files += album_files(release, extension)

        self.share(f"Barker Album {number}", username, f"Music\\Barker\\{username} Album {number}", release, score,
                   files=[*files, ("cover.jpg", 0)])
        return release

    def downloaded_names(self, username, number=1):
        return [item["virtual_path"].rpartition("\\")[-1] for item in
                self.automation.downloads[username, f"Music\\Barker\\{username} Album {number}"]]

    def test_folder_with_both_formats_gives_only_flac(self):

        job_id = self.create([self.share_formats("alice", ["flac", "mp3"])], formats=["flac"])
        self.steps(2)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])
        self.assertEqual(self.downloaded_names("alice"),
                         ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac", "cover.jpg"])

    def test_mp3_folder_does_not_count_as_having_it(self):

        job_id = self.create([self.share_formats("alice", ["mp3"])], formats=["flac"])
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.NOT_FOUND])

    def test_flac_tracks_alone_have_to_be_complete(self):
        """All tracks are in the folder, but only one of them in FLAC."""

        release = make_release(1)
        files = album_files(release, "mp3") + album_files(release, "flac")[:1]
        self.share("Barker Album 1", "alice", "Music\\Barker\\Album 1", release, files=files)
        job_id = self.create([release], formats=["flac"])

        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.PARTIAL])

    def test_race_is_on_a_flac_file(self):

        self.create([self.share_formats("alice", ["mp3", "flac"])], formats=["flac"])
        self.steps(1)

        source = next(iter(self.manager.jobs.values())).items[0].sources[0]
        self.assertTrue(source["probe_file"]["basename"].endswith(".flac"))

    def test_owned_mp3_does_not_stop_a_flac_download(self):

        album_folder = os.path.join(self.folder, "library", "Barker", "Album 1")
        os.makedirs(album_folder)
        release = self.share_formats("alice", ["flac"])

        for basename, _length in album_files(release, "mp3"):
            with open(os.path.join(album_folder, basename), "w", encoding="utf-8"):
                pass

        self.manager = self.make_manager(library=LibraryIndex([os.path.join(self.folder, "library")]))
        job_id = self.create([release], formats=["flac"], mode="preview")
        self.steps(1)
        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED])

        self.manager.jobs.clear()
        job_id = self.create([release], mode="preview")
        self.steps(1)
        self.assertEqual(self.item_states(job_id), [ItemState.OWNED])

    def test_preview_counts_flac_copies(self):

        job_id = self.create([self.share_formats("alice", ["flac"]), self.share_formats("bob", ["mp3"], 2)],
                             mode="preview")
        self.steps(3)

        job = self.manager.describe_job(job_id)
        self.assertEqual((job["num_available"], job["num_available_flac"]), (2, 1))

    def test_flac_only_after_preview(self):

        flac_release = self.share_formats("alice", ["flac"], 1)
        mp3_release = self.share_formats("bob", ["mp3"], 2)
        job_id = self.create([flac_release, mp3_release], mode="preview")
        self.steps(3)
        num_searches = len(self.automation.queries)

        self.manager.start_downloads(job_id, flac_only=True)
        self.assertEqual(self.item_states(job_id), [ItemState.SOURCED, ItemState.PENDING])
        self.assertEqual(self.manager.jobs[job_id].formats, ["flac"])

        self.steps(1)

        # The FLAC one starts without another search, the MP3 one is looked for in FLAC, and isn't there
        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING, ItemState.NOT_FOUND])
        self.assertGreater(len(self.automation.queries), num_searches)
        self.assertEqual(self.downloaded_names("alice"),
                         ["01 Album 1 Song One.flac", "02 Album 1 Song Two.flac", "cover.jpg"])

    def test_switching_uploaders_stays_in_flac(self):

        release = self.share_formats("slow", ["flac"], score=95)
        self.share_formats("fast", ["mp3", "flac"], score=50)
        self.automation.uploader_speeds.update(slow=5000, fast=1)
        job_id = self.create([release], formats=["flac"])
        self.steps(1)
        self.manager.sourcer.race_seconds = 1
        self.steps(1)

        item = self.manager.jobs[job_id].items[0]
        self.assertEqual(item.current_source["username"], "slow")

        downloads = self.automation.downloads["slow", "Music\\Barker\\slow Album 1"]
        downloads[0].update(state="finished", bytes_transferred=1000)
        downloads[1].update(state="active", size_bytes=100_000_000, bytes_transferred=5)

        self.now += 61
        self.steps(1)

        self.assertEqual(item.current_source["username"], "fast")
        self.assertEqual(self.automation.enqueued_files[0][2], ["02 Album 1 Song Two.flac"])
