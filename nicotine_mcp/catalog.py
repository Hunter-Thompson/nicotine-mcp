# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Looks up labels, artists, releases and tracklists in online music catalogs.

Record labels are rarely part of the file paths shared on Soulseek, so the
only way to know what a label has released is to ask a catalog.

- MusicBrainz is the primary source: its label listings are clean, and they
  include release types, catalog numbers and track counts. No account needed.
- Discogs is the fallback, it knows more obscure vinyl releases. Its label
  listings also include releases the label was only credited on, so those are
  verified against the release's own label list when its tracklist is fetched.
  Set the ``DISCOGS_TOKEN`` environment variable for a higher rate limit.

Release and label IDs carry their source as a prefix, e.g. ``discogs:178535``.

Only the Python standard library is used. Nothing here depends on the Nicotine+
core, and every call may block for a long time because of the rate limits, so
call them from a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from collections import OrderedDict
from threading import Lock

from nicotine_mcp import __version__
from nicotine_mcp.matching import Release
from nicotine_mcp.matching import Track
from nicotine_mcp.matching import is_various_artists
from nicotine_mcp.matching import normalize_text
from nicotine_mcp.matching import significant_tokens

MUSICBRAINZ = "musicbrainz"
DISCOGS = "discogs"
SOURCES = (MUSICBRAINZ, DISCOGS)

RELEASE_KINDS = ("album", "ep", "single", "compilation", "live", "mix", "other")
_KIND_RANK = {kind: rank for rank, kind in enumerate(RELEASE_KINDS)}

USER_AGENT = f"nicotine-mcp/{__version__} ( https://github.com/Hunter-Thompson/nicotine-mcp )"

_MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/"
_MUSICBRAINZ_INTERVAL = 1.1
_MUSICBRAINZ_PAGE_SIZE = 100

_DISCOGS_URL = "https://api.discogs.com/"
_DISCOGS_INTERVAL = 2.5
_DISCOGS_INTERVAL_WITH_TOKEN = 1.1
_DISCOGS_PAGE_SIZE = 100
_DISCOGS_APPEARANCE_ROLES = {"Appearance", "TrackAppearance", "Remix"}
_RELEASE_TYPE_WORDS = {"ep", "lp"}  # Part of the title in one catalog, and not in the other

_MAX_PAGES = 30
_MIN_RECORDING_SCORE = 60
_ALBUM_SEARCH_SIZE = 25

_DISCOGS_NAME_SUFFIX_REGEX = re.compile(r"\s*\(\d+\)")
_PARENTHESES_REGEX = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_LUCENE_ESCAPE_REGEX = re.compile(r'(["\\])')
_CATALOG_NUMBER_REGEX = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)$")
_MUSICBRAINZ_DIGITAL_FORMATS = ("digital",)
_MUSICBRAINZ_SECONDARY_KINDS = {"compilation": "compilation", "live": "live", "dj-mix": "mix", "mixtape/street": "mix"}
_DISCOGS_UNOFFICIAL_FORMATS = {"promo", "tp", "w/lbl", "unofficial", "smplr"}


class CatalogError(RuntimeError):
    """A catalog could not be reached, or does not know what was asked for."""


