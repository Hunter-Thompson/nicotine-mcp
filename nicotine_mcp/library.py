# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finds out which releases are already on disk, so they are not downloaded twice.

Only file names are looked at, the same way shared folders are compared with
a release in ``matching.py``. Nothing here depends on the Nicotine+ core.
"""

from __future__ import annotations

import os
import time

from threading import Lock

from nicotine_mcp.matching import Verdict
from nicotine_mcp.matching import catalog_number_in_text
from nicotine_mcp.matching import match_folder
from nicotine_mcp.matching import significant_tokens
from nicotine_mcp.matching import text_tokens
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import is_audio_extension
from nicotine_mcp.ranking import is_file_wanted


class LibraryIndex:
    """An index of the folders with audio files below a set of root folders."""

    MAX_AGE = 600

    def __init__(self, folders=(), clock=time.monotonic):

        self.folders = [os.path.normpath(os.path.expandvars(folder)) for folder in folders if folder]
        self.unavailable_folders = []
        self._clock = clock
        self._lock = Lock()
        self._entries = None
        self._scanned_at = None

    def _scan(self):
        """Returns [(path, tokens of its last path segments, basenames)] of folders with audio files."""

        entries = []
        unavailable = []
        seen_paths = set()

        for root_folder in self.folders:
            if not os.path.isdir(root_folder):
                unavailable.append(root_folder)
                continue

            for folder_path, _folders, basenames in os.walk(root_folder):
                if folder_path in seen_paths:
                    continue

                seen_paths.add(folder_path)
                # Hidden files aren't music: macOS adds a '._' file for each file on exFAT drives
                audio_files = [name for name in basenames
                               if is_audio_extension(get_extension(name)) and not name.startswith(".")]

                if audio_files:
                    segments = folder_path.split(os.sep)[-3:]
                    entries.append((folder_path, set(text_tokens(" ".join(segments))), audio_files))

        self.unavailable_folders = unavailable
        return entries

    def _get_entries(self):

        with self._lock:
            now = self._clock()

            if self._entries is None or now - self._scanned_at > self.MAX_AGE:
                self._entries = self._scan()
                self._scanned_at = now

            return self._entries

    def invalidate(self):

        with self._lock:
            self._entries = None

    def find_release(self, release, formats=None):
        """Returns the local path of a release, or None. Blocks while the library is scanned.
        With ``formats``, only copies in those formats count: an MP3 doesn't stand in the way of a FLAC."""

        title_tokens = significant_tokens(release.title)

        if not title_tokens:
            return None

        for folder_path, path_tokens, basenames in self._get_entries():
            has_catalog_number = any(
                catalog_number_in_text(number, " ".join(sorted(path_tokens))) for number in release.catalog_numbers
            )

            if not title_tokens <= path_tokens and not has_catalog_number:
                continue

            match = match_folder(
                release, folder_path, [(name, 0) for name in basenames if is_file_wanted(name, formats)]
            )

            if match.verdict in Verdict.ACCEPTABLE:
                return folder_path

        return None
