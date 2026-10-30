# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import tempfile

from unittest import TestCase

from nicotine_mcp import portable
from nicotine_mcp.cli import build_parser
from nicotine_mcp.job_models import Job
from nicotine_mcp.job_models import JobItem
from nicotine_mcp.jobs import JOBS_FILE_NAME
from nicotine_mcp.speed import SPEEDS_FILE_NAME


def make_job(job_id, status="running", folder="/Volumes/Drive/Music/downloads/BITE"):

    return {"job_id": job_id, "name": "BITE", "status": status, "download_folder": folder, "items": [
        {"state": "finished", "download_folder": f"{folder}/Altars", "local_path": f"{folder}/Altars"}
    ]}


SETTINGS = {
    "downloaddir": "/Volumes/Drive/Music/downloads",
    "incompletedir": "/Volumes/Drive/Music/incomplete",
    "shared": [("downloads", "/Volumes/Drive/Music/downloads")],
    "buddyshared": [],
    "use_download_speed_limit": "unlimited",
    "downloadlimit": 1000,
    "autoclear_downloads": True,
    "uploaddir": "/Users/somebody/received"
}


class PortableTestCase(TestCase):

    def setUp(self):

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.source = os.path.join(self.folder, "source")
        self.target = os.path.join(self.folder, "target")
        os.makedirs(self.source)
        os.makedirs(self.target)

    def write(self, folder, name, data):

        with open(os.path.join(folder, name), "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle)

    def read(self, folder, name):

        with open(os.path.join(folder, name), encoding="utf-8") as file_handle:
            return json.load(file_handle)

    def fill_source(self):

        self.write(self.source, JOBS_FILE_NAME, {"jobs": [make_job("a"), make_job("b", "paused")]})
        self.write(self.source, SPEEDS_FILE_NAME, {"alice": [1000, 50.0]})
        self.write(self.source, portable.TRANSFERS_FILE_NAME, [
            ["alice", "Music\\1.flac", "/Volumes/Drive/Music/downloads/BITE/Altars", "Paused", 10, 5, {}]
        ])


class ExportTest(PortableTestCase):

    def test_contains_jobs_of_every_status_and_the_download_list(self):

        self.fill_source()

        data = portable.build_export(self.source, SETTINGS)

        self.assertEqual([job["status"] for job in data["jobs"]], ["running", "paused"])
        self.assertEqual(len(data["transfers"]), 1)
        self.assertEqual(portable.summarize(data)["jobs_by_status"], {"running": 1, "paused": 1})

    def test_only_listed_settings_get_in(self):

        settings = {**SETTINGS, "login": "me", "passw": "secret"}

        data = portable.build_export(self.source, settings)
        content = json.dumps(data)

        self.assertNotIn("secret", content)
        self.assertNotIn("passw", content)
        self.assertNotIn("uploaddir", content)
        self.assertEqual(set(data["settings"]) - set(portable.SHARED_SETTINGS), set())

    def test_empty_profile_can_be_exported(self):

        data = portable.build_export(self.source, SETTINGS)

        self.assertEqual((data["jobs"], data["transfers"], data["speeds"]), ([], [], {}))

    def test_refuses_a_shared_folder(self):

        shared = os.path.join(self.folder, "shared")
        os.makedirs(shared)
        data = portable.build_export(self.source, {"shared": [("music", shared)]})

        with self.assertRaisesRegex(ValueError, "shared folder"):
            portable.write_export(data, os.path.join(shared, "export.json"))

    def test_folder_with_a_similar_name_is_not_the_shared_folder(self):

        shared = os.path.join(self.folder, "shared")
        data = portable.build_export(self.source, {"shared": [("music", shared)]})
        path = os.path.join(self.target, "shared-export.json")

        portable.write_export(data, path)

        self.assertTrue(os.path.exists(path))

    def test_refuses_a_drive_that_is_gone(self):

        data = portable.build_export(self.source, {})

        with self.assertRaisesRegex(ValueError, "is the drive connected"):
            portable.write_export(data, os.path.join(self.folder, "gone", "export.json"))

    def test_default_path_is_next_to_the_download_folder(self):

        self.assertEqual(portable.default_export_path("/Volumes/Drive/Music/downloads"),
                         "/Volumes/Drive/Music/" + portable.EXPORT_FILE_NAME)


class ImportTest(PortableTestCase):

    def export(self):

        self.fill_source()
        path = os.path.join(self.folder, "export.json")
        portable.write_export(portable.build_export(self.source, SETTINGS), path)
        return portable.read_export(path)

    def test_round_trip_keeps_states(self):

        data = self.export()
        summary = portable.merge_into(data, self.target)
        jobs = self.read(self.target, JOBS_FILE_NAME)["jobs"]

        self.assertEqual({job["job_id"]: job["status"] for job in jobs}, {"a": "running", "b": "paused"})
        self.assertEqual(summary["num_jobs_replaced"], 0)
        self.assertEqual(self.read(self.target, portable.TRANSFERS_FILE_NAME)[0][3], "Paused")

    def test_imported_jobs_load_in_the_job_manager_format(self):

        job = {"job_id": "a", "name": "BITE", "label_id": None, "mode": "download", "status": "paused",
               "pause_mode": "now", "transfers_paused": True,
               "items": [{"release": {"title": "Altars"}, "state": "downloading"}]}
        self.write(self.source, JOBS_FILE_NAME, {"jobs": [job]})
        data = portable.build_export(self.source, {})
        portable.merge_into(data, self.target)

        stored = self.read(self.target, JOBS_FILE_NAME)["jobs"][0]
        items = [JobItem(**item) for item in stored.pop("items")]

        self.assertTrue(Job(**stored, items=items).transfers_paused)

    def test_what_is_here_stays_and_the_export_wins_on_conflicts(self):

        self.write(self.target, JOBS_FILE_NAME, {"jobs": [make_job("mine"), make_job("a", "finished")]})
        self.write(self.target, SPEEDS_FILE_NAME, {"alice": [9, 99.0], "bob": [5, 1.0]})
        self.write(self.target, portable.TRANSFERS_FILE_NAME, [["bob", "Music\\2.flac", "/x", "Finished", 1, 1, {}]])

        summary = portable.merge_into(self.export(), self.target)
        jobs = {job["job_id"]: job["status"] for job in self.read(self.target, JOBS_FILE_NAME)["jobs"]}
        speeds = self.read(self.target, SPEEDS_FILE_NAME)

        self.assertEqual(jobs, {"mine": "running", "a": "running", "b": "paused"})
        self.assertEqual(summary["num_jobs_replaced"], 1)
        self.assertEqual(speeds, {"alice": [9, 99.0], "bob": [5, 1.0]})
        self.assertEqual(len(self.read(self.target, portable.TRANSFERS_FILE_NAME)), 2)

    def test_map_paths_moves_folders_but_not_uploaders_paths(self):

        data = self.export()

        portable.map_paths(data, [("/Volumes/Drive", "/Volumes/Music")])
        job = data["jobs"][0]

        self.assertEqual(job["download_folder"], "/Volumes/Music/Music/downloads/BITE")
        self.assertEqual(job["items"][0]["local_path"], "/Volumes/Music/Music/downloads/BITE/Altars")
        self.assertEqual(data["transfers"][0][2], "/Volumes/Music/Music/downloads/BITE/Altars")
        self.assertEqual(data["transfers"][0][1], "Music\\1.flac")
        self.assertEqual(data["settings"]["downloaddir"], "/Volumes/Music/Music/downloads")
        self.assertEqual(data["settings"]["shared"][0][1], "/Volumes/Music/Music/downloads")

    def test_map_paths_needs_a_whole_folder_name(self):

        data = {"jobs": [make_job("a", folder="/Volumes/Drive2/x")], "transfers": [], "settings": {}}

        portable.map_paths(data, [("/Volumes/Drive", "/Volumes/Music")])

        self.assertEqual(data["jobs"][0]["download_folder"], "/Volumes/Drive2/x")

    def test_drive_that_is_not_connected_is_found(self):

        data = self.export()

        self.assertIn("/Volumes/Drive/Music/downloads", portable.find_unusable_folders(data))

    def test_folders_that_exist_are_fine(self):

        here = os.path.join(self.target, "downloads")
        data = {"jobs": [make_job("a", folder=os.path.join(here, "new"))], "settings": {"downloaddir": here}}

        self.assertEqual(portable.find_unusable_folders(data), [])

    def test_not_an_export_is_refused(self):

        path = os.path.join(self.folder, "other.json")
        self.write(self.folder, "other.json", {"something": "else"})

        with self.assertRaisesRegex(ValueError, "not an export"):
            portable.read_export(path)

    def test_newer_version_is_refused(self):

        path = os.path.join(self.folder, "new.json")
        self.write(self.folder, "new.json", {"version": 99, "jobs": [], "settings": {}})

        with self.assertRaisesRegex(ValueError, "version 99"):
            portable.read_export(path)

    def test_back_up_keeps_what_was_there(self):

        self.write(self.target, JOBS_FILE_NAME, {"jobs": [make_job("mine")]})
        config_path = os.path.join(self.folder, "config")

        with open(config_path, "w", encoding="utf-8") as file_handle:
            file_handle.write("[server]\n")

        backup_folder = portable.back_up(self.target, config_path, clock=lambda: 0)

        self.assertEqual(sorted(os.listdir(backup_folder)), sorted([JOBS_FILE_NAME, "config"]))

    def test_shares_get_the_form_the_configuration_has(self):

        data = {"settings": {"shared": [["downloads", "/x"]], "downloaddir": "/x"}}

        self.assertEqual(portable.get_import_settings(data)["shared"], [("downloads", "/x")])

    def test_path_mapping_needs_both_sides(self):

        self.assertEqual(portable.parse_path_mapping("/a/=/b"), ("/a", "/b"))

        with self.assertRaises(ValueError):
            portable.parse_path_mapping("/a")


class ServerInfoTest(PortableTestCase):

    def test_own_process_counts_as_running(self):

        portable.write_server_info(self.target, "http://127.0.0.1:8585/")

        self.assertEqual(portable.get_running_server(self.target)["dashboard_url"], "http://127.0.0.1:8585/")

        portable.remove_server_info(self.target)

        self.assertIsNone(portable.get_running_server(self.target))

    def test_process_that_is_gone_does_not_count(self):

        self.write(self.target, portable.SERVER_FILE_NAME, {"pid": 99999999, "dashboard_url": None})

        self.assertIsNone(portable.get_running_server(self.target))

    def test_broken_file_does_not_count(self):

        with open(os.path.join(self.target, portable.SERVER_FILE_NAME), "w", encoding="utf-8") as file_handle:
            file_handle.write("{")

        self.assertIsNone(portable.get_running_server(self.target))


class ParserTest(TestCase):

    def test_commands_are_optional(self):

        self.assertIsNone(build_parser().parse_args([]).command)
        self.assertIsNone(build_parser().parse_args(["--http"]).command)

    def test_commands(self):

        self.assertEqual(build_parser().parse_args(["export"]).command, "export")

        args = build_parser().parse_args(["import", "file.json", "--map-path", "/a=/b", "--keep-settings"])

        self.assertEqual((args.command, args.file, args.map_path, args.keep_settings),
                         ("import", "file.json", ["/a=/b"], True))