class HttpClient:
    """Fetches JSON from one site, keeping to its rate limit, retrying when it
    is overloaded and caching responses for repeated lookups."""

    MAX_ATTEMPTS = 4
    TIMEOUT = 25
    CACHE_SIZE = 512
    CACHE_SECONDS = 3600

    def __init__(self, min_interval, headers=None, opener=None, sleep=time.sleep, clock=time.monotonic):

        self.min_interval = min_interval
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._clock = clock
        self._lock = Lock()
        self._last_request_at = None
        self._cache = OrderedDict()

    def _wait_for_turn(self):

        with self._lock:
            now = self._clock()
            delay = 0.0

            if self._last_request_at is not None:
                delay = max(0.0, self._last_request_at + self.min_interval - now)

            self._last_request_at = now + delay

        if delay:
            self._sleep(delay)

    def _get_cached(self, url):

        with self._lock:
            cached = self._cache.get(url)

            if cached is None:
                return None

            if self._clock() - cached[0] > self.CACHE_SECONDS:
                del self._cache[url]
                return None

            self._cache.move_to_end(url)
            return cached[1]

    def _store_cached(self, url, data):

        with self._lock:
            self._cache[url] = (self._clock(), data)

            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)

    def get_json(self, url):

        cached = self._get_cached(url)

        if cached is not None:
            return cached

        site = urllib.parse.urlparse(url).netloc
        request = urllib.request.Request(url, headers=self.headers)
        error_message = None

        for attempt in range(self.MAX_ATTEMPTS):
            self._wait_for_turn()
            retry_after = 2.0 ** (attempt + 1)

            try:
                with self._opener(request, timeout=self.TIMEOUT) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self._store_cached(url, data)
                return data

            except urllib.error.HTTPError as error:
                error_message = f"{site} returned HTTP {error.code}"

                if error.code == 404:
                    raise CatalogError(f"{site} does not know this entry (HTTP 404)") from error

                if error.code not in {429, 500, 502, 503, 504}:
                    raise CatalogError(error_message) from error

                try:
                    retry_after = max(retry_after, float(error.headers.get("Retry-After") or 0))

                except (AttributeError, TypeError, ValueError):
                    pass

            except (urllib.error.URLError, TimeoutError, OSError) as error:
                error_message = f"could not reach {site}: {error}"

            except ValueError as error:
                raise CatalogError(f"{site} returned an invalid response") from error

            if attempt + 1 < self.MAX_ATTEMPTS:
                self._sleep(retry_after)

        raise CatalogError(error_message)


def _year_of(date) -> int | None:

    match = re.match(r"\d{4}", str(date or ""))
    return int(match.group()) if match else None


def _merge_releases(releases: list) -> Release:
    """Merges editions of one release. The first one is the preferred edition,
    the one whose tracklist will be fetched."""

    merged = releases[0]
    years = [release.year for release in releases if release.year]
    merged.year = min(years) if years else None

    for release in releases[1:]:
        for number in release.catalog_numbers:
            if number not in merged.catalog_numbers:
                merged.catalog_numbers.append(number)

        for release_format in release.formats:
            if release_format not in merged.formats:
                merged.formats.append(release_format)

    return merged


def _release_keys(release):
    """What can tell that entries of two catalogs are the same release. Punctuation and
    word order differ between them: 'Dettmann | Wiedemann' and 'Wiedemann & Dettmann'."""

    title_tokens = significant_tokens(release.title)
    title = tuple(sorted(title_tokens - _RELEASE_TYPE_WORDS or title_tokens))
    artist = () if is_various_artists(release.artist) else tuple(sorted(significant_tokens(release.artist)))
    keys = {("name", artist, title)}

    for number in release.catalog_numbers:
        compact = "".join(normalize_text(number).split())
        match = _CATALOG_NUMBER_REGEX.match(compact)

        if match:
            keys.add(("number", match.group("prefix"), int(match.group("number"))))

    return keys


def merge_catalogs(primary, secondary):
    """Combines the listings of a label from two catalogs. Neither is complete: MusicBrainz
    lacked 6 of the 38 numbers of BITE, and 7 of 132 of Ostgut Ton's 12" series. Entries
    of the first catalog are kept for what both know about. Returns (releases, number added)."""

    merged = list(primary)
    known = {}

    for release in merged:
        for key in _release_keys(release):
            known.setdefault(key, release)

    num_added = 0

    for release in secondary:
        keys = _release_keys(release)
        existing = next((known[key] for key in keys if key in known), None)

        if existing is not None:
            for number in release.catalog_numbers:
                if number not in existing.catalog_numbers:
                    existing.catalog_numbers.append(number)

            if release.year and (not existing.year or release.year < existing.year):
                existing.year = release.year

            continue

        merged.append(release)
        num_added += 1

        for key in keys:
            known[key] = release

    return merged, num_added


def _base_title_tokens(title):
    """The words of a title without remarks in parentheses and release types like 'EP'."""

    tokens = significant_tokens(_PARENTHESES_REGEX.sub(" ", title or ""))
    return tokens - _RELEASE_TYPE_WORDS or tokens


