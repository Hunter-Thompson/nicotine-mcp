# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest import TestCase

from nicotine_mcp.matching import Release
from nicotine_mcp.matching import Track
from nicotine_mcp.matching import Verdict
from nicotine_mcp.matching import assess_album_folder
from nicotine_mcp.matching import build_search_queries
from nicotine_mcp.matching import catalog_number_in_text
from nicotine_mcp.matching import match_folder
from nicotine_mcp.matching import normalize_text
from nicotine_mcp.matching import select_remaining_files
from nicotine_mcp.matching import split_file_name
from nicotine_mcp.matching import title_similarity


def make_release(titles=("Paradise Engineering", "Utility", "Look How Hard I've Tried"), artist="Barker",
                 title="Utility", catalog_numbers=("OSTGUTCD46",), lengths=None, **kwargs):

    tracks = None

    if titles is not None:
        lengths = lengths or [0] * len(titles)
        tracks = [Track(item, lengths[index], str(index + 1)) for index, item in enumerate(titles)]

    return Release(release_id="musicbrainz:1", source="musicbrainz", artist=artist, title=title,
                   catalog_numbers=list(catalog_numbers), tracks=tracks, **kwargs)


class NormalizeTest(TestCase):

    def test_accents_and_punctuation(self):

        self.assertEqual(normalize_text("Fünfzehn + 1"), "funfzehn 1")
        self.assertEqual(normalize_text("Björk & Friends!"), "bjork and friends")
        self.assertEqual(normalize_text("Ostgut_Ton"), "ostgut ton")
        self.assertEqual(normalize_text(None), "")

    def test_stylized_artist_names(self):
        self.assertEqual(normalize_text("SΛRIN"), "sarin")


class FileNameTest(TestCase):

    def test_track_numbers(self):

        for basename, stripped, position in (
            ("01 - Title.flac", "Title", "1"),
            ("01. Title.flac", "Title", "1"),
            ("1-05 Title.flac", "Title", "5"),
            ("A1 - Title.mp3", "Title", "A1"),
            ("b2. Title.mp3", "Title", "B2"),
            ("track01.flac", "", "1"),
            ("Track 12.flac", "", "12"),
            ("Title.flac", "Title", ""),
            ("Artist - Title.flac", "Artist - Title", "")
        ):
            _stem, result_stripped, result_position = split_file_name(basename)
            self.assertEqual((result_stripped, result_position), (stripped, position), basename)

    def test_title_with_number_is_kept_for_comparison(self):
        self.assertGreater(title_similarity("99 Problems", "99 Problems.flac"), 0.9)

    def test_title_similarity(self):

        self.assertGreater(title_similarity("Utility", "02 - Barker - Utility.flac"), 0.9)
        self.assertGreater(title_similarity("Track (Original Mix)", "01 Track.flac"), 0.72)
        self.assertLess(title_similarity("Utility", "02 - Something Else.flac"), 0.72)

    def test_short_titles_need_an_exact_match(self):

        self.assertGreater(title_similarity("II", "02 - II.flac"), 0.9)
        self.assertEqual(title_similarity("II", "02 - Part II of the Story.flac"), 0.0)


class CatalogNumberTest(TestCase):

    def test_ignores_separators_and_leading_zeros(self):

        self.assertTrue(catalog_number_in_text("OSTGUTCD46", "2019 Utility {Ostgut Ton OSTGUTCD46}"))
        self.assertTrue(catalog_number_in_text("ostgut CD46", "Barker-Utility-OSTGUTCD046-WEB"))
        self.assertTrue(catalog_number_in_text("BITE028", "[bite-28] Mala Vida"))
        self.assertTrue(catalog_number_in_text("BITE28", "[BITE028] Mala Vida"))

    def test_other_numbers_do_not_match(self):

        self.assertFalse(catalog_number_in_text("BITE028", "[BITE0281] Mala Vida"))
        self.assertFalse(catalog_number_in_text("BITE028", "[BITE029] Mala Vida"))

    def test_bare_numbers_are_not_catalog_numbers(self):
        self.assertFalse(catalog_number_in_text("2019", "Music\\2019 Utility"))


