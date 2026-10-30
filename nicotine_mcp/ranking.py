# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Scoring and ranking of Soulseek search results.

This module is intentionally free of any dependency on the Nicotine+
core, so the ranking logic can be unit tested in isolation and reused
outside of the MCP server.

A file candidate is scored on three axes:

- availability: whether the uploader has a free upload slot, and how long
  their queue is otherwise ("no queue" is what an agent usually wants)
- speed: how fast the uploader was when measured. The speed uploaders report
  doesn't predict what they deliver, so it counts for little
- quality: lossless beats lossy, higher bitrate beats lower bitrate

The weights of the axes depend on the ``prefer`` preference. Hard filters
(allowed formats, minimum bitrate, free slot required, maximum queue
length) are applied before scoring.
"""

from __future__ import annotations

import math
import os
import re

from collections import Counter
from collections import defaultdict
from collections import namedtuple
from dataclasses import dataclass
from dataclasses import field

LOSSLESS_EXTENSIONS = frozenset({
    "flac", "wav", "aiff", "aif", "ape", "wv", "alac", "tta", "shn", "dsf", "dff", "tak"
})
LOSSY_EXTENSIONS = frozenset({
    "mp3", "m4a", "aac", "ogg", "oga", "opus", "wma", "mpc", "mp2", "ac3", "m4b"
})
AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | LOSSY_EXTENSIONS
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "webp"})
ALBUM_EXTRA_EXTENSIONS = IMAGE_EXTENSIONS | frozenset({"cue", "log", "nfo", "m3u", "m3u8", "txt", "md5", "sfv"})

DEFAULT_FORMATS = ("flac", "mp3")
PREFER_CHOICES = ("balanced", "quality", "availability")

# Words in a file/folder name that usually indicate a non-canonical version of a track or album.
# They are only penalized when the query itself does not ask for them.
VARIANT_WORDS = frozenset({
    "remix", "remixes", "remixed", "mix", "mixes", "live", "edit", "edits", "instrumental", "instrumentals",
    "karaoke", "acoustic", "demo", "demos", "cover", "covers", "bootleg", "bootlegs", "mashup", "dub",
    "extended", "rework", "reworks", "vs", "tribute", "megamix"
})

# Weights for each 'prefer' mode
Weights = namedtuple("Weights", ["availability", "speed", "quality"])
_PREFER_WEIGHTS = {
    "balanced": Weights(availability=1.0, speed=1.0, quality=1.0),
    "quality": Weights(availability=0.6, speed=0.6, quality=1.6),
    "availability": Weights(availability=1.5, speed=1.3, quality=0.6)
}

# Maximum points per axis
_AVAILABILITY_POINTS = 30.0
_SPEED_POINTS = 20.0
_QUALITY_POINTS = 35.0
_LOSSLESS_POINTS = 32.0
_HIRES_POINTS = 3.0
_LOSSY_MAX_POINTS = 28.0
_LOSSY_REFERENCE_BITRATE = 320
_UNKNOWN_BITRATE_POINTS = 9.0
_FORMAT_PREFERENCE_POINTS = 4.0
_DUPLICATE_POINTS_MAX = 5.0
_SUSPICIOUS_SIZE_PENALTY = 15.0
_SUSPICIOUS_SIZE_BYTES = 300_000
_FOLDER_COMPLETENESS_POINTS = 10.0
_FOLDER_CONSISTENCY_POINTS = 3.0
_RELEVANCE_POINTS = 10.0
_VARIANT_PENALTY = 8.0
_DEFAULT_MAX_PER_USER = 3

_TOKEN_SPLIT_REGEX = re.compile(r"[^0-9a-z]+")

# Measured speed range used for the logarithmic speed score (bytes/second)
_SPEED_FLOOR = 50_000
_SPEED_CEILING = 5_000_000

# A reported speed only tells that the uploader has uploaded something recently. This is as
# many points as somebody measured at about 150 KB/s gets, so that somebody unknown is
# preferred over somebody known to be slow, and somebody known to be fast over somebody unknown.
_REPORTED_SPEED_POINTS = 5.0
_REPORTED_SPEED_FLOOR = 10_000
_REPORTED_SPEED_ENOUGH = 100_000


def get_extension(path: str) -> str:
    """Returns the lowercase file extension of a (virtual) path without the dot."""

    basename = path.rpartition("\\")[-1].rpartition("/")[-1]
    return os.path.splitext(basename)[1][1:].lower()


def tokenize(text: str) -> list:
    """Splits text into lowercase alphanumeric words."""
    return [word for word in _TOKEN_SPLIT_REGEX.split(text.lower()) if word]


def is_audio_extension(extension: str) -> bool:
    return extension in AUDIO_EXTENSIONS


def is_lossless_extension(extension: str) -> bool:
    return extension in LOSSLESS_EXTENSIONS


def normalize_formats(formats) -> tuple[str, ...]:
    """Normalizes a user-provided list of formats (extensions).

    An empty list means "any audio format". Formats are lowercased and
    stripped of leading dots. Duplicates are removed, order is preserved.
    """

    if not formats:
        return ()

    if isinstance(formats, str):
        formats = formats.replace(",", " ").split()

    normalized = []

    for item in formats:
        item = str(item).strip().lower().lstrip(".")

        if item == "any":
            return ()

        if item == "lossless":
            for extension in sorted(LOSSLESS_EXTENSIONS):
                if extension not in normalized:
                    normalized.append(extension)
            continue

        if item == "lossy":
            for extension in sorted(LOSSY_EXTENSIONS):
                if extension not in normalized:
                    normalized.append(extension)
            continue

        if item and item not in normalized:
            normalized.append(item)

    return tuple(normalized)


@dataclass
class RankingPreferences:
    """User/agent preferences for filtering and ranking results."""

    # Allowed file formats (extensions), ordered by preference. Empty means any audio format.
    formats: tuple[str, ...] = DEFAULT_FORMATS

    # One of PREFER_CHOICES
    prefer: str = "balanced"

    # Minimum bitrate in kbps for lossy files (lossless files always pass)
    min_bitrate: int = 0

    # Only accept uploaders with a free upload slot
    require_free_slot: bool = False

    # Only accept uploaders with at most this many files in their upload queue
    max_queue: int | None = None

    # Minimum/maximum track length in seconds, if known (0 disables)
    min_length: int = 0
    max_length: int = 0

    # Words of the search query, used to score how relevant a file name is
    query_words: tuple[str, ...] = ()

    # Maximum number of results per uploader in ranked lists (0 = unlimited)
    max_per_user: int = _DEFAULT_MAX_PER_USER

    def __post_init__(self):

        self.formats = normalize_formats(self.formats)
        self.query_words = tuple(word.lower() for word in (self.query_words or ()) if word)
        self.max_per_user = max(0, int(self.max_per_user or 0))

        if self.prefer not in _PREFER_WEIGHTS:
            raise ValueError(f"prefer must be one of {', '.join(PREFER_CHOICES)}")

        self.min_bitrate = max(0, int(self.min_bitrate or 0))
        self.min_length = max(0, int(self.min_length or 0))
        self.max_length = max(0, int(self.max_length or 0))

        if self.max_queue is not None:
            self.max_queue = max(0, int(self.max_queue))

    @property
    def weights(self):
        return _PREFER_WEIGHTS[self.prefer]

    def allows_extension(self, extension: str) -> bool:

        if not self.formats:
            return is_audio_extension(extension)

        return extension in self.formats

    def format_preference_points(self, extension: str) -> float:
        """Bonus for formats listed first in the preference list."""

        num_formats = len(self.formats)

        if num_formats < 2 or extension not in self.formats:
            return 0.0

        index = self.formats.index(extension)
        return _FORMAT_PREFERENCE_POINTS * (num_formats - 1 - index) / (num_formats - 1)


@dataclass
class FileCandidate:
    """A single file from a search result, with uploader information."""

    username: str
    virtual_path: str
    size: int = 0
    bitrate: int = 0
    length: int = 0
    vbr: bool = False
    sample_rate: int | None = None
    bit_depth: int | None = None
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0
    measured_speed: int = 0
    country_code: str = ""
    is_private: bool = False

    # Opaque reference to the original file attributes object (not serialized)
    file_attributes: object = field(default=None, repr=False, compare=False)

    # Filled in by rank_files()
    score: float = field(default=0.0, compare=False)
    score_details: dict = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        return self.username + self.virtual_path

    @property
    def basename(self) -> str:
        return self.virtual_path.rpartition("\\")[-1]

    @property
    def folder_path(self) -> str:
        return self.virtual_path.rpartition("\\")[0]

    @property
    def extension(self) -> str:
        return get_extension(self.virtual_path)

    @property
    def is_lossless(self) -> bool:
        return is_lossless_extension(self.extension)

    @property
    def is_audio(self) -> bool:
        return is_audio_extension(self.extension)

    @property
    def is_hires(self) -> bool:
        return bool((self.bit_depth or 0) > 16 or (self.sample_rate or 0) > 48000)

    @property
    def effective_bitrate(self) -> int:
        """Bitrate in kbps, derived from sample rate and bit depth if needed."""

        if self.bitrate > 0:
            return self.bitrate

        if self.sample_rate and self.bit_depth:
            return (self.sample_rate * self.bit_depth * 2) // 1000

        return 0

    @property
    def quality_label(self) -> str:

        if self.is_lossless:
            if self.sample_rate and self.bit_depth:
                return f"{self.bit_depth} bit / {self.sample_rate / 1000:.3g} kHz"

            return "lossless"

        bitrate = self.effective_bitrate

        if bitrate <= 0:
            return "unknown bitrate"

        return f"{bitrate} kbps{' (vbr)' if self.vbr else ''}"


@dataclass
class FolderCandidate:
    """A folder (album) shared by a user, built from matching file candidates."""

    username: str
    folder_path: str
    files: list = field(default_factory=list)

    # Filled in by rank_folders()
    score: float = field(default=0.0, compare=False)
    score_details: dict = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        return self.username + self.folder_path

    @property
    def folder_name(self) -> str:
        return self.folder_path.rpartition("\\")[-1]

    @property
    def audio_files(self) -> list:
        return [candidate for candidate in self.files if candidate.is_audio]

    @property
    def has_free_slot(self) -> bool:
        return any(candidate.has_free_slot for candidate in self.files)

    @property
    def queue_length(self) -> int:
        return min((candidate.queue_length for candidate in self.files), default=0)

    @property
    def upload_speed(self) -> int:
        return max((candidate.upload_speed for candidate in self.files), default=0)

    @property
    def measured_speed(self) -> int:
        return max((candidate.measured_speed for candidate in self.files), default=0)

    @property
    def country_code(self) -> str:
        return next((candidate.country_code for candidate in self.files if candidate.country_code), "")

    @property
    def total_size(self) -> int:
        return sum(candidate.size for candidate in self.files)

    @property
    def formats(self) -> list:
        return sorted({candidate.extension for candidate in self.audio_files})


# Filters #


def is_file_wanted(basename: str, formats: list | None) -> bool:
    """Says if a file of a folder should be used, given the formats asked for. A folder can
    hold an album in both FLAC and MP3. Other files, such as cover art, are not affected."""

    allowed_formats = normalize_formats(formats)
    extension = get_extension(basename)

    return not allowed_formats or not is_audio_extension(extension) or extension in allowed_formats


def check_file_filters(candidate: FileCandidate, prefs: RankingPreferences) -> str | None:
    """Returns None if the candidate passes all hard filters, otherwise a
    reason describing why it was rejected."""

    extension = candidate.extension

    if not prefs.allows_extension(extension):
        return f"format '{extension or 'none'}' not allowed"

    if prefs.require_free_slot and not candidate.has_free_slot:
        return "uploader has no free slot"

    if prefs.max_queue is not None and not candidate.has_free_slot and candidate.queue_length > prefs.max_queue:
        return f"upload queue too long ({candidate.queue_length})"

    if prefs.min_bitrate > 0 and not candidate.is_lossless:
        bitrate = candidate.effective_bitrate

        if 0 < bitrate < prefs.min_bitrate:
            return f"bitrate {bitrate} kbps below minimum {prefs.min_bitrate} kbps"

    if candidate.length > 0:
        if prefs.min_length > 0 and candidate.length < prefs.min_length:
            return f"track too short ({candidate.length} s)"

        if 0 < prefs.max_length < candidate.length:
            return f"track too long ({candidate.length} s)"

    return None


# Scoring #


def availability_points(has_free_slot: bool, queue_length: int) -> float:

    if has_free_slot:
        return _AVAILABILITY_POINTS

    return _AVAILABILITY_POINTS / (1 + max(0, queue_length) / 3)


def speed_points(upload_speed: int, measured_speed: int = 0) -> float:

    if measured_speed > 0:
        fraction = math.log10(measured_speed / _SPEED_FLOOR) / math.log10(_SPEED_CEILING / _SPEED_FLOOR)
        return _SPEED_POINTS * min(1.0, max(0.0, fraction))

    if upload_speed <= 0:
        return 0.0

    fraction = math.log10(max(upload_speed, 1) / _REPORTED_SPEED_FLOOR) / math.log10(
        _REPORTED_SPEED_ENOUGH / _REPORTED_SPEED_FLOOR
    )
    return _REPORTED_SPEED_POINTS * min(1.0, max(0.0, fraction))


def quality_points(candidate: FileCandidate) -> float:

    if candidate.is_lossless:
        points = _LOSSLESS_POINTS

        if candidate.is_hires:
            points += _HIRES_POINTS

    else:
        bitrate = candidate.effective_bitrate

        if bitrate <= 0:
            points = _UNKNOWN_BITRATE_POINTS
        else:
            points = _LOSSY_MAX_POINTS * min(bitrate, _LOSSY_REFERENCE_BITRATE) / _LOSSY_REFERENCE_BITRATE

    return min(_QUALITY_POINTS, points)


def relevance_points(name: str, prefs: RankingPreferences) -> tuple[float, float]:
    """Scores how well a file or folder name matches the query.

    Returns (relevance, variant_penalty). Relevance grows with the share of
    query words found in the name itself (not just in parent folders). The
    penalty applies when the name contains variant markers such as "remix"
    or "live" that the query did not ask for.
    """

    if not prefs.query_words:
        return 0.0, 0.0

    name_lower = name.lower()
    tokens = set(tokenize(name_lower))
    matched = sum(1 for word in prefs.query_words if word in name_lower)
    relevance = _RELEVANCE_POINTS * matched / len(prefs.query_words)
    penalty = 0.0

    if any(token in VARIANT_WORDS and token not in prefs.query_words for token in tokens):
        penalty = _VARIANT_PENALTY

    return relevance, penalty


def _limit_per_user(items, prefs: RankingPreferences, limit: int | None) -> list:
    """Applies the per-user cap and the overall limit to a sorted list."""

    if prefs.max_per_user > 0:
        per_user = defaultdict(int)
        limited = []

        for item in items:
            per_user[item.username] += 1

            if per_user[item.username] <= prefs.max_per_user:
                limited.append(item)

        items = limited

    if limit is not None:
        del items[max(0, int(limit)):]

    return items


def score_file(candidate: FileCandidate, prefs: RankingPreferences, duplicates: int = 0) -> tuple[float, dict]:
    """Scores a single file candidate. Returns (score, details)."""

    availability_weight, speed_weight, quality_weight = prefs.weights

    availability = availability_points(candidate.has_free_slot, candidate.queue_length)
    speed = speed_points(candidate.upload_speed, candidate.measured_speed)
    quality = quality_points(candidate)
    format_preference = prefs.format_preference_points(candidate.extension)
    popularity = min(_DUPLICATE_POINTS_MAX, float(duplicates))
    relevance, penalty = relevance_points(os.path.splitext(candidate.basename)[0], prefs)

    if candidate.is_audio and 0 < candidate.size < _SUSPICIOUS_SIZE_BYTES:
        penalty += _SUSPICIOUS_SIZE_PENALTY

    score = (
        availability * availability_weight
        + speed * speed_weight
        + (quality + format_preference) * quality_weight
        + relevance
        + popularity
        - penalty
    )

    details = {
        "availability": round(availability, 1),
        "speed": round(speed, 1),
        "quality": round(quality, 1),
        "format_preference": round(format_preference, 1),
        "relevance": round(relevance, 1),
        "popularity": round(popularity, 1),
        "penalty": round(penalty, 1)
    }

    return round(score, 2), details


def _count_duplicates(candidates) -> dict:
    """Counts how many distinct users share a file with the same name and size.

    A file shared by many users is more likely to be a correct, complete
    copy, so this is used as a small tie-breaker.
    """

    users_per_file = defaultdict(set)

    for candidate in candidates:
        users_per_file[(candidate.basename.lower(), candidate.size)].add(candidate.username)

    return {
        file_key: len(users) - 1
        for file_key, users in users_per_file.items()
    }


def rank_files(candidates, prefs: RankingPreferences | None = None, limit: int | None = None) -> list:
    """Filters and ranks file candidates, best first.

    Sets the ``score`` and ``score_details`` attributes of every returned
    candidate.
    """

    if prefs is None:
        prefs = RankingPreferences()

    candidates = list(candidates)
    duplicates = _count_duplicates(candidates)
    accepted = []

    for candidate in candidates:
        if check_file_filters(candidate, prefs) is not None:
            continue

        num_duplicates = duplicates.get((candidate.basename.lower(), candidate.size), 0)
        candidate.score, candidate.score_details = score_file(candidate, prefs, duplicates=num_duplicates)
        accepted.append(candidate)

    # Stable sort: best score first, then faster uploader, then smaller queue, then larger file
    accepted.sort(
        key=lambda item: (
            -item.score, -item.measured_speed, -item.upload_speed, item.queue_length, -item.size, item.key
        )
    )

    return _limit_per_user(accepted, prefs, limit)


def group_folders(candidates) -> list:
    """Groups file candidates into folder candidates, one per user and folder."""

    folders = {}

    for candidate in candidates:
        folder_key = candidate.username + candidate.folder_path

        if folder_key not in folders:
            folders[folder_key] = FolderCandidate(candidate.username, candidate.folder_path)

        folders[folder_key].files.append(candidate)

    return list(folders.values())


def score_folder(folder: FolderCandidate, prefs: RankingPreferences, duplicates: int = 0) -> tuple[float, dict]:
    """Scores a folder (album) candidate. Returns (score, details).

    Only audio files passing the hard filters contribute to the quality
    score. Folders without any acceptable audio file score 0.
    """

    availability_weight, speed_weight, quality_weight = prefs.weights
    accepted_files = [
        candidate for candidate in folder.audio_files if check_file_filters(candidate, prefs) is None
    ]

    if not accepted_files:
        return 0.0, {"rejected": "no audio files matching the requested formats/filters"}

    availability = availability_points(folder.has_free_slot, folder.queue_length)
    speed = speed_points(folder.upload_speed, folder.measured_speed)
    quality = sum(quality_points(candidate) for candidate in accepted_files) / len(accepted_files)
    format_preference = sum(
        prefs.format_preference_points(candidate.extension) for candidate in accepted_files
    ) / len(accepted_files)
    completeness = min(_FOLDER_COMPLETENESS_POINTS, float(len(accepted_files)))
    consistency = _FOLDER_CONSISTENCY_POINTS if len({c.extension for c in accepted_files}) == 1 else 0.0
    popularity = min(_DUPLICATE_POINTS_MAX, float(duplicates))

    # Match the query against the folder name and its parent (typically album and artist)
    folder_parts = folder.folder_path.split("\\")
    relevance, penalty = relevance_points(" ".join(folder_parts[-2:]), prefs)

    score = (
        availability * availability_weight
        + speed * speed_weight
        + (quality + format_preference) * quality_weight
        + completeness
        + consistency
        + relevance
        + popularity
        - penalty
    )

    details = {
        "availability": round(availability, 1),
        "speed": round(speed, 1),
        "quality": round(quality, 1),
        "format_preference": round(format_preference, 1),
        "completeness": round(completeness, 1),
        "consistency": round(consistency, 1),
        "relevance": round(relevance, 1),
        "popularity": round(popularity, 1),
        "penalty": round(penalty, 1),
        "num_matching_files": len(accepted_files)
    }

    return round(score, 2), details


def rank_folders(candidates, prefs: RankingPreferences | None = None, limit: int | None = None) -> list:
    """Groups file candidates by folder and ranks the folders, best first."""

    if prefs is None:
        prefs = RankingPreferences()

    folders = group_folders(candidates)

    # Count how many other users share a folder with the same name
    users_per_folder_name = defaultdict(set)

    for folder in folders:
        users_per_folder_name[folder.folder_name.lower()].add(folder.username)

    accepted = []

    for folder in folders:
        num_duplicates = len(users_per_folder_name[folder.folder_name.lower()]) - 1
        folder.score, folder.score_details = score_folder(folder, prefs, duplicates=num_duplicates)

        if folder.score <= 0:
            continue

        accepted.append(folder)

    accepted.sort(
        key=lambda item: (
            -item.score, -item.measured_speed, -item.upload_speed, item.queue_length, -len(item.files), item.key
        )
    )

    return _limit_per_user(accepted, prefs, limit)


def summarize_formats(candidates) -> dict:
    """Returns a count of audio formats among candidates, for quick overviews."""

    counter = Counter(candidate.extension for candidate in candidates if candidate.is_audio)
    return dict(counter.most_common())
