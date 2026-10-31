# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import json
import urllib.error

from unittest import TestCase

from nicotine_mcp.catalog import Catalog
from nicotine_mcp.catalog import CatalogError
from nicotine_mcp.catalog import DiscogsClient
from nicotine_mcp.catalog import HttpClient
from nicotine_mcp.catalog import MusicBrainzClient
from nicotine_mcp.catalog import _relaxed_name
from nicotine_mcp.catalog import album_title_rank
from nicotine_mcp.catalog import filter_releases
from nicotine_mcp.catalog import merge_catalogs
from nicotine_mcp.catalog import rank_album_matches
from nicotine_mcp.matching import Release

BITE_ID = "d4d9c40d-894e-49f6-89e6-28b19919cef9"


def mb_release(release_id, title, artist="New Frames", date="2020-10-23", status="Official", kind="EP", secondary=None,
               group=None, catno="BITE016", media=(("Digital Media", 4),)):

    return {
        "id": release_id,
        "title": title,
        "date": date,
        "status": status,
        "artist-credit": [{"name": artist, "joinphrase": ""}],
        "label-info": [{"catalog-number": catno, "label": {"id": BITE_ID, "name": "BITE"}}],
        "release-group": {
            "id": group or f"group-{release_id}", "primary-type": kind, "secondary-types": secondary or []
        },
        "media": [{"format": item[0], "track-count": item[1]} for item in media]
    }


class FakeResponse(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class FakeOpener:
    """Answers requests from a list of (URL part, response), recording what was asked for."""

    def __init__(self, routes):

        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout=None):

        self.requests.append(request)

        for url_part, response in self.routes:
            if url_part in request.full_url:
                if isinstance(response, list):
                    response = response.pop(0)

                if isinstance(response, Exception):
                    raise response

                return FakeResponse(json.dumps(response).encode("utf-8"))

        raise AssertionError(f"Unexpected request to {request.full_url}")


def make_http(routes, **kwargs):

    opener = FakeOpener(routes)
    sleeps = []
    http = HttpClient(0, opener=opener, sleep=sleeps.append, **kwargs)
    return http, opener, sleeps


def http_error(code, retry_after=None):

    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.org", code, "error", headers, None)


class HttpClientTest(TestCase):

    def test_identifies_itself(self):

        http, opener, _sleeps = make_http([("example.org", {"a": 1})])
        http.get_json("https://example.org/")

        self.assertIn("nicotine-mcp/", opener.requests[0].get_header("User-agent"))

    def test_keeps_to_the_rate_limit(self):

        clock = [100.0]
        sleeps = []
        opener = FakeOpener([("example.org", {"a": 1})])
        http = HttpClient(1.1, opener=opener, sleep=sleeps.append, clock=lambda: clock[0])

        http.get_json("https://example.org/1")
        self.assertEqual(sleeps, [])

        http.get_json("https://example.org/2")
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.1)

    def test_caches_responses(self):

        http, opener, _sleeps = make_http([("example.org", {"a": 1})])

        self.assertEqual(http.get_json("https://example.org/"), {"a": 1})
        self.assertEqual(http.get_json("https://example.org/"), {"a": 1})
        self.assertEqual(len(opener.requests), 1)

    def test_retries_when_overloaded(self):

        http, opener, sleeps = make_http([("example.org", [http_error(503), http_error(429, "30"), {"a": 1}])])

        self.assertEqual(http.get_json("https://example.org/"), {"a": 1})
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(sleeps, [2.0, 30.0])

    def test_gives_up_eventually(self):

        http, opener, _sleeps = make_http([("example.org", [http_error(503) for _ in range(4)])])

        with self.assertRaisesRegex(CatalogError, "HTTP 503"):
            http.get_json("https://example.org/")

        self.assertEqual(len(opener.requests), 4)

    def test_does_not_retry_client_errors(self):

        http, opener, _sleeps = make_http([("example.org", [http_error(404)])])

        with self.assertRaises(CatalogError):
            http.get_json("https://example.org/")

        self.assertEqual(len(opener.requests), 1)

    def test_network_failure(self):

        http, _opener, _sleeps = make_http([("example.org", [urllib.error.URLError("no route") for _ in range(4)])])

        with self.assertRaisesRegex(CatalogError, "could not reach example.org"):
            http.get_json("https://example.org/")