def album_title_rank(wanted, title):
    """How well the title of a release matches the one asked for: 0 when it is the same, 1 apart
    from a remark in parentheses or a type like 'EP', 2 when one contains the other, None otherwise."""

    if normalize_text(wanted) == normalize_text(title):
        return 0

    wanted_tokens = _base_title_tokens(wanted)
    title_tokens = _base_title_tokens(title)

    if wanted_tokens == title_tokens:
        return 1

    if wanted_tokens and title_tokens and (wanted_tokens <= title_tokens or title_tokens <= wanted_tokens):
        return 2

    return None


def rank_album_matches(releases, artist, title):
    """Keeps the releases that are the album asked for, the closest match first: by title, then
    albums before EPs and singles, then the oldest. Any credit shared with the artist counts, so
    collaborations are found, and compilations are matched by title alone."""

    ranked = []

    for release in releases:
        title_rank = album_title_rank(title, release.title)

        if title_rank is None:
            continue

        if not (is_various_artists(artist) or is_various_artists(release.artist)
                or significant_tokens(artist) & significant_tokens(release.artist)):
            continue

        rank = (title_rank, _KIND_RANK.get(release.kind, len(_KIND_RANK)), release.year or 9999)
        ranked.append((rank, release))

    ranked.sort(key=lambda entry: entry[0])
    return [release for _rank, release in ranked]


# MusicBrainz #


