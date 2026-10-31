# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Decides whether a shared folder really contains a given release.

Soulseek searches only look at file paths, so the ranking in ``ranking.py``
can tell which folder is the most available, but not whether it is the right
album. This module compares a folder against what a music catalog says the
release should contain: its tracklist, track lengths, catalog numbers, title
and artist.

This module is intentionally free of any dependency on the Nicotine+ core
and on the network, like ``ranking.py``.
"""

from __future__ import annotations

import os
import re
import unicodedata

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields
from difflib import SequenceMatcher

from nicotine_mcp.ranking import VARIANT_WORDS
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import is_audio_extension


class Verdict:
    """How well a folder matches a release, best first."""

    COMPLETE = "complete"  # Every expected track is there
    PROBABLE = "probable"  # No tracklist to compare with, but the folder is named after the release
    PARTIAL = "partial"  # Right release but tracks are missing, or buried among many other files
    MISMATCH = "mismatch"

    ACCEPTABLE = {COMPLETE, PROBABLE}
    ORDER = {COMPLETE: 0, PROBABLE: 1, PARTIAL: 2, MISMATCH: 3}


# Characters NFKD does not decompose into a base letter. Greek letters are commonly
# used for stylized Latin artist names, e.g. SΛRIN
_TRANSLATIONS = str.maketrans({
    "°": "o", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "ł": "l", "þ": "th", "ı": "i",
    "λ": "a", "σ": "s", "ς": "s"
})
_NON_WORD_REGEX = re.compile(r"[\W_]+")
_PARENTHESES_REGEX = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SEGMENT_REGEX = re.compile(r"\s+[-–—]\s+")
_TRACK_PREFIX_REGEX = re.compile(
    r"^\s*(?:\d{1,2}[-.])?(?P<position>[a-hA-H]?\d{1,3})(?:\s*[-._)\]]+\s*|\s+)(?=\S)"
)
_BARE_TRACK_NAME_REGEX = re.compile(r"^\s*(?:track|trk)[\s_-]*(?P<position>\d{1,3})\b", re.IGNORECASE)
_CATALOG_NUMBER_REGEX = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)$")
_DISC_FOLDER_REGEX = re.compile(r"^(?:cd|disc|disk|lp|vinyl|side)\s*\d*[a-z]?$")

_STOPWORDS = frozenset({"the", "a", "an", "and", "of", "feat", "ft", "featuring", "with", "vs"})
_VARIOUS_ARTISTS = frozenset({"", "various", "various artists", "va", "v a"})
_GENERIC_FOLDER_NAMES = frozenset({
    "music", "musik", "mp3", "mp3s", "flac", "audio", "songs", "tracks", "downloads", "download", "incoming",
    "complete", "completed", "shared", "share", "soulseek", "slsk", "misc", "miscellaneous", "random", "singles",
    "single", "various", "unsorted", "new", "new folder", "other", "others", "stuff", "library", "playlist",
    "playlists", "favorites", "favourites", "tmp", "temp", "unknown", "unknown album"
})

_TRACK_MATCH_THRESHOLD = 0.72
_LENGTH_CLOSE_SECONDS = 5
_LENGTH_FAR_SECONDS = 30
_LENGTH_FAR_RATIO = 0.15
_MAX_ALBUM_FILES = 60
_MAX_QUERIES = 3
_MAX_QUERY_WORDS = 8


def normalize_text(text: str) -> str:
    """Lowercases text and strips accents and punctuation, for comparing names."""

    text = unicodedata.normalize("NFKD", text or "").casefold().translate(_TRANSLATIONS)
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.replace("&", " and ")
    return " ".join(_NON_WORD_REGEX.sub(" ", text).split())


def text_tokens(text: str) -> list:
    return normalize_text(text).split()


def significant_tokens(text: str) -> set:
    """Tokens worth comparing. Text consisting of only stopwords keeps them, e.g. 'The The'."""

    tokens = set(text_tokens(text))
    return (tokens - _STOPWORDS) or tokens


def is_various_artists(artist: str) -> bool:
    return normalize_text(artist) in _VARIOUS_ARTISTS


@dataclass
class Track:
    """A track a release is expected to contain."""

    title: str
    length: int = 0  # Seconds, 0 when unknown
    position: str = ""


@dataclass
class Release:
    """A release as described by a music catalog."""

    release_id: str
    source: str
    artist: str
    title: str
    primary_artist: str = ""
    year: int | None = None
    kind: str = "other"  # album, ep, single, compilation, live, mix or other
    catalog_numbers: list = field(default_factory=list)
    formats: list = field(default_factory=list)
    track_count: int = 0
    label_ids: list = field(default_factory=list)
    labels: list = field(default_factory=list)  # Label names, when the catalog gave them
    listed_on_label: str | None = None  # The label ID whose listing this came from
    tracks: list | None = None  # None until the tracklist has been fetched

    def __post_init__(self):

        if not self.primary_artist:
            self.primary_artist = self.artist

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Release:

        known_fields = {item.name for item in fields(cls)}
        release = cls(**{key: value for key, value in data.items() if key in known_fields})

        if release.tracks is not None:
            release.tracks = [Track(**track) if isinstance(track, dict) else track for track in release.tracks]

        return release

    def summary(self) -> dict:
        """The fields an agent needs to recognize a release, without the tracklist."""

        data = self.to_dict()
        tracks = data.pop("tracks")
        data.pop("label_ids")
        data.pop("listed_on_label")
        data.pop("primary_artist")

        if tracks:
            data["track_count"] = len(tracks)

        return data


@dataclass
class FolderMatch:
    """The result of comparing a folder with a release."""

    verdict: str
    score: float
    num_expected: int
    num_matched: int
    num_audio_files: int
    missing_tracks: list = field(default_factory=list)
    title_in_path: bool = False
    artist_found: bool = False
    catalog_number_in_path: bool = False
    length_mismatches: int = 0
    reasons: list = field(default_factory=list)

    @property
    def is_acceptable(self) -> bool:
        return self.verdict in Verdict.ACCEPTABLE

    def to_dict(self) -> dict:
        return asdict(self)


# File names #


def split_file_name(basename: str) -> tuple[str, str, str]:
    """Returns (stem, stem without its track number, track number) of a file name."""

    stem = os.path.splitext(basename)[0]
    match = _TRACK_PREFIX_REGEX.match(stem) or _BARE_TRACK_NAME_REGEX.match(stem)

    if match is None:
        return stem, stem, ""

    return stem, stem[match.end():], match.group("position").lstrip("0").upper()


def title_similarity(expected_title: str, basename: str) -> float:
    """Returns how well a file name matches a track title, from 0 to 1."""

    expected_tokens = text_tokens(expected_title)

    if not expected_tokens:
        return 0.0

    stem, stripped_stem, _position = split_file_name(basename)
    file_tokens = set(text_tokens(stem))
    extra_tokens = file_tokens - set(expected_tokens)

    if set(expected_tokens) <= file_tokens:
        # Very short titles (e.g. "A" or "II") match almost any file name, they have to match exactly
        if len(expected_tokens) == 1 and len(expected_tokens[0]) <= 2:
            segments = _SEGMENT_REGEX.split(stripped_stem)
            return 0.95 if normalize_text(segments[-1]) == expected_tokens[0] else 0.0

        # "Track" should prefer "01 Track" over "02 Track (Someone Remix)"
        return 0.8 if extra_tokens & VARIANT_WORDS else 0.95

    # "Track (Original Mix)" is usually shared as just "Track"
    base_tokens = text_tokens(_PARENTHESES_REGEX.sub(" ", expected_title))

    if base_tokens and base_tokens != expected_tokens and set(base_tokens) <= file_tokens:
        return 0.78

    expected_text = " ".join(expected_tokens)
    candidates = [stripped_stem, *_SEGMENT_REGEX.split(stripped_stem)]

    return max(
        SequenceMatcher(None, expected_text, normalize_text(candidate)).ratio() for candidate in candidates
    )


def _pair_score(track: Track, index: int, basename: str, length: int) -> tuple[float, bool]:
    """Returns (score, whether the length is clearly wrong) for a track and a file."""

    score = title_similarity(track.title, basename)
    _stem, _stripped, position = split_file_name(basename)
    same_position = bool(position) and position in {str(index + 1), (track.position or "").lstrip("0").upper()}
    is_far = False

    if track.length and length:
        difference = abs(track.length - length)
        is_far = difference > _LENGTH_FAR_SECONDS and difference > track.length * _LENGTH_FAR_RATIO

        if is_far:
            score -= 0.3

        elif difference <= _LENGTH_CLOSE_SECONDS:
            score += 0.1

            # Nondescript names like "track01.flac" still count when number and length agree
            if same_position and difference <= 3:
                score = max(score, 0.75)

    if same_position:
        score += 0.02

    return score, is_far


def _assign_tracks(tracks: list, files: list) -> tuple[set, int]:
    """Pairs each expected track with its best file, each file used once.
    Returns (indexes of matched tracks, number of pairs with a wrong length)."""

    pairs = []

    for track_index, track in enumerate(tracks):
        for file_index, (basename, length) in enumerate(files):
            score, is_far = _pair_score(track, track_index, basename, length)

            if score >= _TRACK_MATCH_THRESHOLD:
                pairs.append((score, track_index, file_index, is_far))

    pairs.sort(key=lambda item: -item[0])
    matched_tracks = set()
    used_files = set()
    length_mismatches = 0

    for _score, track_index, file_index, is_far in pairs:
        if track_index in matched_tracks or file_index in used_files:
            continue

        matched_tracks.add(track_index)
        used_files.add(file_index)
        length_mismatches += int(is_far)

    return matched_tracks, length_mismatches


# Folders #


def catalog_number_in_text(catalog_number: str, text: str) -> bool:
    """Checks for a catalog number in a path, ignoring separators and leading
    zeros in its number, so OSTGUTCD46 matches 'Ostgut CD 46' and BITE028 'bite-28'."""

    compact = "".join(text_tokens(catalog_number))
    match = _CATALOG_NUMBER_REGEX.match(compact)

    # Only something like "ab12" can be told apart from an ordinary number in a folder name
    if match is None or len(compact) < 4 or not match.group("prefix"):
        return False

    haystack = "".join(text_tokens(text))
    pattern = re.escape(match.group("prefix")) + "0*" + str(int(match.group("number"))) + r"(?!\d)"
    return re.search(pattern, haystack) is not None


def _path_text(folder_path: str) -> str:
    """Album and artist are in the last few segments of a path, higher levels are arbitrary."""
    return " ".join(folder_path.replace("/", "\\").split("\\")[-3:])


def _audio_files(files: list) -> list:
    """Normalizes (basename, length) pairs, keeping only the audio files."""
    return [(basename, length or 0) for basename, length in files if is_audio_extension(get_extension(basename))]


def _artist_found(release: Release, path_text: str, audio_files: list) -> bool:

    if is_various_artists(release.primary_artist):
        return True

    artist_tokens = significant_tokens(release.primary_artist)
    tokens = set(text_tokens(path_text))

    if artist_tokens <= tokens:
        return True

    return any(artist_tokens <= set(text_tokens(basename)) for basename, _length in audio_files)


def match_folder(release: Release, folder_path: str, files: list) -> FolderMatch:
    """Compares a folder's files, a list of (basename, length in seconds) pairs,
    with a release."""

    audio_files = _audio_files(files)
    path_text = _path_text(folder_path)
    path_tokens = set(text_tokens(path_text))
    title_tokens = significant_tokens(release.title)

    title_in_path = bool(title_tokens) and title_tokens <= path_tokens
    artist_found = _artist_found(release, path_text, audio_files)
    catalog_number_in_path = any(catalog_number_in_text(number, path_text) for number in release.catalog_numbers)
    reasons = []

    for condition, reason in (
        (title_in_path, "title in folder name"),
        (artist_found, "artist found"),
        (catalog_number_in_path, "catalog number in folder name")
    ):
        if condition:
            reasons.append(reason)

    if not audio_files:
        return FolderMatch(
            Verdict.MISMATCH, 0.0, len(release.tracks or []) or release.track_count, 0, 0, title_in_path=title_in_path,
            artist_found=artist_found, catalog_number_in_path=catalog_number_in_path,
            reasons=[*reasons, "no audio files"]
        )

    identity_points = 15.0 * title_in_path + 5.0 * artist_found + 10.0 * catalog_number_in_path

    if not release.tracks:
        return _match_without_tracklist(
            release, audio_files, identity_points, title_in_path, artist_found, catalog_number_in_path, reasons
        )

    tracks = release.tracks
    matched_tracks, length_mismatches = _assign_tracks(tracks, audio_files)
    num_expected = len(tracks)
    num_matched = len(matched_tracks)
    num_extra = max(0, len(audio_files) - num_matched)
    coverage = num_matched / num_expected
    max_extra = max(2, num_expected // 2)
    has_too_many_files = num_extra > max_extra

    fit_points = 10.0 * max(0.0, 1.0 - num_extra / max(1, num_expected))
    score = coverage * 60.0 + fit_points + identity_points - min(15.0, 3.0 * length_mismatches)

    # A title match on its own could be a different release with the same track name
    has_identity = catalog_number_in_path or artist_found or (num_expected >= 3 and title_in_path)

    # Unusual file names can hide a few tracks, an exact file count says they are there anyway
    names_hide_tracks = (coverage >= 0.8 and len(audio_files) == num_expected
                         and (title_in_path or catalog_number_in_path))

    if num_matched == num_expected or names_hide_tracks:
        verdict = Verdict.COMPLETE if has_identity and not has_too_many_files else Verdict.PARTIAL

    elif coverage >= 0.5 and (has_identity or title_in_path):
        verdict = Verdict.PARTIAL

    else:
        verdict = Verdict.MISMATCH

    reasons.append(f"{num_matched}/{num_expected} tracks")

    if has_too_many_files:
        reasons.append(f"{num_extra} other audio files")

    if length_mismatches:
        reasons.append(f"{length_mismatches} wrong track lengths")

    return FolderMatch(
        verdict, round(max(0.0, score), 1), num_expected, num_matched, len(audio_files),
        missing_tracks=[track.title for index, track in enumerate(tracks) if index not in matched_tracks],
        title_in_path=title_in_path, artist_found=artist_found, catalog_number_in_path=catalog_number_in_path,
        length_mismatches=length_mismatches, reasons=reasons
    )


def _match_without_tracklist(release, audio_files, identity_points, title_in_path, artist_found,
                             catalog_number_in_path, reasons):

    num_audio = len(audio_files)
    num_expected = release.track_count
    is_named_after_release = catalog_number_in_path or (title_in_path and artist_found)
    fits_expected_count = not num_expected or num_expected <= num_audio <= num_expected + max(2, num_expected // 2)
    score = identity_points * 2

    if is_named_after_release and fits_expected_count:
        verdict = Verdict.PROBABLE

    elif is_named_after_release:
        verdict = Verdict.PARTIAL
        reasons.append(f"{num_audio} audio files, expected {num_expected}")

    else:
        verdict = Verdict.MISMATCH

    reasons.append("no tracklist to compare with")

    return FolderMatch(
        verdict, round(score, 1), num_expected, 0, num_audio, title_in_path=title_in_path, artist_found=artist_found,
        catalog_number_in_path=catalog_number_in_path, reasons=reasons
    )


def select_remaining_files(release: Release, done_basenames: list, listing: list) -> list:
    """Returns the audio files of a folder that are still needed, given the file names we have
    from another uploader. Their names and even formats can differ, so tracks are what is compared."""

    done_files = _audio_files([(basename, 0) for basename in done_basenames])
    audio_files = _audio_files([(basename, 0) for basename in listing])

    if not release.tracks:
        done_titles = {normalize_text(split_file_name(basename)[1]) for basename, _length in done_files}
        return [basename for basename, _length in audio_files
                if normalize_text(split_file_name(basename)[1]) not in done_titles]

    matched_tracks, _length_mismatches = _assign_tracks(release.tracks, done_files)
    remaining_tracks = [(index, track) for index, track in enumerate(release.tracks) if index not in matched_tracks]
    remaining_files = []
    used_files = set()

    for index, track in remaining_tracks:
        best_score = 0.0
        best_file = None

        for file_index, (basename, length) in enumerate(audio_files):
            if file_index in used_files:
                continue

            score, _is_far = _pair_score(track, index, basename, length)

            if score >= _TRACK_MATCH_THRESHOLD and score > best_score:
                best_score = score
                best_file = file_index

        if best_file is not None:
            used_files.add(best_file)
            remaining_files.append(audio_files[best_file][0])

    return remaining_files


def assess_album_folder(folder_path: str, files: list) -> dict:
    """Guesses whether a folder holds one album, for when no catalog knows the release.

    Protects against downloading a folder like ``Music\\Misc`` because one song
    in it was wanted. Returns {"is_album": bool, "reasons": [...]}.
    """

    audio_files = _audio_files(files)
    folder_name = folder_path.replace("/", "\\").rpartition("\\")[-1]
    normalized_name = normalize_text(folder_name)
    num_files = len(audio_files)

    if normalized_name in _GENERIC_FOLDER_NAMES:
        return {"is_album": False, "reasons": [f"'{folder_name}' is a generic folder name"]}

    if num_files < 2:
        return {"is_album": False, "reasons": ["fewer than two audio files"]}

    if num_files > _MAX_ALBUM_FILES:
        return {"is_album": False, "reasons": [f"{num_files} audio files is too many for one album"]}

    if len({get_extension(basename) for basename, _length in audio_files}) > 1:
        return {"is_album": False, "reasons": ["mixed audio formats"]}

    numbers = []
    num_vinyl_positions = 0

    for basename, _length in audio_files:
        _stem, _stripped, position = split_file_name(basename)

        if position.isdigit():
            numbers.append(int(position))

        elif position:
            num_vinyl_positions += 1

    if num_vinyl_positions + len(numbers) < num_files * 0.7:
        return {"is_album": False, "reasons": ["most files have no track number"]}

    if len(set(numbers)) != len(numbers) and not _DISC_FOLDER_REGEX.match(normalized_name):
        # Repeated numbers within a folder mean it was merged from several releases,
        # unless they are multiple discs: 1-01, 1-02, 2-01, ...
        has_disc_prefix = any(re.match(r"^\s*\d{1,2}[-.]\d", basename) for basename, _length in audio_files)

        if not has_disc_prefix:
            return {"is_album": False, "reasons": ["repeated track numbers"]}

    if numbers and min(numbers) > 2:
        return {"is_album": False, "reasons": [f"track numbers start at {min(numbers)}"]}

    if numbers and (max(numbers) - len(set(numbers))) > 2:
        return {"is_album": False, "reasons": ["gaps in the track numbers"]}

    return {"is_album": True, "reasons": [f"{num_files} numbered audio files in '{folder_name}'"]}


# Searching #


def _clean_query(text: str) -> str:

    words = _NON_WORD_REGEX.sub(" ", text or "").split()
    return " ".join(words[:_MAX_QUERY_WORDS])


def build_search_queries(release: Release) -> list:
    """Soulseek queries for a release, most likely to succeed first.

    A search only matches files whose path contains every word, so each extra
    word risks losing results. That's why a shorter artist name is tried too.
    """

    title = release.title
    queries = []

    if is_various_artists(release.artist):
        queries.append(_clean_query(title))

    else:
        queries.append(_clean_query(f"{release.artist} {title}"))

    # A catalog number is the most precise query there is, when uploaders include it
    for number in release.catalog_numbers:
        compact = "".join(text_tokens(number))

        if len(compact) >= 5 and any(character.isdigit() for character in compact) and \
                any(character.isalpha() for character in compact):
            queries.append(_clean_query(number))
            break

    if not is_various_artists(release.primary_artist):
        queries.append(_clean_query(f"{release.primary_artist} {title}"))

    # People without an international keyboard layout share 'Bjork', not 'Björk'
    queries.append(" ".join(normalize_text(queries[0]).split()[:_MAX_QUERY_WORDS]))

    unique_queries = []

    for query in queries:
        if query and query.casefold() not in {item.casefold() for item in unique_queries}:
            unique_queries.append(query)

    return unique_queries[:_MAX_QUERIES]