class MatchFolderTest(TestCase):

    def test_complete_album(self):

        match = match_folder(make_release(), "Music\\Barker\\2019 Utility", [
            ("01 - Paradise Engineering.flac", 300), ("02 - Utility.flac", 300),
            ("03 - Look How Hard I've Tried.flac", 300), ("cover.jpg", 0), ("rip.log", 0)
        ])

        self.assertEqual(match.verdict, Verdict.COMPLETE)
        self.assertEqual(match.num_matched, 3)
        self.assertEqual(match.missing_tracks, [])
        self.assertTrue(match.is_acceptable)

    def test_missing_tracks(self):

        release = make_release(titles=("One", "Two Words", "Three Things", "Four Corners"))
        match = match_folder(release, "Music\\Barker\\Utility", [("01 One.flac", 0), ("02 Two Words.flac", 0)])

        self.assertEqual(match.verdict, Verdict.PARTIAL)
        self.assertEqual(match.missing_tracks, ["Three Things", "Four Corners"])
        self.assertFalse(match.is_acceptable)

    def test_different_album_with_same_title(self):
        """The search matched 'bite', but this is the wrong release."""

        release = make_release(titles=("Altars", "Burial", "Ritual"), artist="Phase Fatale", title="Bite",
                               catalog_numbers=())
        match = match_folder(release, "Music\\Girlschool\\1988 - Take A Bite", [
            ("01 - Tear It Up.flac", 200), ("02 - Fox on the Run.flac", 200), ("03 - Love & Lust.flac", 200)
        ])

        self.assertEqual(match.verdict, Verdict.MISMATCH)

    def test_same_title_by_another_artist_is_rejected(self):
        """A single called 'Atmosphere' has one track, also called 'Atmosphere'."""

        release = make_release(titles=("Atmosphere",), artist="Kerri Chandler", title="Atmosphere", catalog_numbers=())
        match = match_folder(release, "Music\\Joy Division\\Atmosphere", [("01 - Atmosphere.flac", 250)])

        self.assertEqual(match.verdict, Verdict.PARTIAL)

    def test_dump_folder_with_all_tracks_is_rejected(self):
        """Every track is in there, among many unrelated files."""

        files = [("Barker - Utility.flac", 0), ("Barker - Paradise Engineering.flac", 0),
                 ("Barker - Look How Hard I've Tried.flac", 0)]
        files += [(f"Someone Else - Track {index}.flac", 0) for index in range(40)]

        match = match_folder(make_release(), "Music\\Misc", files)

        self.assertEqual(match.verdict, Verdict.PARTIAL)

    def test_track_lengths_tell_apart_similar_titles(self):

        release = make_release(titles=("Utility",), lengths=[300], catalog_numbers=())
        right = match_folder(release, "Barker\\Utility", [("01 Utility.flac", 301)])
        wrong = match_folder(release, "Barker\\Utility", [("01 Utility.flac", 620)])

        self.assertEqual(right.verdict, Verdict.COMPLETE)
        self.assertEqual(right.length_mismatches, 0)
        self.assertEqual(wrong.verdict, Verdict.MISMATCH)

    def test_remix_ep_pairs_each_title_with_its_own_file(self):

        release = make_release(titles=("Foo", "Foo (Someone Remix)", "Foo (Other Remix)"), title="Foo",
                               catalog_numbers=())
        match = match_folder(release, "Barker - Foo", [
            ("01 Foo (Someone Remix).flac", 0), ("02 Foo (Other Remix).flac", 0), ("03 Foo.flac", 0)
        ])

        self.assertEqual(match.verdict, Verdict.COMPLETE)
        self.assertEqual(match.num_matched, 3)

    def test_nondescript_names_with_matching_lengths(self):

        release = make_release(titles=("Paradise Engineering", "Look How Hard I've Tried"), lengths=[300, 410])
        match = match_folder(release, "Barker\\Utility", [("track01.flac", 301), ("track02.flac", 410)])

        self.assertEqual(match.verdict, Verdict.COMPLETE)
        self.assertEqual(match.num_matched, 2)

    def test_unusual_names_with_exact_count_in_named_folder(self):

        release = make_release(titles=("Alpha", "Beta", "Gamma", "Delta", "Epsilon"), catalog_numbers=())
        match = match_folder(release, "Barker - Utility", [
            ("1 Alpha.flac", 0), ("2 Beta.flac", 0), ("3 Gamma.flac", 0), ("4 Delta.flac", 0),
            ("5 Epsylon Mix 2.flac", 0)
        ])

        self.assertEqual(match.verdict, Verdict.COMPLETE)

    def test_catalog_number_identifies_a_folder(self):

        release = make_release(titles=("Alpha", "Beta"), artist="Someone Unknown", title="Different Name",
                               catalog_numbers=("BITE028",))
        match = match_folder(release, "Music\\[BITE028]", [("01 Alpha.flac", 0), ("02 Beta.flac", 0)])

        self.assertEqual(match.verdict, Verdict.COMPLETE)
        self.assertTrue(match.catalog_number_in_path)

    def test_no_audio_files(self):
        self.assertEqual(match_folder(make_release(), "Barker\\Utility", [("cover.jpg", 0)]).verdict, Verdict.MISMATCH)