class MusicBrainzClient:

    def __init__(self, http=None):
        self.http = http or HttpClient(_MUSICBRAINZ_INTERVAL)

    def _get(self, path, **params):

        params["fmt"] = "json"
        return self.http.get_json(_MUSICBRAINZ_URL + path + "?" + urllib.parse.urlencode(params))

    @staticmethod
    def _quote(text):
        return '"' + _LUCENE_ESCAPE_REGEX.sub(r"\\\1", text) + '"'

    def find_labels(self, name, limit=10):

        data = self._get("label", query=f"label:{self._quote(name)}", limit=limit)
        labels = []

        for label in data.get("labels", []):
            labels.append({
                "label_id": f"{MUSICBRAINZ}:{label['id']}",
                "source": MUSICBRAINZ,
                "name": label.get("name"),
                "description": label.get("disambiguation") or None,
                "type": label.get("type"),
                "country": label.get("country"),
                "area": (label.get("area") or {}).get("name"),
                "active_since": _year_of((label.get("life-span") or {}).get("begin")),
                "match_score": label.get("score")
            })

        return labels

    def find_artists(self, name, limit=10):

        data = self._get("artist", query=f"artist:{self._quote(name)}", limit=limit)
        artists = []

        for artist in data.get("artists", []):
            artists.append({
                "artist_id": f"{MUSICBRAINZ}:{artist['id']}",
                "source": MUSICBRAINZ,
                "name": artist.get("name"),
                "description": artist.get("disambiguation") or None,
                "type": artist.get("type"),
                "country": artist.get("country"),
                "area": (artist.get("area") or {}).get("name"),
                "active_since": _year_of((artist.get("life-span") or {}).get("begin")),
                "match_score": artist.get("score")
            })

        return artists

    @staticmethod
    def _artist_credit(credits_list):

        artist = "".join(credit.get("name", "") + credit.get("joinphrase", "") for credit in credits_list or [])
        primary_artist = (credits_list[0].get("name") or "") if credits_list else ""
        return artist, primary_artist

    @staticmethod
    def _release_kind(release_group):

        for secondary_type in release_group.get("secondary-types") or []:
            kind = _MUSICBRAINZ_SECONDARY_KINDS.get(secondary_type.lower())

            if kind:
                return kind

        primary_type = (release_group.get("primary-type") or "").lower()
        return primary_type if primary_type in {"album", "ep", "single"} else "other"

    @classmethod
    def _parse_release(cls, data):

        artist, primary_artist = cls._artist_credit(data.get("artist-credit"))
        media = data.get("media") or []
        catalog_numbers = []
        label_ids = []

        for label_info in data.get("label-info") or []:
            number = label_info.get("catalog-number")

            if number and number not in catalog_numbers:
                catalog_numbers.append(number)

            label_id = (label_info.get("label") or {}).get("id")

            if label_id:
                label_ids.append(label_id)

        return Release(
            release_id=f"{MUSICBRAINZ}:{data['id']}",
            source=MUSICBRAINZ,
            artist=artist,
            primary_artist=primary_artist,
            title=data.get("title") or "",
            year=_year_of(data.get("date")),
            kind=cls._release_kind(data.get("release-group") or {}),
            catalog_numbers=catalog_numbers,
            formats=sorted({medium["format"] for medium in media if medium.get("format")}),
            track_count=sum(medium.get("track-count") or 0 for medium in media),
            label_ids=label_ids
        )

    @staticmethod
    def _edition_preference(data):
        """Sort key for editions of a release. Digital ones have the tracklist shared files
        most likely follow (vinyl editions often lack tracks), official ones have the best data."""

        formats = [(medium.get("format") or "").lower() for medium in data.get("media") or []]
        is_digital = any(any(name in item for name in _MUSICBRAINZ_DIGITAL_FORMATS) for item in formats)
        return (data.get("status") != "Official", not is_digital, not formats, data.get("date") or "9999")

    def _browse_releases(self, groups, **query):
        """Adds the editions of a listing to ``groups``. Returns (their number, whether the listing was cut off)."""

        offset = 0
        total = 0

        for _page in range(_MAX_PAGES):
            data = self._get("release", limit=_MUSICBRAINZ_PAGE_SIZE, offset=offset,
                             inc="artist-credits+labels+release-groups+media", **query)
            items = data.get("releases") or []
            total = data.get("release-count") or len(items)

            for item in items:
                if item.get("status") in {"Bootleg", "Pseudo-Release"}:
                    continue

                group_id = (item.get("release-group") or {}).get("id") or item["id"]
                groups.setdefault(group_id, []).append(item)

            offset += len(items)

            if not items or offset >= total:
                break

        return total, offset < total

    def _merge_groups(self, groups):

        releases = []

        for editions in groups.values():
            editions.sort(key=self._edition_preference)
            releases.append(_merge_releases([self._parse_release(edition) for edition in editions]))

        return releases

    def label_releases(self, label_id):
        """Returns (releases, number of listed editions, whether the listing was cut off)."""

        groups = {}
        total, is_truncated = self._browse_releases(groups, label=label_id)

        return self._merge_groups(groups), total, is_truncated

    def artist_releases(self, artist_id, include_appearances=False):
        """Returns (releases, number of listed editions, whether the listing was cut off). Appearances
        are releases credited to somebody else with a track by this artist, like compilations."""

        groups = {}
        total, is_truncated = self._browse_releases(groups, artist=artist_id)

        if include_appearances:
            num_appearances, is_appearances_truncated = self._browse_releases(groups, track_artist=artist_id)
            total += num_appearances
            is_truncated = is_truncated or is_appearances_truncated

        return self._merge_groups(groups), total, is_truncated

    def release_details(self, release_id):

        data = self._get(f"release/{release_id}", inc="recordings+artist-credits+labels+release-groups")
        release = self._parse_release(data)
        tracks = []

        for medium in data.get("media") or []:
            prefix = f"{medium.get('position')}-" if len(data["media"]) > 1 else ""

            for track in medium.get("tracks") or []:
                length = track.get("length") or (track.get("recording") or {}).get("length") or 0
                tracks.append(Track(
                    title=track.get("title") or (track.get("recording") or {}).get("title") or "",
                    length=round(length / 1000),
                    position=str(track.get("number") or f"{prefix}{track.get('position') or ''}")
                ))

        release.tracks = tracks
        release.track_count = len(tracks) or release.track_count
        return release

    def find_track_albums(self, artist, title, limit=25):
        """Returns the releases a song appears on, the most canonical one first."""

        query = f"recording:{self._quote(title)} AND artist:{self._quote(artist)}"
        data = self._get("recording", query=query, limit=limit)
        wanted_title = normalize_text(title)
        wanted_artist = significant_tokens(artist)
        groups = {}

        for recording in data.get("recordings") or []:
            if (recording.get("score") or 0) < _MIN_RECORDING_SCORE:
                continue

            credited_artist, _primary_artist = self._artist_credit(recording.get("artist-credit"))

            if not wanted_artist & significant_tokens(credited_artist):
                continue

            recording_title = normalize_text(recording.get("title") or "")
            base_title = normalize_text(_PARENTHESES_REGEX.sub(" ", recording.get("title") or ""))

            # An exact title beats the same song's remixes, which beat other songs containing the words
            title_rank = 0 if recording_title == wanted_title else (1 if base_title == wanted_title else 2)

            for release in recording.get("releases") or []:
                if release.get("status") in {"Bootleg", "Pseudo-Release"}:
                    continue

                release_group = release.get("release-group") or {}
                kind = self._release_kind(release_group)
                entry = {
                    "release_id": f"{MUSICBRAINZ}:{release['id']}",
                    "title": release.get("title"),
                    "artist": credited_artist,
                    "year": _year_of(release.get("date")),
                    "kind": kind,
                    "track_count": release.get("track-count") or 0,
                    "matched_recording": recording.get("title"),
                    "_rank": (
                        title_rank, _KIND_RANK[kind], release.get("status") != "Official",
                        release.get("date") or "9999"
                    )
                }
                group_id = release_group.get("id") or release["id"]

                if group_id not in groups or entry["_rank"] < groups[group_id]["_rank"]:
                    groups[group_id] = entry

        albums = sorted(groups.values(), key=lambda item: item["_rank"])

        for album in albums:
            del album["_rank"]

        return albums

    def find_albums(self, artist, title, limit=10):
        """Returns the releases with this title by this artist, the closest match first,
        with their editions merged."""

        query = f"release:{self._quote(title)} AND artist:{self._quote(artist)}"
        data = self._get("release", query=query, limit=_ALBUM_SEARCH_SIZE)
        groups = {}

        for item in data.get("releases") or []:
            if item.get("status") in {"Bootleg", "Pseudo-Release"}:
                continue

            group_id = (item.get("release-group") or {}).get("id") or item["id"]
            groups.setdefault(group_id, []).append(item)

        return rank_album_matches(self._merge_groups(groups), artist, title)[:limit]


