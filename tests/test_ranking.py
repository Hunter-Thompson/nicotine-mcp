# SPDX-FileCopyrightText: 2026 nicotine-mcp contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest import TestCase

from nicotine_mcp.ranking import FileCandidate
from nicotine_mcp.ranking import RankingPreferences
from nicotine_mcp.ranking import check_file_filters
from nicotine_mcp.ranking import get_extension
from nicotine_mcp.ranking import normalize_formats
from nicotine_mcp.ranking import rank_files
from nicotine_mcp.ranking import rank_folders
from nicotine_mcp.ranking import relevance_points
from nicotine_mcp.ranking import score_file
from nicotine_mcp.ranking import tokenize


def make_candidate(username="user", virtual_path="Music\\Artist\\Album\\01 - Track.flac", size=30_000_000,
                   bitrate=0, length=240, sample_rate=None, bit_depth=None, has_free_slot=True, queue_length=0,
                   upload_speed=2_000_000, **kwargs):

    return FileCandidate(
        username=username, virtual_path=virtual_path, size=size, bitrate=bitrate, length=length,
        sample_rate=sample_rate, bit_depth=bit_depth, has_free_slot=has_free_slot, queue_length=queue_length,
        upload_speed=upload_speed, **kwargs
    )


class RankingHelpersTest(TestCase):

    def test_get_extension(self):

        self.assertEqual(get_extension("Music\\Artist\\01 - Track.FLAC"), "flac")
        self.assertEqual(get_extension("Music/Artist/track.mp3"), "mp3")
        self.assertEqual(get_extension("Music\\Artist\\noext"), "")
        self.assertEqual(get_extension("Music\\Artist.Name\\noext"), "")

    def test_normalize_formats(self):

        self.assertEqual(normalize_formats(None), ())
        self.assertEqual(normalize_formats([]), ())
        self.assertEqual(normalize_formats(["any"]), ())
        self.assertEqual(normalize_formats([".FLAC", "mp3", "flac"]), ("flac", "mp3"))
        self.assertEqual(normalize_formats("mp3, m4a"), ("mp3", "m4a"))
        self.assertIn("flac", normalize_formats(["lossless"]))
        self.assertNotIn("mp3", normalize_formats(["lossless"]))
        self.assertIn("mp3", normalize_formats(["lossy"]))

    def test_candidate_properties(self):

        candidate = make_candidate(virtual_path="A\\B\\track.flac", sample_rate=96000, bit_depth=24)

        self.assertEqual(candidate.basename, "track.flac")
        self.assertEqual(candidate.folder_path, "A\\B")
        self.assertEqual(candidate.extension, "flac")
        self.assertTrue(candidate.is_lossless)
        self.assertTrue(candidate.is_hires)
        self.assertEqual(candidate.effective_bitrate, 96000 * 24 * 2 // 1000)
        self.assertEqual(candidate.quality_label, "24 bit / 96 kHz")

        candidate = make_candidate(virtual_path="A\\B\\track.mp3", bitrate=320, vbr=True)

        self.assertFalse(candidate.is_lossless)
        self.assertEqual(candidate.quality_label, "320 kbps (vbr)")

    def test_preferences_validation(self):

        with self.assertRaises(ValueError):
            RankingPreferences(prefer="fastest")

        prefs = RankingPreferences(formats=["mp3"], min_bitrate=-5, max_queue=-1)

        self.assertEqual(prefs.formats, ("mp3",))
        self.assertEqual(prefs.min_bitrate, 0)
        self.assertEqual(prefs.max_queue, 0)


class FileFilterTest(TestCase):

    def test_format_filter(self):

        prefs = RankingPreferences(formats=["flac"])

        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.flac"), prefs))
        self.assertIsNotNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3"), prefs))

        # Any audio format when no formats specified, but never non-audio files
        prefs = RankingPreferences(formats=[])

        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.ogg"), prefs))
        self.assertIsNotNone(check_file_filters(make_candidate(virtual_path="x\\cover.jpg"), prefs))

    def test_availability_filters(self):

        prefs = RankingPreferences(require_free_slot=True)

        self.assertIsNone(check_file_filters(make_candidate(has_free_slot=True), prefs))
        self.assertIsNotNone(check_file_filters(make_candidate(has_free_slot=False, queue_length=1), prefs))

        prefs = RankingPreferences(max_queue=5)

        self.assertIsNone(check_file_filters(make_candidate(has_free_slot=False, queue_length=5), prefs))
        self.assertIsNotNone(check_file_filters(make_candidate(has_free_slot=False, queue_length=6), prefs))

    def test_bitrate_and_length_filters(self):

        prefs = RankingPreferences(formats=["flac", "mp3"], min_bitrate=256, min_length=60, max_length=600)

        self.assertIsNotNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=192), prefs))
        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=320), prefs))

        # Lossless files always pass the bitrate filter, unknown bitrates pass too
        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.flac"), prefs))
        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=0), prefs))

        self.assertIsNotNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=320, length=30),
                                                prefs))
        self.assertIsNotNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=320, length=900),
                                                prefs))
        self.assertIsNone(check_file_filters(make_candidate(virtual_path="x\\a.mp3", bitrate=320, length=0), prefs))