class MusicBrainzTest(TestCase):

    opener = None

    def client(self, routes):

        http, self.opener, _sleeps = make_http(routes)
        return MusicBrainzClient(http)

    def test_find_labels(self):

        client = self.client([("ws/2/label", {"labels": [{
            "id": BITE_ID, "name": "BITE", "score": 100, "disambiguation": "techno", "type": "Original Production",
            "area": {"name": "Berlin"}, "life-span": {"begin": "2018"}
        }]})])
        label = client.find_labels("Bite")[0]

        self.assertEqual(label["label_id"], f"musicbrainz:{BITE_ID}")
        self.assertEqual(label["description"], "techno")
        self.assertEqual(label["area"], "Berlin")
        self.assertEqual(label["active_since"], 2018)

    def test_search_terms_are_escaped(self):

        client = self.client([("ws/2/label", {"labels": []})])
        client.find_labels('He said "hi"')

        self.assertIn("%5C%22hi%5C%22", self.opener.requests[0].full_url)

    def test_find_artists(self):

        client = self.client([("ws/2/artist", {"artists": [
            {"id": "a1", "name": "Phase Fatale", "disambiguation": "techno producer", "type": "Person",
             "country": "DE", "life-span": {"begin": "2012"}, "score": 100}
        ]})])

        artists = client.find_artists("Phase Fatale")

        self.assertEqual(artists[0]["artist_id"], "musicbrainz:a1")
        self.assertEqual(artists[0]["description"], "techno producer")
        self.assertEqual(artists[0]["active_since"], 2012)

    def test_find_albums_prefers_the_exact_title_and_merges_editions(self):

        client = self.client([("ws/2/release", {"releases": [
            mb_release("r1", "Altars (The Remixes)", group="g1"),
            mb_release("r2", "Altars", group="g2", media=(('12" Vinyl', 4),), date="2021-01-01"),
            mb_release("r3", "Altars", group="g2", date="2020-10-23"),
            mb_release("r4", "Altars", artist="Somebody Else"),
            mb_release("r5", "Unrelated Record")
        ]})])

        albums = client.find_albums("New Frames", "Altars")

        self.assertEqual([album.title for album in albums], ["Altars", "Altars (The Remixes)"])
        self.assertEqual(albums[0].release_id, "musicbrainz:r3")  # The digital edition is preferred
        self.assertEqual(albums[0].year, 2020)
        self.assertEqual(albums[0].labels, ["BITE"])
        self.assertIn("release%3A%22Altars%22+AND+artist%3A%22New+Frames%22", self.opener.requests[0].full_url)

    def test_artist_releases_leaves_out_appearances_by_default(self):

        client = self.client([("ws/2/release", {"release-count": 1, "releases": [mb_release("r1", "Altars")]})])

        releases, total, is_truncated = client.artist_releases("a1")

        self.assertEqual([release.title for release in releases], ["Altars"])
        self.assertEqual((total, is_truncated), (1, False))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertIn("artist=a1", self.opener.requests[0].full_url)

    def test_artist_releases_can_include_appearances(self):

        client = self.client([("ws/2/release", {"release-count": 1, "releases": [mb_release("r1", "Altars")]})])

        _releases, total, _is_truncated = client.artist_releases("a1", include_appearances=True)

        self.assertEqual(total, 2)
        self.assertIn("track_artist=a1", self.opener.requests[1].full_url)

    def test_label_releases_merges_editions(self):

        client = self.client([("ws/2/release", {"release-count": 3, "releases": [
            mb_release("vinyl", "Science Gardens", group="g", catno="BITE06", date="2019-01",
                       media=(('12" Vinyl', 4),)),
            mb_release("digital", "Science Gardens", group="g", catno="BITE06D", date="2019-02"),
            mb_release("other", "Outer Limits", group="h", catno="BITE016")
        ]})])
        releases, num_listed, is_truncated = client.label_releases(BITE_ID)

        self.assertEqual(num_listed, 3)
        self.assertFalse(is_truncated)
        self.assertEqual(len(releases), 2)

        release = next(item for item in releases if item.title == "Science Gardens")

        # The digital edition's tracklist is the one wanted, but the year is the original one
        self.assertEqual(release.release_id, "musicbrainz:digital")
        self.assertEqual(release.year, 2019)
        self.assertEqual(sorted(release.catalog_numbers), ["BITE06", "BITE06D"])
        self.assertEqual(release.kind, "ep")
        self.assertEqual(release.track_count, 4)

    def test_label_releases_reads_all_pages(self):

        first_page = [mb_release(f"a{index}", f"Title {index}") for index in range(100)]
        second_page = [mb_release("last", "Last")]
        client = self.client([("offset=100", {"release-count": 101, "releases": second_page}),
                              ("offset=0", {"release-count": 101, "releases": first_page})])

        releases, num_listed, _is_truncated = client.label_releases(BITE_ID)

        self.assertEqual(num_listed, 101)
        self.assertEqual(len(releases), 101)

    def test_bootlegs_are_left_out(self):

        client = self.client([("ws/2/release", {"release-count": 1, "releases": [
            mb_release("a", "Fake", status="Bootleg")
        ]})])
        self.assertEqual(client.label_releases(BITE_ID)[0], [])

    def test_release_kinds(self):

        for kind, secondary, expected in (
            ("Album", [], "album"), ("EP", [], "ep"), ("Single", [], "single"),
            ("Album", ["Compilation"], "compilation"),
            ("Album", ["Live"], "live"), ("Album", ["DJ-mix"], "mix"), ("Broadcast", [], "other"), (None, [], "other")
        ):
            release = MusicBrainzClient._parse_release(  # pylint: disable=protected-access
                mb_release("a", "T", kind=kind, secondary=secondary)
            )
            self.assertEqual(release.kind, expected, (kind, secondary))

    def test_release_details(self):

        data = mb_release("a", "Panorama Bar 01", artist="Cassy", catno="ostgut CD02", kind="Album",
                          secondary=["DJ-mix"])
        data["media"] = [{"position": 1, "format": "CD", "tracks": [
            {"title": "Blood on My Hands", "number": "1", "position": 1, "length": 187000},
            {"title": "No Length", "number": "2", "position": 2, "recording": {"length": 61400}}
        ]}]
        client = self.client([("ws/2/release/a", data)])
        release = client.release_details("a")

        self.assertEqual(release.artist, "Cassy")
        self.assertEqual(release.catalog_numbers, ["ostgut CD02"])
        self.assertEqual(release.track_count, 2)
        self.assertEqual((release.tracks[0].title, release.tracks[0].length), ("Blood on My Hands", 187))
        self.assertEqual(release.tracks[1].length, 61)

    def test_several_artists(self):

        data = mb_release("a", "Mala Vida")
        data["artist-credit"] = [{"name": "Unhuman", "joinphrase": " + "}, {"name": "Petra Flurr", "joinphrase": ""}]
        release = MusicBrainzClient._parse_release(data)  # pylint: disable=protected-access

        self.assertEqual(release.artist, "Unhuman + Petra Flurr")
        self.assertEqual(release.primary_artist, "Unhuman")

    @staticmethod
    def recording(title, releases, score=100, artist="Kerri Chandler"):
        return {"title": title, "score": score, "artist-credit": [{"name": artist}], "releases": releases}

    @staticmethod
    def track_release(release_id, kind="Album", secondary=None, date="2000", status="Official", group=None):

        return {"id": release_id, "title": release_id, "date": date, "status": status, "track-count": 5,
                "release-group": {"id": group or release_id, "primary-type": kind, "secondary-types": secondary or []}}

    def test_track_albums_prefers_exact_title_then_album(self):

        client = self.client([("ws/2/recording", {"recordings": [
            self.recording("Atmosphere (Kaval edit)", [self.track_release("remix-album")]),
            self.recording("Atmosphere", [
                self.track_release("comp", secondary=["Compilation"], date="1995"),
                self.track_release("single", kind="Single", date="1998"),
                self.track_release("album", date="2005")
            ])
        ]})])
        albums = client.find_track_albums("Kerri Chandler", "Atmosphere")

        self.assertEqual([album["title"] for album in albums], ["album", "single", "comp", "remix-album"])

    def test_track_albums_one_entry_per_release_group(self):

        client = self.client([("ws/2/recording", {"recordings": [self.recording("Atmosphere", [
            self.track_release("reissue", group="g", date="2015"),
            self.track_release("original", group="g", date="1998"),
            self.track_release("promo", group="g", date="1997", status="Promotion")
        ])]})])
        albums = client.find_track_albums("Kerri Chandler", "Atmosphere")

        self.assertEqual([album["title"] for album in albums], ["original"])

    def test_track_albums_ignores_other_artists_and_low_scores(self):

        client = self.client([("ws/2/recording", {"recordings": [
            self.recording("Atmosphere", [self.track_release("other")], artist="Joy Division"),
            self.recording("Atmosphere", [self.track_release("weak")], score=30)
        ]})])
        self.assertEqual(client.find_track_albums("Kerri Chandler", "Atmosphere"), [])