# Discogs #


class DiscogsClient:

    def __init__(self, http=None, token=None):

        if http is None:
            headers = {"Authorization": f"Discogs token={token}"} if token else None
            http = HttpClient(_DISCOGS_INTERVAL_WITH_TOKEN if token else _DISCOGS_INTERVAL, headers=headers)

        self.http = http

    def _get(self, path, **params):

        query = "?" + urllib.parse.urlencode(params) if params else ""
        return self.http.get_json(_DISCOGS_URL + path + query)

    @staticmethod
    def clean_name(name):
        """Discogs tells apart artists with the same name as 'Barker (2)', and marks
        name variations with a '*'. Neither appears in shared file names."""

        name = _DISCOGS_NAME_SUFFIX_REGEX.sub("", (name or "").replace("*", ""))
        return " ".join(name.replace(" | ", ", ").split())

    def _find_by_name(self, name, entry_type, limit):

        data = self._get("database/search", q=name, type=entry_type, per_page=limit)
        wanted_name = normalize_text(name)
        entries = []

        for result in data.get("results") or []:
            title = result.get("title") or ""
            entries.append({
                f"{entry_type}_id": f"{DISCOGS}:{result['id']}",
                "source": DISCOGS,
                "name": title,
                "description": None,
                "type": None,
                "country": None,
                "area": None,
                "active_since": None,
                "match_score": 100 if normalize_text(self.clean_name(title)) == wanted_name else 50
            })

        entries.sort(key=lambda item: -item["match_score"])
        return entries

    def find_labels(self, name, limit=10):
        return self._find_by_name(name, "label", limit)

    def find_artists(self, name, limit=10):
        return self._find_by_name(name, "artist", limit)

    def find_albums(self, artist, title, limit=10):
        """Returns the releases with this title by this artist, the closest match first,
        with their editions merged."""

        params = {"release_title": title, "type": "release", "per_page": _ALBUM_SEARCH_SIZE}

        if not is_various_artists(artist):
            params["artist"] = artist

        data = self._get("database/search", **params)
        items = []

        for result in data.get("results") or []:
            # Search results carry 'Artist - Title' in one field
            credited, separator, release_title = (result.get("title") or "").partition(" - ")

            if not separator:
                continue

            year = str(result.get("year") or "")
            items.append({
                "id": result["id"], "artist": credited, "title": release_title,
                "format": ", ".join(result.get("format") or []), "catno": result.get("catno"),
                "year": int(year) if year.isdigit() else None
            })

        return rank_album_matches(self._merge_listing(items), artist, title)[:limit]

    @staticmethod
    def _kind_from_format(format_names):

        tokens = {item.strip().lower() for item in format_names}

        for token, kind in (
            ("comp", "compilation"), ("compilation", "compilation"), ("mixed", "mix"), ("live", "live"),
            ("ep", "ep"), ("mini-album", "ep"), ("album", "album"), ("lp", "album"), ("single", "single"),
            ("maxi", "single"), ('7"', "single")
        ):
            if token in tokens:
                return kind

        # Most electronic 12" records are not marked as anything, and are EPs in all but name
        return "ep" if '12"' in tokens or '10"' in tokens else "other"

    @staticmethod
    def _edition_preference(item):

        tokens = {token.strip().lower() for token in (item.get("format") or "").split(",")}
        return (bool(tokens & _DISCOGS_UNOFFICIAL_FORMATS), "file" not in tokens, "cd" not in tokens,
                item.get("year") or 9999)

    def _parse_listing_item(self, item):

        artist = self.clean_name(item.get("artist"))
        format_names = (item.get("format") or "").split(",")
        catalog_number = (item.get("catno") or "").strip()

        return Release(
            # Artist listings have masters, whose tracklists are found through their main release
            release_id=f"{DISCOGS}:{item.get('main_release') or item['id']}",
            source=DISCOGS,
            artist=artist,
            primary_artist=artist.split(", ", maxsplit=1)[0],
            title=item.get("title") or "",
            year=item.get("year") or None,
            kind=self._kind_from_format(format_names),
            catalog_numbers=[catalog_number] if catalog_number and catalog_number.lower() != "none" else [],
            formats=[name.strip() for name in format_names if name.strip()]
        )

    def label_releases(self, label_id):
        """Returns (releases, number of listed editions, whether the listing was cut off)."""

        items = {}
        pages = 1
        page = 1

        while page <= min(pages, _MAX_PAGES):
            data = self._get(f"labels/{label_id}/releases", page=page, per_page=_DISCOGS_PAGE_SIZE)
            pages = (data.get("pagination") or {}).get("pages") or 1

            # The same release is listed once per catalog number
            for item in data.get("releases") or []:
                items.setdefault(item["id"], item)

            page += 1

        releases = self._merge_listing(items.values())

        for release in releases:
            release.listed_on_label = f"{DISCOGS}:{label_id}"

        return releases, len(items), pages > _MAX_PAGES

    def _merge_listing(self, items):

        groups = {}

        for item in items:
            key = (normalize_text(self.clean_name(item.get("artist"))), normalize_text(item.get("title") or ""))
            groups.setdefault(key, []).append(item)

        releases = []

        for editions in groups.values():
            editions.sort(key=self._edition_preference)
            releases.append(_merge_releases([self._parse_listing_item(edition) for edition in editions]))

        return releases

    def artist_releases(self, artist_id, include_appearances=False):
        """Returns (releases, number of listed editions, whether the listing was cut off). Discogs also
        lists what somebody produced or wrote, those releases are not part of a discography."""

        roles = _DISCOGS_APPEARANCE_ROLES | {"Main"} if include_appearances else {"Main"}
        items = {}
        pages = 1
        page = 1

        while page <= min(pages, _MAX_PAGES):
            data = self._get(f"artists/{artist_id}/releases", page=page, per_page=_DISCOGS_PAGE_SIZE, sort="year")
            pages = (data.get("pagination") or {}).get("pages") or 1

            for item in data.get("releases") or []:
                # Promos are a real edition of a release that may be known no other way, bootlegs are not
                is_bootleg = "unofficial" in (item.get("format") or "").lower()

                if item.get("role") in roles and not is_bootleg:
                    items.setdefault((item.get("type"), item["id"]), item)

            page += 1

        return self._merge_listing(items.values()), len(items), pages > _MAX_PAGES

    @staticmethod
    def _parse_length(duration):

        seconds = 0

        for part in (duration or "").split(":"):
            if not part.isdigit():
                return 0

            seconds = seconds * 60 + int(part)

        return seconds

    @classmethod
    def _flatten_tracks(cls, tracklist):

        tracks = []

        for track in tracklist or []:
            if track.get("sub_tracks"):
                tracks.extend(cls._flatten_tracks(track["sub_tracks"]))

            elif track.get("type_", "track") == "track":
                tracks.append(Track(
                    title=track.get("title") or "", length=cls._parse_length(track.get("duration")),
                    position=track.get("position") or ""
                ))

        return tracks

    def release_details(self, release_id):

        data = self._get(f"releases/{release_id}")
        artists = [self.clean_name(artist.get("anv") or artist.get("name")) for artist in data.get("artists") or []]
        labels = data.get("labels") or []
        format_names = []

        for release_format in data.get("formats") or []:
            format_names.append(release_format.get("name") or "")
            format_names.extend(release_format.get("descriptions") or [])

        tracks = self._flatten_tracks(data.get("tracklist"))
        catalog_numbers = []

        for label in labels:
            number = (label.get("catno") or "").strip()

            if number and number.lower() != "none" and number not in catalog_numbers:
                catalog_numbers.append(number)

        return Release(
            release_id=f"{DISCOGS}:{release_id}",
            source=DISCOGS,
            artist=", ".join(artists),
            primary_artist=artists[0] if artists else "",
            title=data.get("title") or "",
            year=data.get("year") or None,
            kind=self._kind_from_format(format_names),
            catalog_numbers=catalog_numbers,
            formats=[name for name in format_names if name],
            track_count=len(tracks),
            label_ids=[str(label["id"]) for label in labels if label.get("id")],
            tracks=tracks
        )