class MatchWithoutTracklistTest(TestCase):

    def test_named_after_release(self):

        match = match_folder(make_release(titles=None, track_count=3), "Barker\\Utility", [
            ("a.flac", 0), ("b.flac", 0), ("c.flac", 0)
        ])

        self.assertEqual(match.verdict, Verdict.PROBABLE)
        self.assertTrue(match.is_acceptable)

    def test_wrong_number_of_files(self):

        match = match_folder(make_release(titles=None, track_count=9), "Barker\\Utility", [("a.flac", 0)])
        self.assertEqual(match.verdict, Verdict.PARTIAL)

    def test_not_named_after_release(self):

        match = match_folder(make_release(titles=None, catalog_numbers=()), "Music\\Utility", [("a.flac", 0)])
        self.assertEqual(match.verdict, Verdict.MISMATCH)


class RemainingFilesTest(TestCase):

    def test_only_missing_tracks(self):

        remaining = select_remaining_files(
            make_release(), ["01 - Paradise Engineering.flac"],
            ["1. Paradise Engineering.mp3", "2. Utility.mp3", "3. Look How Hard I've Tried.mp3", "cover.jpg"]
        )
        self.assertEqual(remaining, ["2. Utility.mp3", "3. Look How Hard I've Tried.mp3"])

    def test_nothing_missing(self):

        remaining = select_remaining_files(
            make_release(), ["Paradise Engineering.flac", "Utility.flac", "Look How Hard I've Tried.flac"],
            ["1. Paradise Engineering.mp3", "2. Utility.mp3", "3. Look How Hard I've Tried.mp3"]
        )
        self.assertEqual(remaining, [])

    def test_copy_lacking_a_track(self):

        remaining = select_remaining_files(make_release(), ["Paradise Engineering.flac"], ["2. Utility.mp3"])
        self.assertEqual(remaining, ["2. Utility.mp3"])

    def test_without_tracklist(self):

        remaining = select_remaining_files(
            make_release(titles=None), ["01 - One.flac"], ["01 - One.flac", "02 - Two.flac"]
        )
        self.assertEqual(remaining, ["02 - Two.flac"])