class DiscogsTest(TestCase):

    opener = None

    def client(self, routes):

        http, self.opener, _sleeps = make_http(routes)
        return DiscogsClient(http)

    def test_token_is_sent_as_a_header(self):

        client = DiscogsClient(token="secret")

        self.assertEqual(client.http.headers["Authorization"], "Discogs token=secret")
        self.assertLess(client.http.min_interval, DiscogsClient().http.min_interval)

    def test_clean_name(self):

        self.assertEqual(DiscogsClient.clean_name("Barker (2)"), "Barker")
        self.assertEqual(DiscogsClient.clean_name("Dettmann* | Klock*"), "Dettmann, Klock")

    def test_find_labels_puts_exact_names_first(self):

        client = self.client([("database/search", {"results": [
            {"id": 76625, "title": "Bite it!"}, {"id": 40459, "title": "Bite"}, {"id": 3091, "title": "BITE Records"}
        ]})])
        labels = client.find_labels("bite")

        self.assertEqual(labels[0]["label_id"], "discogs:40459")
        self.assertEqual(len(labels), 3)

    ARTIST_LISTING = {"pagination": {"pages": 1}, "releases": [
        {"id": 100, "type": "master", "main_release": 555, "role": "Main", "artist": "Phase Fatale (2)",
         "title": "Altars", "year": 2024},
        {"id": 100, "type": "release", "role": "Main", "artist": "Phase Fatale", "title": "Live", "year": 2020,
         "format": "File, FLAC, Mixed"},
        {"id": 200, "type": "release", "role": "Main", "artist": "Phase Fatale", "title": "Bootleg", "year": 2021,
         "format": "Vinyl, Unofficial Release"},
        {"id": 300, "type": "master", "main_release": 777, "role": "Remix", "artist": "Somebody", "title": "Theirs",
         "year": 2016},
        {"id": 400, "type": "release", "role": "TrackAppearance", "artist": "Various", "title": "Comp", "year": 2019,
         "format": "File, FLAC, Comp"},
        {"id": 500, "type": "release", "role": "Producer", "artist": "Other", "title": "Produced", "year": 2019,
         "format": "File, FLAC"}
    ]}

    def test_artist_releases_are_only_their_own(self):

        client = self.client([("artists/9/releases", self.ARTIST_LISTING)])

        releases, total, is_truncated = client.artist_releases("9")
        ids = {release.title: release.release_id for release in releases}

        self.assertEqual(ids, {"Altars": "discogs:555", "Live": "discogs:100"})
        self.assertEqual((total, is_truncated), (2, False))
        self.assertTrue(all(release.listed_on_label is None for release in releases))

    def test_artist_releases_can_include_appearances(self):

        client = self.client([("artists/9/releases", self.ARTIST_LISTING)])

        releases, _total, _is_truncated = client.artist_releases("9", include_appearances=True)

        self.assertEqual(sorted(release.title for release in releases), ["Altars", "Comp", "Live", "Theirs"])

    def test_find_artists_puts_exact_names_first(self):

        client = self.client([("database/search", {"results": [
            {"id": 1, "title": "Phase Fatale Tribute"}, {"id": 2, "title": "Phase Fatale (2)"}
        ]})])

        artists = client.find_artists("Phase Fatale")

        self.assertEqual(artists[0]["artist_id"], "discogs:2")

    def test_label_releases_cleans_up_the_listing(self):

        client = self.client([("labels/178535/releases", {"pagination": {"pages": 1}, "releases": [
            {"id": 629804, "artist": "Dettmann* | Klock*", "title": "Dawning", "catno": "o-ton01", "format": '12"',
             "year": 2006},
            # The same release is listed once per catalog number
            {"id": 629804, "artist": "Dettmann* | Klock*", "title": "Dawning", "catno": "o-ton01", "format": '12"',
             "year": 2006},
            # Editions of a release have no shared ID in this listing
            {"id": 111, "artist": "Dettmann | Klock", "title": "Dawning", "catno": "o-ton01D", "format": "File, MP3",
             "year": 2007},
            {"id": 222, "artist": "Dettmann | Klock", "title": "Dawning", "catno": "TEST", "format": '12", TP',
             "year": 2005},
            {"id": 772763, "artist": "Cassy", "title": "Panorama Bar 01", "catno": "ostgut CD02",
             "format": "CD, Comp, Mixed", "year": 2006}
        ]})])
        releases, num_listed, _is_truncated = client.label_releases("178535")

        self.assertEqual(num_listed, 4)
        self.assertEqual(len(releases), 2)

        dawning = next(item for item in releases if item.title == "Dawning")

        self.assertEqual(dawning.release_id, "discogs:111")
        self.assertEqual(dawning.artist, "Dettmann, Klock")
        self.assertEqual(dawning.primary_artist, "Dettmann")
        self.assertEqual(dawning.year, 2005)
        self.assertIn("o-ton01", dawning.catalog_numbers)

        panorama = next(item for item in releases if item.title == "Panorama Bar 01")
        self.assertEqual(panorama.kind, "compilation")

    def test_release_details(self):

        client = self.client([("releases/772763", {
            "title": "Panorama Bar 01", "year": 2006, "artists": [{"name": "Cassy (3)", "anv": ""}],
            "labels": [{"id": 178535, "name": "Ostgut Ton", "catno": "ostgut CD02"}],
            "formats": [{"name": "CD", "descriptions": ["Compilation", "Mixed"]}],
            "tracklist": [
                {"position": "1", "type_": "track", "title": "One", "duration": "5:52"},
                {"position": "", "type_": "heading", "title": "Side B"},
                {"position": "", "type_": "index", "title": "Suite", "sub_tracks": [
                    {"position": "2a", "type_": "track", "title": "Two", "duration": "1:02:03"},
                    {"position": "2b", "type_": "track", "title": "Three", "duration": ""}
                ]}
            ]
        })])
        release = client.release_details("772763")

        self.assertEqual(release.artist, "Cassy")
        self.assertEqual(release.kind, "compilation")
        self.assertEqual(release.label_ids, ["178535"])
        self.assertEqual([track.title for track in release.tracks], ["One", "Two", "Three"])
        self.assertEqual([track.length for track in release.tracks], [352, 3723, 0])

    def test_find_albums_parses_search_results(self):

        client = self.client([("database/search", {"results": [
            {"id": 5, "type": "release", "title": "New Frames - Altars", "year": "2020",
             "format": ["Vinyl", '12"', "EP"], "catno": "BITE016", "label": ["BITE"]},
            {"id": 6, "type": "release", "title": "New Frames - Altars", "year": "2021",
             "format": ["File", "FLAC"], "catno": "BITE016D"},
            {"id": 7, "type": "release", "title": "Other Band - Altars", "year": "1999", "format": ["CD"]},
            {"id": 8, "type": "release", "title": "No Separator", "year": "1999", "format": ["CD"]}
        ]})])

        albums = client.find_albums("New Frames", "Altars")

        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0].release_id, "discogs:6")  # The file edition is preferred
        self.assertEqual(albums[0].artist, "New Frames")
        self.assertEqual(albums[0].title, "Altars")
        self.assertEqual(albums[0].year, 2020)
        self.assertEqual(albums[0].catalog_numbers, ["BITE016D", "BITE016"])
        self.assertEqual(albums[0].labels, ["BITE"])  # Merged from the other edition
        self.assertIn("release_title=Altars", self.opener.requests[0].full_url)
        self.assertIn("artist=New+Frames", self.opener.requests[0].full_url)

    def test_find_albums_of_various_artists_searches_by_title_only(self):

        client = self.client([("database/search", {"results": [
            {"id": 5, "type": "release", "title": "Various - Self-Aware III", "year": "2020", "format": ["File"]}
        ]})])

        albums = client.find_albums("Various Artists", "Self-Aware III")

        self.assertEqual([album.release_id for album in albums], ["discogs:5"])
        self.assertNotIn("artist=", self.opener.requests[0].full_url)


