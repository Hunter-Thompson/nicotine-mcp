# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil
import tempfile

from unittest import TestCase

import anyio

from nicotine_mcp import speed as speed_module
from nicotine_mcp.jobs import ItemState
from nicotine_mcp.speed import Entrant
from nicotine_mcp.speed import RACE_ENTRANTS
from nicotine_mcp.speed import SpeedTracker
from nicotine_mcp.speed import pick_entrants
from nicotine_mcp.speed import race_downloads

from tests.test_jobs import FakeRunner
from tests.test_jobs import JobTestCase
from tests.test_jobs import make_release


class SpeedTest(JobTestCase):

    def test_race_picks_the_fastest_not_the_best_ranked(self):

        release = self.share_release(1, "claims", score=95)
        self.share_release(1, "really", score=50)
        self.automation.uploader_speeds.update(claims=100, really=9000)
        self.manager.jobs.clear()
        job_id = self.create([release])

        self.steps(1)
        self.manager.sourcer.race_seconds = 2
        self.steps(1)

        item = self.manager.jobs[job_id].items[0]

        self.assertEqual(item.current_source["username"], "really")
        self.assertEqual([username for username, _path in self.automation.discarded], ["claims"])
        self.assertEqual({entry["username"] for entry in item.last_race}, {"claims", "really"})
        self.assertGreater(self.automation.measured["really"], self.automation.measured["claims"])

    def test_loser_stays_available_as_fallback(self):

        release = self.share_release(1, "first", score=95)
        self.share_release(1, "second", score=50)
        job_id = self.create([release], start_timeout=900)

        self.steps(2)
        winner = self.manager.jobs[job_id].items[0].current_source["username"]

        self.now += 901
        self.steps(2)

        now_downloading = self.manager.jobs[job_id].items[0].current_source["username"]
        self.assertEqual({winner, now_downloading}, {"first", "second"})
        self.assertEqual(self.automation.measured[winner], -1)

    def test_more_sources_than_entrants(self):

        release = make_release(1)

        for index in range(5):
            self.share_release(1, f"user{index}", score=90 - index)

        job_id = self.create([release])
        self.steps(2)

        self.assertEqual({entry["username"] for entry in self.manager.jobs[job_id].items[0].last_race},
                         {"user0", "user1", "user2", "user3"})

    def test_sources_saved_without_probe_file_can_still_race(self):
        """Found by a preview from before races existed."""

        job_id = self.create([self.share_release(1)], mode="preview")
        self.steps(1)
        del self.manager.jobs[job_id].items[0].sources[0]["probe_file"]

        self.manager.start_downloads(job_id)
        self.steps(1)

        self.assertEqual(self.item_states(job_id), [ItemState.DOWNLOADING])

    def slow_job(self, min_speed=300_000):
        """A job downloading from 'slow', who shares it with 'fast', one track finished."""

        release = self.share_release(1, "slow", score=95)
        self.share_release(1, "fast", score=50)
        self.automation.uploader_speeds.update(slow=5000, fast=1)
        job_id = self.create([release], min_speed=min_speed)

        self.steps(1)
        self.manager.sourcer.race_seconds = 1
        self.steps(1)

        item = self.manager.jobs[job_id].items[0]
        self.assertEqual(item.current_source["username"], "slow")

        downloads = self.automation.downloads["slow", "Music\\Barker\\Album 1"]
        downloads[0].update(state="finished", bytes_transferred=1000, size_bytes=1000)
        downloads[1].update(state="active", size_bytes=100_000_000)
        return job_id, item

    def test_slow_uploader_is_replaced_for_remaining_tracks(self):

        _job_id, item = self.slow_job()
        destination = item.download_folder

        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 10
        self.steps(1)
        self.assertEqual(item.current_source["username"], "slow")

        self.now += 61
        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 20
        self.steps(1)

        self.assertEqual(item.current_source["username"], "fast")
        self.assertIn("switched from slow to fast", item.note)
        self.assertEqual(self.automation.enqueued_files, [
            ("fast", "Music\\Barker\\Album 1", ["02 Album 1 Song Two.flac"], destination)
        ])
        self.assertEqual(item.done_files, ["01 Album 1 Song One.flac"])

        # The finished track stays, only the rest was discarded
        remaining = self.automation.downloads["slow", "Music\\Barker\\Album 1"]
        self.assertEqual([download["state"] for download in remaining], ["finished"])

        self.assertEqual(item.num_files_finished, 1)
        self.automation.set_transfer_state("fast", "Music\\Barker\\Album 1", "finished", bytes_transferred=1000)
        self.steps(1)

        self.assertEqual(item.state, ItemState.FINISHED)
        self.assertEqual((item.num_files_finished, item.num_files), (2, 2))
        self.assertEqual(item.progress_percent, 100.0)
        self.assertEqual(item.local_path, destination)

    def test_fast_enough_stays(self):

        _job_id, item = self.slow_job()

        self.now += 61
        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 60_000_000
        self.steps(1)

        self.assertEqual(item.current_source["username"], "slow")

    def test_switching_can_be_turned_off(self):

        _job_id, item = self.slow_job(min_speed=0)

        self.now += 61
        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 20
        self.steps(1)

        self.assertEqual(item.current_source["username"], "slow")

    def test_does_not_switch_to_somebody_known_to_be_slower(self):

        _job_id, item = self.slow_job()
        self.automation.measured["fast"] = 100
        self.automation.measured["slow"] = 200_000

        self.now += 61
        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 12_000_000
        self.steps(1)

        self.assertEqual(item.current_source["username"], "slow")

    def test_no_switch_close_to_the_end(self):

        _job_id, item = self.slow_job()

        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 95_000_000
        self.steps(1)

        self.now += 61
        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1]["bytes_transferred"] = 95_000_010
        self.steps(1)

        self.assertEqual(item.current_source["username"], "slow")

    def test_failed_tracks_come_from_another_uploader(self):

        _job_id, item = self.slow_job()

        self.automation.downloads["slow", "Music\\Barker\\Album 1"][1].update(state="failed")
        self.steps(1)

        self.assertEqual(item.state, ItemState.DOWNLOADING)
        self.assertEqual(item.current_source["username"], "fast")
        self.assertEqual(item.done_files, ["01 Album 1 Song One.flac"])

    def test_copy_lacking_tracks_is_no_improvement(self):

        _job_id, item = self.slow_job()
        self.runner.listings["fast", "Music\\Barker\\Album 1"] = [("cover.jpg", 0)]

        self.now += 61
        self.steps(1)

        self.assertEqual(item.current_source["username"], "slow")