class AssessAlbumFolderTest(TestCase):

    @staticmethod
    def files(names):
        return [(name, 0) for name in names]

    def test_album(self):

        result = assess_album_folder("Music\\Barker - Utility", self.files(
            ["01 One.flac", "02 Two.flac", "03 Three.flac", "cover.jpg"]
        ))
        self.assertTrue(result["is_album"], result)

    def test_vinyl_positions(self):

        result = assess_album_folder("Music\\Someone - EP", self.files(["A1 One.mp3", "A2 Two.mp3", "B1 Three.mp3"]))
        self.assertTrue(result["is_album"], result)

    def test_multiple_discs_in_one_folder(self):

        result = assess_album_folder("Music\\Comp", self.files(
            ["1-01 One.flac", "1-02 Two.flac", "2-01 Three.flac", "2-02 Four.flac"]
        ))
        self.assertTrue(result["is_album"], result)

    def test_generic_folder(self):

        result = assess_album_folder("Music\\Misc", self.files(["01 One.flac", "02 Two.flac"]))
        self.assertFalse(result["is_album"])

    def test_unnumbered_files(self):

        result = assess_album_folder("Music\\Mixed Bag", self.files(
            ["Artist - One.mp3", "Other - Two.mp3", "Third - Three.mp3"]
        ))
        self.assertFalse(result["is_album"])

    def test_too_many_files(self):

        result = assess_album_folder("Music\\Everything", self.files([f"{index:03d} Song.mp3" for index in range(90)]))
        self.assertFalse(result["is_album"])

    def test_repeated_numbers_from_merged_albums(self):

        result = assess_album_folder("Music\\Best Of", self.files(
            ["01 One.mp3", "01 Other.mp3", "02 Two.mp3", "02 Another.mp3"]
        ))
        self.assertFalse(result["is_album"])

    def test_single_file(self):
        self.assertFalse(assess_album_folder("Music\\Barker", self.files(["01 One.flac"]))["is_album"])

    def test_mixed_formats(self):

        result = assess_album_folder("Music\\Barker", self.files(["01 One.flac", "02 Two.mp3"]))
        self.assertFalse(result["is_album"])

    def test_gaps(self):

        result = assess_album_folder("Music\\Picks", self.files(["01 One.mp3", "07 Two.mp3", "12 Three.mp3"]))
        self.assertFalse(result["is_album"])


class SearchQueriesTest(TestCase):

    def test_artist_title_then_catalog_number(self):

        self.assertEqual(
            build_search_queries(make_release()), ["Barker Utility", "OSTGUTCD46"]
        )

    def test_several_artists_also_tries_the_first_one(self):

        release = make_release(artist="Unhuman + Petra Flurr", primary_artist="Unhuman", title="Mala Vida",
                               catalog_numbers=("BITE028",))
        self.assertEqual(
            build_search_queries(release), ["Unhuman Petra Flurr Mala Vida", "BITE028", "Unhuman Mala Vida"]
        )

    def test_various_artists(self):

        release = make_release(artist="Various Artists", title="Fünfzehn + 1", catalog_numbers=("ostgut CD 30",))
        self.assertEqual(build_search_queries(release), ["Fünfzehn 1", "ostgut CD 30", "funfzehn 1"])

    def test_punctuation_is_removed(self):

        release = make_release(title="...Im Garten", artist="André Galluzzi", catalog_numbers=())
        self.assertEqual(build_search_queries(release), ["André Galluzzi Im Garten", "andre galluzzi im garten"])

    def test_long_names_are_shortened(self):

        release = make_release(title="one two three four five six seven eight nine ten", catalog_numbers=())
        self.assertLessEqual(len(build_search_queries(release)[0].split()), 8)

    def test_numbers_alone_are_not_queried(self):

        self.assertEqual(build_search_queries(make_release(catalog_numbers=("12345",))), ["Barker Utility"])


class ReleaseTest(TestCase):

    def test_round_trip(self):

        release = make_release(lengths=[1, 2, 3])
        restored = Release.from_dict(release.to_dict())

        self.assertEqual(restored, release)
        self.assertIsInstance(restored.tracks[0], Track)

    def test_summary_has_no_tracklist(self):

        summary = make_release().summary()

        self.assertNotIn("tracks", summary)
        self.assertEqual(summary["track_count"], 3)
