# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Moves jobs and downloads to another user or Mac that uses the same drive.

An export is one JSON file. Music and partial files stay on the drive, and carry on
from where they were, since the name of a partial file only depends on the uploader.

Accounts are personal, so only the settings listed in ``SHARED_SETTINGS`` are exported.
Nothing else from the Nicotine+ configuration ends up in the file, like the login.
"""

from __future__ import annotations

import json
import os
import shutil
import time

from nicotine_mcp.job_models import is_destination_available
from nicotine_mcp.jobs import JOBS_FILE_NAME
from nicotine_mcp.speed import SPEEDS_FILE_NAME

EXPORT_VERSION = 1
EXPORT_FILE_NAME = "nicotine-mcp-export.json"
TRANSFERS_FILE_NAME = "downloads.json"
SERVER_FILE_NAME = "nicotine_mcp_server.json"
SHARED_SETTINGS = (
    "downloaddir", "incompletedir", "shared", "buddyshared", "use_download_speed_limit", "downloadlimit",
    "use_upload_speed_limit", "uploadlimit"
)
_SHARE_SETTINGS = ("shared", "buddyshared")

# Rows of the download list: [username, virtual path, folder, status, size, offset, attributes]
_TRANSFER_FOLDER_COLUMN = 2


def _read_json(path, default):

    try:
        with open(path, encoding="utf-8") as file_handle:
            return json.load(file_handle)

    except FileNotFoundError:
        return default


def _write_json(path, data):

    temporary_path = path + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as file_handle:
        json.dump(data, file_handle, ensure_ascii=False)

    os.replace(temporary_path, path)


def _is_inside(path, folder):

    path = os.path.abspath(path)
    folder = os.path.abspath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


# Export #


def default_export_path(download_folder):
    """Next to the download folder, which puts it on the drive and usually outside of what is shared."""
    return os.path.join(os.path.dirname(os.path.abspath(download_folder)), EXPORT_FILE_NAME)


def build_export(data_folder, transfer_settings, clock=time.time):
    """Builds an export from the state files in a data folder. ``transfer_settings``
    is the 'transfers' section of the Nicotine+ configuration."""

    return {
        "version": EXPORT_VERSION,
        "exported_at": clock(),
        "jobs": _read_json(os.path.join(data_folder, JOBS_FILE_NAME), {}).get("jobs", []),
        "speeds": _read_json(os.path.join(data_folder, SPEEDS_FILE_NAME), {}),
        "transfers": _read_json(os.path.join(data_folder, TRANSFERS_FILE_NAME), []),
        "settings": {name: transfer_settings[name] for name in SHARED_SETTINGS if name in transfer_settings}
    }


def write_export(data, path):
    """Writes an export. Other users can see what is in a shared folder, so that's not allowed."""

    settings = data["settings"]

    for option in _SHARE_SETTINGS:
        for share in settings.get(option) or []:
            if _is_inside(path, share[1]):
                raise ValueError(f"{path} is inside the shared folder {share[1]}, where anybody can download it. "
                                 f"Choose another place.")

    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        raise ValueError(f"The folder of {path} doesn't exist, is the drive connected?")

    _write_json(path, data)


def summarize(data):

    statuses = {}

    for job in data["jobs"]:
        statuses[job["status"]] = statuses.get(job["status"], 0) + 1

    return {"num_jobs": len(data["jobs"]), "jobs_by_status": statuses, "num_transfers": len(data["transfers"]),
            "num_uploader_speeds": len(data["speeds"])}


# Import #


def read_export(path):

    with open(path, encoding="utf-8") as file_handle:
        data = json.load(file_handle)

    if not isinstance(data, dict) or "jobs" not in data or "settings" not in data:
        raise ValueError(f"{path} is not an export of nicotine-mcp")

    if data.get("version") != EXPORT_VERSION:
        raise ValueError(f"{path} has version {data.get('version')}, this version of nicotine-mcp reads "
                         f"version {EXPORT_VERSION}")

    return data


def parse_path_mapping(text):

    old_path, separator, new_path = text.partition("=")

    if not separator or not old_path or not new_path:
        raise ValueError(f"Expected OLD=NEW, got '{text}'")

    return old_path.rstrip(os.sep), new_path.rstrip(os.sep)


def _map_path(path, mappings):

    for old_path, new_path in mappings:
        if path and _is_inside(path, old_path):
            return new_path + os.path.abspath(path)[len(os.path.abspath(old_path)):]

    return path