class PickEntrantsTest(TestCase):

    @staticmethod
    def candidate(username, quality=10000):
        return {"username": username, "quality": quality}

    @staticmethod
    def pick(candidates, speeds=None, limit=RACE_ENTRANTS):
        return [item["username"] for item in pick_entrants(candidates, speeds or {}, lambda c: c["quality"], limit)]

    def test_ranking_decides_when_nothing_is_known(self):

        names = [f"user{index}" for index in range(6)]
        self.assertEqual(self.pick([self.candidate(name) for name in names]), names[:RACE_ENTRANTS])

    def test_known_slow_uploaders_give_way(self):
        """A place in a race is wasted on somebody already measured at 80 KB/s."""

        candidates = [self.candidate(name) for name in ("slow", "unknown", "fast", "other")]
        speeds = {"slow": 80_000, "fast": 1_500_000}

        self.assertEqual(self.pick(candidates, speeds, 2), ["fast", "unknown"])

    def test_known_slow_uploaders_race_if_there_is_nobody_else(self):
        self.assertEqual(self.pick([self.candidate("slow")], {"slow": 80_000}), ["slow"])

    def test_lesser_quality_cannot_win_by_being_fast(self):

        candidates = [self.candidate("flac"), self.candidate("mp3", 320), self.candidate("flac2")]

        self.assertEqual(self.pick(candidates, {"mp3": 5_000_000}), ["flac", "flac2"])

    def test_similar_lossy_quality_races(self):

        candidates = [self.candidate("a", 320), self.candidate("b", 256), self.candidate("c", 128)]

        self.assertEqual(self.pick(candidates), ["a", "b"])

    def test_no_candidates(self):
        self.assertEqual(self.pick([]), [])