class AlbumMatchTest(TestCase):

    def test_title_ranks(self):

        self.assertEqual(album_title_rank("Tocsin", "tocsin"), 0)
        self.assertEqual(album_title_rank("Separate Ways (Original Mix)", "Separate Ways"), 1)
        self.assertEqual(album_title_rank("Mass Control - EP", "Mass Control"), 1)
        self.assertEqual(album_title_rank("Tocsin", "Tocsin Live"), 2)
        self.assertIsNone(album_title_rank("Tocsin", "Viva"))
        self.assertIsNone(album_title_rank("", "Viva"))

    def test_relaxed_names(self):

        self.assertEqual(_relaxed_name("SDH (Semiotics Department of Heteronyms)"), "SDH")
        self.assertEqual(_relaxed_name("Separate Ways (Original Mix)"), "Separate Ways")
        self.assertEqual(_relaxed_name("Mass Control - EP"), "Mass Control")
        self.assertEqual(_relaxed_name("Border Country: The Remixes"), "Border Country: The Remixes")
        self.assertEqual(_relaxed_name(""), "")

    def test_matches_are_ranked_by_title_then_kind_then_age(self):

        releases = [
            Release("discogs:1", "discogs", "Tham, Blackworks", "Separate Ways", kind="single", year=2020),
            Release("discogs:2", "discogs", "Tham", "Separate Ways", kind="ep", year=2021),
            Release("discogs:3", "discogs", "Tham", "Separate Ways (Remixes)", kind="ep", year=2019),
            Release("discogs:4", "discogs", "Someone Else", "Separate Ways", kind="album", year=2000),
            Release("discogs:5", "discogs", "Various", "Separate Ways", kind="compilation", year=2000)
        ]

        ranked = rank_album_matches(releases, "Tham", "Separate Ways")

        self.assertEqual([release.release_id for release in ranked],
                         ["discogs:2", "discogs:1", "discogs:5", "discogs:3"])


