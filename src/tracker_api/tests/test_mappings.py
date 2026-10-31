from django.test import SimpleTestCase

from tracker_api.mappings import mal_from_tmdb_show

# Shapes as in AniBridge's mappings.min.json
MAPPING = {
    "tmdb_show:209867:s1": {"mal:52991": {"1-28": "1-28"}},
    "tmdb_show:37854:s22": {"mal:21": {"1089-": "1089-"}},
    "tvdb_show:81797:s23": {"mal:21": {"1-": "1156-"}},
}


class TmdbShowMappingTests(SimpleTestCase):
    """mal_from_tmdb_show."""

    def test_maps_season_episode(self):
        """A TMDB season maps to its MAL entry and episode."""
        self.assertEqual(mal_from_tmdb_show(MAPPING, 209867, 1, 7), (52991, 7))

    def test_absolute_numbering(self):
        """Absolute TMDB numbering (One Piece) maps one to one."""
        self.assertEqual(mal_from_tmdb_show(MAPPING, "37854", 22, 1100), (21, 1100))

    def test_missing_season(self):
        """Seasons without a TMDB entry stay unmapped instead of guessing."""
        self.assertEqual(mal_from_tmdb_show(MAPPING, 37854, 23, 1163), (None, None))