def map_paths(data, mappings):
    """Changes where things are, for a drive that goes by another name. Uploaders' paths stay."""

    for job in data["jobs"]:
        job["download_folder"] = _map_path(job.get("download_folder"), mappings)

        for item in job.get("items", []):
            item["download_folder"] = _map_path(item.get("download_folder"), mappings)
            item["local_path"] = _map_path(item.get("local_path"), mappings)

    for row in data["transfers"]:
        row[_TRANSFER_FOLDER_COLUMN] = _map_path(row[_TRANSFER_FOLDER_COLUMN], mappings)

    settings = data["settings"]

    for name in ("downloaddir", "incompletedir"):
        if name in settings:
            settings[name] = _map_path(settings[name], mappings)

    for option in _SHARE_SETTINGS:
        if option in settings:
            settings[option] = [[share[0], _map_path(share[1], mappings), *share[2:]] for share in settings[option]]


def find_unusable_folders(data):
    """Folders that can't be used here: on a drive that isn't connected, or in another user's home folder."""

    folders = {job.get("download_folder") for job in data["jobs"]}
    folders.update(data["settings"].get(name) for name in ("downloaddir", "incompletedir"))
    unusable = []

    for folder in sorted(folder for folder in folders if folder):
        existing = os.path.abspath(folder)

        while not os.path.isdir(existing) and os.path.dirname(existing) != existing:
            existing = os.path.dirname(existing)

        if not is_destination_available(folder) or not os.access(existing, os.W_OK):
            unusable.append(folder)

    return unusable


def back_up(data_folder, config_path, clock=time.time):
    """Keeps what an import is about to change. Returns the folder it is in."""

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    backup_folder = os.path.join(data_folder, f"nicotine_mcp_backup_{stamp}")
    os.makedirs(backup_folder, exist_ok=True)

    for path in (os.path.join(data_folder, JOBS_FILE_NAME), os.path.join(data_folder, SPEEDS_FILE_NAME),
                 os.path.join(data_folder, TRANSFERS_FILE_NAME), config_path):
        if path and os.path.exists(path):
            shutil.copy2(path, backup_folder)

    return backup_folder


def merge_into(data, data_folder):
    """Adds an export to the state files in a data folder. What is here and not in
    the export stays. What is in both comes from the export, which is the newer copy."""

    jobs_path = os.path.join(data_folder, JOBS_FILE_NAME)
    jobs = {job["job_id"]: job for job in _read_json(jobs_path, {}).get("jobs", [])}
    num_replaced = sum(1 for job in data["jobs"] if job["job_id"] in jobs)
    jobs.update({job["job_id"]: job for job in data["jobs"]})
    _write_json(jobs_path, {"jobs": list(jobs.values())})

    speeds_path = os.path.join(data_folder, SPEEDS_FILE_NAME)
    speeds = _read_json(speeds_path, {})

    for username, entry in data["speeds"].items():
        if username not in speeds or entry[1] > speeds[username][1]:
            speeds[username] = entry

    _write_json(speeds_path, speeds)

    transfers_path = os.path.join(data_folder, TRANSFERS_FILE_NAME)
    transfers = {(row[0], row[1]): row for row in _read_json(transfers_path, [])}
    transfers.update({(row[0], row[1]): row for row in data["transfers"]})
    _write_json(transfers_path, list(transfers.values()))

    return {**summarize(data), "num_jobs_replaced": num_replaced}


def get_import_settings(data):
    """The exported settings in the form the Nicotine+ configuration has them in."""

    settings = dict(data["settings"])

    for option in _SHARE_SETTINGS:
        if option in settings:
            settings[option] = [tuple(share) for share in settings[option]]

    return settings


# The running server #


def write_server_info(data_folder, dashboard_url):

    _write_json(os.path.join(data_folder, SERVER_FILE_NAME), {"pid": os.getpid(), "dashboard_url": dashboard_url})


def remove_server_info(data_folder):

    try:
        os.remove(os.path.join(data_folder, SERVER_FILE_NAME))

    except OSError:
        pass


def get_running_server(data_folder):
    """Returns what a server running with this data folder wrote about itself, or None."""

    try:
        info = _read_json(os.path.join(data_folder, SERVER_FILE_NAME), None)

    except ValueError:
        return None

    if info is None:
        return None

    try:
        os.kill(info["pid"], 0)

    except ProcessLookupError:
        return None

    except PermissionError:
        pass

    return info


def export_running(runner, path=None):
    """Exports from a server that is running, after saving what it has in memory."""

    prepared = runner.call(runner.automation.prepare_export).result(30)
    runner.jobs.flush()

    data = build_export(prepared["data_folder"], prepared["transfer_settings"])
    path = path or default_export_path(prepared["download_folder"])
    write_export(data, path)

    return {
        "path": path,
        **summarize(data),
        "warning": "Stop this server before the other user starts theirs, or both will download the same albums."
    }