class FilterTest(TestCase):

    releases = [
        Release("a:1", "a", "New Frames", "Scherben", year=2023, kind="album", catalog_numbers=["BITE030"]),
        Release("a:2", "a", "Reka", "Diving", year=2018, kind="ep", catalog_numbers=["BITE05"]),
        Release("a:3", "a", "Cassy", "Panorama", year=None, kind="mix", catalog_numbers=["ostgut CD02"])
    ]

    def titles(self, **filters):
        return [release.title for release in filter_releases(self.releases, **filters)]

    def test_no_filters(self):
        self.assertEqual(len(self.titles()), 3)

    def test_kinds(self):

        self.assertEqual(self.titles(kinds=["album", "ep"]), ["Scherben", "Diving"])
        self.assertEqual(self.titles(exclude_kinds=["mix"]), ["Scherben", "Diving"])

    def test_unknown_kind(self):

        with self.assertRaisesRegex(ValueError, "Unknown release kind"):
            self.titles(kinds=["lp"])

    def test_years(self):

        self.assertEqual(self.titles(year_from=2020), ["Scherben"])
        self.assertEqual(self.titles(year_to=2020), ["Diving"])

    def test_artist(self):
        self.assertEqual(self.titles(artist="new frames"), ["Scherben"])

    def test_catalog_prefix(self):

        self.assertEqual(self.titles(catalog_prefix="ostgut"), ["Panorama"])
        self.assertEqual(self.titles(catalog_prefix="bite 03"), ["Scherben"])