class FileRankingTest(TestCase):

    def test_free_slot_beats_queue(self):

        free = make_candidate(username="free", has_free_slot=True)
        queued = make_candidate(username="queued", has_free_slot=False, queue_length=10)

        ranked = rank_files([queued, free])

        self.assertEqual([candidate.username for candidate in ranked], ["free", "queued"])
        self.assertGreater(free.score, queued.score)
        self.assertGreater(free.score_details["availability"], queued.score_details["availability"])

    def test_higher_bitrate_wins(self):

        low = make_candidate(username="low", virtual_path="x\\a.mp3", bitrate=128)
        high = make_candidate(username="high", virtual_path="x\\a.mp3", bitrate=320)

        ranked = rank_files([low, high], RankingPreferences(formats=["mp3"]))

        self.assertEqual([candidate.username for candidate in ranked], ["high", "low"])

    def test_lossless_beats_lossy_with_same_availability(self):

        mp3 = make_candidate(username="mp3", virtual_path="x\\a.mp3", bitrate=320)
        flac = make_candidate(username="flac", virtual_path="x\\a.flac")

        ranked = rank_files([mp3, flac])

        self.assertEqual(ranked[0].username, "flac")

    def test_prefer_modes(self):

        # A queued FLAC against a free-slot 320 kbps MP3
        flac = make_candidate(username="flac", virtual_path="x\\a.flac", has_free_slot=False, queue_length=4,
                              upload_speed=0)
        mp3 = make_candidate(username="mp3", virtual_path="x\\a.mp3", bitrate=320, upload_speed=0)

        ranked = rank_files([flac, mp3], RankingPreferences(prefer="quality"))
        self.assertEqual(ranked[0].username, "flac")

        ranked = rank_files([flac, mp3], RankingPreferences(prefer="availability"))
        self.assertEqual(ranked[0].username, "mp3")

    def test_speed_is_tie_breaker(self):

        slow = make_candidate(username="slow", upload_speed=100_000)
        fast = make_candidate(username="fast", upload_speed=20_000_000)

        ranked = rank_files([slow, fast])

        self.assertEqual(ranked[0].username, "fast")
        self.assertGreater(fast.score_details["speed"], slow.score_details["speed"])

    def test_format_preference_order(self):

        flac = make_candidate(username="flac", virtual_path="x\\a.flac", bitrate=0)
        mp3 = make_candidate(username="mp3", virtual_path="x\\a.mp3", bitrate=320)

        # Same availability and speed, mp3 listed first gets a bonus but lossless still scores higher on quality
        ranked = rank_files([flac, mp3], RankingPreferences(formats=["mp3", "flac"]))
        self.assertGreater(mp3.score_details["format_preference"], flac.score_details["format_preference"])
        self.assertEqual(ranked[0].username, "flac")

    def test_suspicious_size_penalty(self):

        tiny = make_candidate(username="tiny", size=100_000)
        normal = make_candidate(username="normal")

        ranked = rank_files([tiny, normal])

        self.assertEqual(ranked[0].username, "normal")
        self.assertGreater(tiny.score_details["penalty"], 0)

    def test_duplicates_increase_score(self):

        popular_a = make_candidate(username="a", virtual_path="x\\same.flac", size=1000_000)
        popular_b = make_candidate(username="b", virtual_path="y\\same.flac", size=1000_000)
        unique = make_candidate(username="c", virtual_path="z\\other.flac", size=1000_001)

        rank_files([popular_a, popular_b, unique])

        self.assertEqual(popular_a.score_details["popularity"], 1)
        self.assertEqual(unique.score_details["popularity"], 0)

    def test_filtered_and_limited(self):

        candidates = [
            make_candidate(username=f"user{i}", virtual_path=f"x\\{i}.flac", upload_speed=i * 100_000)
            for i in range(10)
        ]
        candidates.append(make_candidate(username="image", virtual_path="x\\cover.jpg"))

        ranked = rank_files(candidates, limit=3)

        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0].username, "user9")
        self.assertNotIn("image", [candidate.username for candidate in ranked])

    def test_score_file_details(self):

        candidate = make_candidate()
        score, details = score_file(candidate, RankingPreferences())

        self.assertGreater(score, 0)
        self.assertEqual(
            set(details),
            {"availability", "speed", "quality", "format_preference", "relevance", "popularity", "penalty"}
        )

    def test_relevance_and_variants(self):

        self.assertEqual(tokenize("Disc 02 Track 03 Around the World (Tee’s Frozen Sun mix)"),
                         ["disc", "02", "track", "03", "around", "the", "world", "tee", "s", "frozen", "sun", "mix"])

        prefs = RankingPreferences(query_words=("daft", "punk", "around", "the", "world"))

        # No query words, no relevance
        self.assertEqual(relevance_points("Around the World", RankingPreferences()), (0.0, 0.0))

        relevance, penalty = relevance_points("Daft Punk - Around the World", prefs)
        self.assertEqual((relevance, penalty), (10.0, 0.0))

        relevance, penalty = relevance_points("03 - Around the World", prefs)
        self.assertEqual((relevance, penalty), (6.0, 0.0))

        relevance, penalty = relevance_points("Around the World (Kenlou mix)", prefs)
        self.assertEqual((relevance, penalty), (6.0, 8.0))

        # Variants asked for in the query are not penalized
        prefs = RankingPreferences(query_words=("around", "the", "world", "mix"))
        self.assertEqual(relevance_points("Around the World (Kenlou mix)", prefs), (10.0, 0.0))

        # The original beats the remix from the same uploader, all else being equal
        prefs = RankingPreferences(query_words=("daft", "punk", "around", "the", "world"))
        original = make_candidate(virtual_path="Daft Punk\\Homework\\07 - Around the World.flac")
        remix = make_candidate(virtual_path="Daft Punk\\Homework (Remixes)\\Around the World (Mellow mix).flac")

        ranked = rank_files([remix, original], prefs)
        self.assertEqual(ranked[0].basename, "07 - Around the World.flac")
        self.assertEqual(ranked[1].score_details["penalty"], 8.0)

    def test_max_per_user(self):

        candidates = [
            make_candidate(username="hoarder", virtual_path=f"x\\{i}.flac", upload_speed=50_000_000)
            for i in range(5)
        ]
        candidates.append(make_candidate(username="other", virtual_path="y\\0.flac", upload_speed=100_000))

        ranked = rank_files(candidates, RankingPreferences(max_per_user=2))
        self.assertEqual([candidate.username for candidate in ranked], ["hoarder", "hoarder", "other"])

        ranked = rank_files(candidates, RankingPreferences(max_per_user=0))
        self.assertEqual(len(ranked), 6)

        ranked = rank_files(candidates, RankingPreferences(max_per_user=2), limit=2)
        self.assertEqual([candidate.username for candidate in ranked], ["hoarder", "hoarder"])