# Filtering #


def filter_releases(releases, kinds=None, year_from=None, year_to=None, artist=None, catalog_prefix=None,
                    exclude_kinds=None):
    """Applies the filters an agent can ask for to a list of releases."""

    wanted_kinds = {kind.lower() for kind in kinds} if kinds else None
    excluded_kinds = {kind.lower() for kind in exclude_kinds} if exclude_kinds else set()

    for kind in (wanted_kinds or set()) | excluded_kinds:
        if kind not in RELEASE_KINDS:
            raise ValueError(f"Unknown release kind '{kind}', expected one of {', '.join(RELEASE_KINDS)}")

    wanted_artist = normalize_text(artist) if artist else None
    wanted_prefix = "".join(normalize_text(catalog_prefix).split()) if catalog_prefix else None
    selected = []

    for release in releases:
        if wanted_kinds is not None and release.kind not in wanted_kinds:
            continue

        if release.kind in excluded_kinds:
            continue

        if year_from and (not release.year or release.year < year_from):
            continue

        if year_to and (not release.year or release.year > year_to):
            continue

        if wanted_artist and wanted_artist not in normalize_text(release.artist):
            continue

        if wanted_prefix and not any(
            "".join(normalize_text(number).split()).startswith(wanted_prefix) for number in release.catalog_numbers
        ):
            continue

        selected.append(release)

    return selected