class MergeCatalogsTest(TestCase):

    @staticmethod
    def release(artist, title, numbers=(), year=None, release_id=None):

        return Release(release_id or f"a:{artist}{title}", "a", artist, title, catalog_numbers=list(numbers),
                       year=year)

    def test_same_release_written_differently_is_not_added(self):

        merged, num_added = merge_catalogs(
            [self.release("Phase Fatale", "Climbing Silver EP"), self.release("Phase Fatale", "Issue Nº Ten")],
            [self.release("Phase Fatale", "Climbing Silver"), self.release("Phase Fatale", "Issue N° Ten")]
        )

        self.assertEqual(num_added, 0)
        self.assertEqual(len(merged), 2)

    def test_adds_what_only_the_second_one_knows(self):

        merged, num_added = merge_catalogs(
            [self.release("Phase Fatale", "Altars", ["BITE034"])],
            [self.release("Sorcery", "Look, I Made It Go Away!", ["BITE038"])]
        )

        self.assertEqual(num_added, 1)
        self.assertEqual([item.title for item in merged], ["Altars", "Look, I Made It Go Away!"])

    def test_same_catalog_number_is_the_same_release(self):
        """Written differently, and with a different title in each catalog."""

        first = self.release("Barker", "Utility", ["OSTGUTCD46"], year=2019)
        merged, num_added = merge_catalogs([first], [
            self.release("Barker", "Utility LP", ["ostgut CD 046"], year=2018)
        ])

        self.assertEqual((len(merged), num_added), (1, 0))
        self.assertEqual(merged[0].year, 2018)
        self.assertEqual(merged[0].catalog_numbers, ["OSTGUTCD46", "ostgut CD 046"])

    def test_same_names_are_the_same_release(self):

        for first, second in (
            (("Dettmann | Wiedemann", "Masse Remixes II"), ("Wiedemann & Dettmann", "Masse Remixes II")),
            (("Various Artists", "Panorama Bar 04"), ("Various", "Panorama Bar 04")),
            (("Björk", "Vespertine!"), ("Bjork", "Vespertine"))
        ):
            merged, _num_added = merge_catalogs([self.release(*first)], [self.release(*second)])
            self.assertEqual(len(merged), 1, first)

    def test_same_title_by_another_artist_is_another_release(self):

        merged, num_added = merge_catalogs([self.release("A", "Remixes")], [self.release("B", "Remixes")])
        self.assertEqual((len(merged), num_added), (2, 1))

    def test_first_catalog_entry_is_kept(self):

        first = self.release("A", "B", release_id="musicbrainz:1")
        merged, _num_added = merge_catalogs([first], [self.release("A", "B", release_id="discogs:1")])

        self.assertEqual(merged[0].release_id, "musicbrainz:1")

    def test_duplicates_within_the_second_one(self):

        merged, num_added = merge_catalogs([], [self.release("A", "B", ["X1"]), self.release("A", "C", ["X1"])])
        self.assertEqual((len(merged), num_added), (1, 1))

    def test_label_ids_of_both_catalogs(self):

        catalog = FacadeTest.catalog([("ws/2/release", {"release-count": 1, "releases": [
            mb_release("a", "Altars", artist="Phase Fatale", catno="BITE034")
        ]})], [("labels/40459/releases", {"pagination": {"pages": 1}, "releases": [
            {"id": 1, "artist": "Phase Fatale", "title": "Altars", "catno": "BITE034", "format": "File", "year": 2024},
            {"id": 2, "artist": "Sorcery", "title": "Go Away", "catno": "BITE038", "format": "File", "year": 2026}
        ]})])
        result = catalog.label_releases(f"musicbrainz:{BITE_ID}", also_label_id="discogs:40459")

        self.assertEqual(result["num_releases"], 2)
        self.assertEqual(result["num_added_from_second_catalog"], 1)
        self.assertEqual([item.release_id for item in result["releases"]], ["discogs:2", "musicbrainz:a"])

        # What came from Discogs gets checked against the label there, even in a job on the MusicBrainz ID
        discogs_release = result["releases"][0]
        discogs_release.label_ids = ["309"]
        self.assertFalse(Catalog.is_on_label(discogs_release, f"musicbrainz:{BITE_ID}"))
        discogs_release.label_ids = ["40459"]
        self.assertTrue(Catalog.is_on_label(discogs_release, f"musicbrainz:{BITE_ID}"))