class FolderRankingTest(TestCase):

    @staticmethod
    def make_album(username, folder_path, num_tracks, extension="flac", **kwargs):

        return [
            make_candidate(
                username=username, virtual_path=f"{folder_path}\\{i:02d} - Track.{extension}", **kwargs
            )
            for i in range(1, num_tracks + 1)
        ]

    def test_folders_grouped_and_ranked(self):

        candidates = []
        candidates += self.make_album("complete", "Music\\Artist\\Album", 10)
        candidates += self.make_album("partial", "Music\\Artist\\Album", 2)
        candidates += self.make_album("queued", "Music\\Artist\\Album", 10, has_free_slot=False, queue_length=20)

        ranked = rank_folders(candidates)

        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0].username, "complete")
        self.assertEqual(ranked[0].folder_name, "Album")
        self.assertEqual(len(ranked[0].files), 10)
        self.assertEqual(ranked[0].score_details["num_matching_files"], 10)

        # Both complete folders are shared by two other users with the same folder name
        self.assertEqual(ranked[0].score_details["popularity"], 2)

    def test_folder_without_matching_audio_is_dropped(self):

        candidates = self.make_album("mp3user", "Music\\Album", 10, extension="mp3", bitrate=320)
        candidates += [make_candidate(username="mp3user", virtual_path="Music\\Album\\cover.jpg")]

        self.assertEqual(rank_folders(candidates, RankingPreferences(formats=["flac"])), [])

        ranked = rank_folders(candidates, RankingPreferences(formats=["mp3"]))
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].formats, ["mp3"])
        self.assertEqual(len(ranked[0].audio_files), 10)
        self.assertEqual(len(ranked[0].files), 11)

    def test_consistent_format_bonus(self):

        consistent = self.make_album("consistent", "Music\\A", 5)
        mixed = self.make_album("mixed", "Music\\B", 4) + self.make_album("mixed", "Music\\B", 1, extension="wav")

        ranked = rank_folders(consistent + mixed, RankingPreferences(formats=[]))

        self.assertEqual(ranked[0].username, "consistent")
        self.assertEqual(ranked[0].score_details["consistency"], 3)
        self.assertEqual(ranked[1].score_details["consistency"], 0)

    def test_folder_relevance_and_per_user_cap(self):

        prefs = RankingPreferences(query_words=("daft", "punk", "discovery"), max_per_user=1)
        original = self.make_album("user", "Music\\Daft Punk\\Discovery (2001)", 10)
        remixes = self.make_album("user", "Music\\Daft Punk\\Discovery (Remixes)", 10)
        other = self.make_album("other", "Music\\Daft Punk\\Discovery", 10, upload_speed=100_000)

        ranked = rank_folders(original + remixes + other, prefs)

        self.assertEqual([folder.username for folder in ranked], ["user", "other"])
        self.assertEqual(ranked[0].folder_name, "Discovery (2001)")
        self.assertEqual(ranked[0].score_details["relevance"], 10.0)
        self.assertEqual(ranked[0].score_details["penalty"], 0.0)

        ranked = rank_folders(original + remixes, RankingPreferences(query_words=prefs.query_words, max_per_user=0))
        self.assertEqual(ranked[1].folder_name, "Discovery (Remixes)")
        self.assertEqual(ranked[1].score_details["penalty"], 8.0)

    def test_folder_properties(self):

        files = self.make_album("user", "Music\\Album", 3, upload_speed=5_000_000, has_free_slot=False,
                                queue_length=2)
        ranked = rank_folders(files)
        folder = ranked[0]

        self.assertEqual(folder.key, "userMusic\\Album")
        self.assertEqual(folder.total_size, 3 * 30_000_000)
        self.assertEqual(folder.upload_speed, 5_000_000)
        self.assertEqual(folder.queue_length, 2)
        self.assertFalse(folder.has_free_slot)