# Facade #


class Catalog:
    """Routes lookups to the right catalog, based on the prefix of an ID."""

    def __init__(self, musicbrainz=None, discogs=None, discogs_token=None):

        self.musicbrainz = musicbrainz or MusicBrainzClient()
        self.discogs = discogs or DiscogsClient(token=discogs_token or os.environ.get("DISCOGS_TOKEN"))

    def _client_for(self, entry_id):

        source, separator, identifier = str(entry_id or "").partition(":")

        if not separator or not identifier or source not in SOURCES:
            raise ValueError(f"Invalid ID '{entry_id}', expected one starting with "
                             f"{MUSICBRAINZ}: or {DISCOGS}: as returned by find_label or find_artist")

        return (self.musicbrainz if source == MUSICBRAINZ else self.discogs), identifier

    def _find_by_name(self, find, name, source, limit):
        """Returns entries matching a name from every catalog, and the warnings of a
        catalog that cannot be reached, since the other one may still have the answer."""

        name = (name or "").strip()

        if not name:
            raise ValueError("Name is empty")

        if source != "all" and source not in SOURCES:
            raise ValueError(f"Unknown catalog '{source}', expected one of all, {', '.join(SOURCES)}")

        entries = []
        warnings = []

        for client_name, client in ((MUSICBRAINZ, self.musicbrainz), (DISCOGS, self.discogs)):
            if source not in {"all", client_name}:
                continue

            try:
                entries.extend(find(client, name, limit))

            except CatalogError as error:
                warnings.append(str(error))

        if not entries and warnings:
            raise CatalogError("; ".join(warnings))

        return entries, warnings

    def find_labels(self, name, source="all", limit=10):

        labels, warnings = self._find_by_name(lambda client, *args: client.find_labels(*args), name, source, limit)
        return {"labels": labels, "warnings": warnings}

    def find_artists(self, name, source="all", limit=10):

        artists, warnings = self._find_by_name(lambda client, *args: client.find_artists(*args), name, source, limit)
        return {"artists": artists, "warnings": warnings}

    def _combined_listing(self, fetch, entry_id, also_id, filters):
        """Lists releases, combining two catalogs when given the ID of the same label or artist in both."""

        client, identifier = self._client_for(entry_id)
        releases, num_listed, is_truncated = fetch(client, identifier)
        num_added = 0

        if also_id:
            client, identifier = self._client_for(also_id)
            other_releases, num_other_listed, is_other_truncated = fetch(client, identifier)
            releases, num_added = merge_catalogs(releases, other_releases)
            num_listed += num_other_listed
            is_truncated = is_truncated or is_other_truncated

        selected = filter_releases(releases, **filters)
        selected.sort(key=lambda release: (-(release.year or 0), normalize_text(release.title)))

        return {
            "num_editions_listed": num_listed,
            "num_releases": len(releases),
            "num_selected": len(selected),
            "num_added_from_second_catalog": num_added,
            "is_truncated": is_truncated,
            "releases": selected
        }

    def label_releases(self, label_id, also_label_id=None, **filters):

        listing = self._combined_listing(
            lambda client, identifier: client.label_releases(identifier), label_id, also_label_id, filters
        )
        return {"label_id": label_id, **listing}

    def artist_releases(self, artist_id, also_artist_id=None, include_appearances=False, **filters):

        listing = self._combined_listing(
            lambda client, identifier: client.artist_releases(identifier, include_appearances),
            artist_id, also_artist_id, filters
        )
        return {"artist_id": artist_id, **listing}

    def release_details(self, release_id):

        client, identifier = self._client_for(release_id)
        return client.release_details(identifier)

    def find_track_albums(self, artist, title, limit=25):

        if not (artist or "").strip() or not (title or "").strip():
            raise ValueError("Both an artist and a title are required")

        return self.musicbrainz.find_track_albums(artist.strip(), title.strip(), limit=limit)

    def find_albums(self, artist, title, limit=10):
        """Looks an album up in every catalog. The same release known to both is listed once,
        and a catalog that cannot be reached is only a warning while the other one answers."""

        artist = (artist or "").strip()
        title = (title or "").strip()

        if not artist or not title:
            raise ValueError("Both an artist and a title are required")

        albums = []
        warnings = []

        for client in (self.musicbrainz, self.discogs):
            try:
                albums, _num_added = merge_catalogs(albums, client.find_albums(artist, title, limit=limit))

            except CatalogError as error:
                warnings.append(str(error))

        if not albums and len(warnings) == 2:
            raise CatalogError("; ".join(warnings))

        return {"albums": rank_album_matches(albums, artist, title)[:limit], "warnings": warnings}

    @staticmethod
    def is_on_label(release, label_id):
        """Discogs label listings also include releases a label was only credited on. Returns
        False when a release's own label list shows that this is one of those."""

        source, _separator, identifier = str(release.listed_on_label or label_id or "").partition(":")

        if source != DISCOGS or not release.label_ids:
            return True

        return identifier in release.label_ids