class RaceTest(TestCase):
    """The race itself, with time under control."""

    def setUp(self):

        self.runner = FakeRunner()
        self.automation = self.runner.automation
        self.now = 0.0
        self.original_sleep = speed_module.anyio.sleep

        async def fake_sleep(seconds):
            self.now += seconds or 1

        speed_module.anyio.sleep = fake_sleep
        self.addCleanup(setattr, speed_module.anyio, "sleep", self.original_sleep)

    def race(self, *usernames, **kwargs):

        entrants = []

        for username in usernames:
            self.automation.enqueue_probe(username, "Folder", "track.flac", 100_000_000)
            entrants.append(Entrant(username, "Folder\\track.flac"))

        async def run():
            return await race_downloads(self.runner, entrants, clock=lambda: self.now, **kwargs)

        return anyio.run(run), entrants

    def test_fastest_wins_and_others_are_removed(self):

        self.automation.uploader_speeds.update(slow=100_000, fast=2_000_000, medium=500_000)
        winner, _entrants = self.race("slow", "fast", "medium")

        self.assertEqual(winner.username, "fast")
        self.assertEqual(sorted(username for username, _path in self.automation.discarded), ["medium", "slow"])
        self.assertEqual(list(self.automation.probes), [("fast", "Folder\\track.flac")])

    def test_measures_real_speed(self):

        self.automation.uploader_speeds["a"] = 1_000_000
        winner, _entrants = self.race("a", "b")

        self.assertAlmostEqual(winner.speed, 1_000_000, delta=150_000)
        self.assertAlmostEqual(self.automation.measured["a"], 1_000_000, delta=150_000)

    def test_initial_burst_does_not_count(self):
        """Network buffers filling up looks fast, and says nothing about what follows."""

        self.automation.uploader_speeds.update(bursty=100_000, steady=500_000)
        original_get_download = self.automation.get_download
        polls = {"bursty": 0}

        def get_download(username, virtual_path):

            download = original_get_download(username, virtual_path)

            if username == "bursty":
                polls["bursty"] += 1
                download["bytes_transferred"] += 3_000_000 if polls["bursty"] > 1 else 0

            return download

        self.automation.get_download = get_download
        winner, entrants = self.race("bursty", "steady")

        self.assertEqual(winner.username, "steady")
        self.assertAlmostEqual(entrants[0].speed, 100_000, delta=20_000)

    def test_ties_go_to_the_earlier_entrant(self):

        winner, _entrants = self.race("first", "second")
        self.assertEqual(winner.username, "first")

    def test_nobody_starts(self):

        self.automation.never_active.update({"a", "b"})
        winner, _entrants = self.race("a", "b", start_timeout=5)

        self.assertIsNone(winner)
        self.assertEqual(self.now, 5)
        self.assertEqual(self.automation.probes, {})

    def test_one_who_never_starts_does_not_hold_up_the_race(self):

        self.automation.never_active.add("stuck")
        winner, _entrants = self.race("stuck", "fine", race_seconds=10)

        self.assertEqual(winner.username, "fine")
        self.assertEqual(self.now, 10)

    def test_only_one_left_needs_no_race(self):

        winner, _entrants = self.race("only")

        self.assertEqual(winner.username, "only")
        self.assertLess(self.now, 3)

    def test_failure_counts_against_an_uploader(self):

        self.automation.never_active.add("broken")
        self.automation.enqueue_probe("broken", "Folder", "track.flac")
        self.automation.probes["broken", "Folder\\track.flac"]["state"] = "failed"
        entrants = [Entrant("broken", "Folder\\track.flac")]

        async def run():
            return await race_downloads(self.runner, entrants, clock=lambda: self.now)

        self.assertIsNone(anyio.run(run))
        self.assertEqual(self.automation.measured["broken"], -1)

    def test_one_who_finished_beats_a_faster_transfer_still_running(self):

        self.automation.enqueue_probe("done", "Folder", "small.flac", 5_000)
        self.automation.enqueue_probe("running", "Folder", "big.flac", 5_000_000)
        self.automation.uploader_speeds["done"] = 1000
        self.automation.uploader_speeds["running"] = 900_000
        original_get_download = self.automation.get_download

        def get_download(username, virtual_path):

            download = original_get_download(username, virtual_path)

            if download["bytes_transferred"] >= download["size_bytes"] > 0:
                download["state"] = "finished"
                self.automation.probes[username, virtual_path]["state"] = "finished"

            return download

        self.automation.get_download = get_download
        entrants = [Entrant("running", "Folder\\big.flac"), Entrant("done", "Folder\\small.flac")]

        winner = anyio.run(race_downloads, self.runner, entrants)

        self.assertEqual(winner.username, "done")

    def test_finishing_early_ends_the_race(self):

        self.automation.uploader_speeds["fast"] = 60_000_000
        self.automation.enqueue_probe("fast", "Folder", "small.flac", 5_000_000)
        self.automation.enqueue_probe("slow", "Folder", "small.flac", 5_000_000)
        self.automation.uploader_speeds["slow"] = 1000
        entrants = [Entrant("slow", "Folder\\small.flac"), Entrant("fast", "Folder\\small.flac")]

        original_get_download = self.automation.get_download

        def get_download(username, virtual_path):

            download = original_get_download(username, virtual_path)

            if download["bytes_transferred"] >= download["size_bytes"] > 0:
                download["state"] = "finished"

            return download

        self.automation.get_download = get_download

        async def run():
            return await race_downloads(self.runner, entrants, clock=lambda: self.now)

        winner = anyio.run(run)

        self.assertEqual(winner.username, "fast")
        self.assertEqual(winner.state, "finished")
        self.assertLess(self.now, 3)