class FacadeTest(TestCase):

    @staticmethod
    def catalog(mb_routes, discogs_routes=()):
        return Catalog(MusicBrainzClient(make_http(mb_routes)[0]), DiscogsClient(make_http(list(discogs_routes))[0]))

    def test_invalid_ids(self):

        catalog = self.catalog([])

        for bad_id in ("", "178535", "spotify:1", "discogs:"):
            with self.assertRaises(ValueError):
                catalog.release_details(bad_id)

    def test_routes_by_prefix(self):

        catalog = self.catalog([("ws/2/release/abc", mb_release("abc", "From MusicBrainz"))], [
            ("releases/123", {"title": "From Discogs", "artists": [], "labels": [], "tracklist": []})
        ])

        self.assertEqual(catalog.release_details("musicbrainz:abc").title, "From MusicBrainz")
        self.assertEqual(catalog.release_details("discogs:123").title, "From Discogs")

    def test_one_catalog_failing_is_only_a_warning(self):

        catalog = self.catalog([("ws/2/label", [http_error(400)])], [
            ("database/search", {"results": [{"id": 1, "title": "Bite"}]})
        ])
        result = catalog.find_labels("Bite")

        self.assertEqual([label["label_id"] for label in result["labels"]], ["discogs:1"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_all_catalogs_failing_is_an_error(self):

        catalog = self.catalog([("ws/2/label", [http_error(400)])], [("database/search", [http_error(400)])])

        with self.assertRaises(CatalogError):
            catalog.find_labels("Bite")

    def test_empty_name(self):

        with self.assertRaises(ValueError):
            self.catalog([]).find_labels("  ")

    def test_artist_releases_combines_catalogs(self):

        musicbrainz = MusicBrainzClient(make_http([("ws/2/release", {"release-count": 1, "releases": [
            mb_release("r1", "Altars", artist="Phase Fatale", date="2024-01-01")
        ]})])[0])
        discogs = DiscogsClient(make_http([("artists/9/releases", {"pagination": {"pages": 1}, "releases": [
            {"id": 1, "type": "master", "main_release": 2, "role": "Main", "artist": "Phase Fatale",
             "title": "Altars", "year": 2024},
            {"id": 3, "type": "master", "main_release": 4, "role": "Main", "artist": "Phase Fatale",
             "title": "Skyscraper", "year": 2014}
        ]})])[0])
        catalog = Catalog(musicbrainz, discogs)

        listing = catalog.artist_releases("musicbrainz:a1", also_artist_id="discogs:9")

        self.assertEqual([release.title for release in listing["releases"]], ["Altars", "Skyscraper"])
        self.assertEqual(listing["num_added_from_second_catalog"], 1)
        self.assertEqual(listing["artist_id"], "musicbrainz:a1")
        self.assertTrue(Catalog.is_on_label(listing["releases"][1], None))

    def test_label_releases_newest_first(self):

        catalog = self.catalog([("ws/2/release", {"release-count": 2, "releases": [
            mb_release("old", "Old", date="2018"), mb_release("new", "New", date="2023")
        ]})])
        result = catalog.label_releases(f"musicbrainz:{BITE_ID}")

        self.assertEqual([release.title for release in result["releases"]], ["New", "Old"])

    def test_is_on_label(self):

        release = Release("discogs:1", "discogs", "A", "B", label_ids=["309"])

        self.assertFalse(Catalog.is_on_label(release, "discogs:178535"))
        self.assertTrue(Catalog.is_on_label(release, "discogs:309"))
        self.assertTrue(Catalog.is_on_label(release, f"musicbrainz:{BITE_ID}"))
        self.assertTrue(Catalog.is_on_label(Release("discogs:1", "discogs", "A", "B"), "discogs:178535"))

    def test_find_albums_combines_catalogs(self):

        catalog = self.catalog(
            [("ws/2/release", {"releases": [mb_release("r1", "Altars")]})],
            [("database/search", {"results": [
                {"id": 1, "type": "release", "title": "New Frames - Altars", "year": "2020", "format": ["File"]},
                {"id": 2, "type": "release", "title": "New Frames - Altars (Remixes)", "year": "2021",
                 "format": ["File"]}
            ]})]
        )

        result = catalog.find_albums("New Frames", "Altars")

        self.assertEqual([album.release_id for album in result["albums"]], ["musicbrainz:r1", "discogs:2"])
        self.assertEqual(result["warnings"], [])

    def test_find_albums_with_one_catalog_down(self):

        catalog = self.catalog([("ws/2/release", [http_error(400)])], [("database/search", {"results": [
            {"id": 1, "type": "release", "title": "New Frames - Altars", "year": "2020", "format": ["File"]}
        ]})])

        result = catalog.find_albums("New Frames", "Altars")

        self.assertEqual([album.release_id for album in result["albums"]], ["discogs:1"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_find_albums_with_both_catalogs_down(self):

        catalog = self.catalog([("ws/2/release", [http_error(400)])], [("database/search", [http_error(400)])])

        with self.assertRaises(CatalogError):
            catalog.find_albums("New Frames", "Altars")

        with self.assertRaises(ValueError):
            catalog.find_albums("New Frames", " ")

    def test_find_albums_asks_again_without_remarks_in_parentheses(self):

        mb_http, mb_opener, _sleeps = make_http([
            ("artist%3A%22SDH+%28Semiotics", {"releases": []}),
            ("ws/2/release", {"releases": [mb_release("r1", "Semiotics Department of Heteronyms", artist="SDH")]})
        ])
        discogs_http, discogs_opener, _sleeps = make_http([
            ("artist=SDH+%28Semiotics", {"results": []}),
            ("database/search", {"results": [
                {"id": 1, "type": "release", "title": "SDH (4) - Semiotics Department Of Heteronyms", "year": "2018",
                 "format": ["File"]}
            ]})
        ])
        catalog = Catalog(MusicBrainzClient(mb_http), DiscogsClient(discogs_http))

        result = catalog.find_albums("SDH (Semiotics Department of Heteronyms)", "Semiotics Department Of Heteronyms")

        self.assertEqual([album.release_id for album in result["albums"]], ["musicbrainz:r1"])
        self.assertEqual(len(mb_opener.requests), 2)
        self.assertEqual(len(discogs_opener.requests), 2)
        self.assertIn("artist%3A%22SDH%22", mb_opener.requests[1].full_url)
        self.assertTrue(discogs_opener.requests[1].full_url.endswith("&artist=SDH"))