class SpeedTrackerTest(TestCase):

    def setUp(self):

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = os.path.join(self.folder, "speeds.json")
        self.now = 1_000_000.0

    def tracker(self):
        return SpeedTracker(self.path, clock=lambda: self.now)

    def test_unknown(self):
        self.assertEqual(self.tracker().estimate("nobody"), 0)

    def test_first_measurement_counts_fully(self):

        tracker = self.tracker()
        tracker.record("a", 500_000, 3)

        self.assertEqual(tracker.estimate("a"), 500_000)

    def test_longer_measurements_count_for_more(self):

        short = self.tracker()
        short.record("a", 1_000_000, 60)
        short.record("a", 0, 3)

        long = self.tracker()
        long.record("a", 1_000_000, 60)
        long.record("a", 0, 60)

        self.assertEqual(short.estimate("a"), 950_000)
        self.assertEqual(long.estimate("a"), 0)

    def test_failures_lower_the_estimate(self):

        tracker = self.tracker()
        tracker.record("a", 1_000_000, 60)
        tracker.record_failure("a")
        tracker.record_failure("unknown")

        self.assertEqual(tracker.estimate("a"), 500_000)
        self.assertLess(tracker.estimate("unknown"), 50_000)
        self.assertGreater(tracker.estimate("unknown"), 0)

    def test_survives_a_restart(self):

        tracker = self.tracker()
        tracker.record("a", 700_000, 10)
        tracker.save()

        self.assertEqual(self.tracker().estimate("a"), 700_000)

    def test_old_measurements_expire(self):

        tracker = self.tracker()
        tracker.record("a", 700_000, 10)
        tracker.save()

        self.now += 90 * 24 * 3600
        self.assertEqual(self.tracker().estimate("a"), 0)

    def test_corrupt_file_is_ignored(self):

        with open(self.path, "w", encoding="utf-8") as file_handle:
            file_handle.write("{nope")

        self.assertEqual(self.tracker().estimate("a"), 0)

    def test_fastest(self):

        tracker = self.tracker()
        tracker.record("slow", 1000, 10)
        tracker.record("fast", 9000, 10)

        self.assertEqual([item["username"] for item in tracker.fastest(1)], ["fast"])
        self.assertEqual(len(tracker), 2)
