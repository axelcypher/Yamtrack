from unittest.mock import patch

from .helpers import ApiTestCase, fake_metadata

TV_META = {"title": "Breaking Bad", "image": "bb.jpg", "tvdb_id": 81189}


@patch("tracker_api.resolve.anime_mappings.fetch_mapping_data", return_value={})
@patch("tracker_api.resolve.services.get_media_metadata", side_effect=fake_metadata)
class ResolveTests(ApiTestCase):
    """POST /api/tracker/v1/resolve."""

    def resolve(self, **data):
        """Post a detection and return the JSON response."""
        response = self.post_json("/api/tracker/v1/resolve", data)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_invalid_kind(self, *_mocks):
        """Unknown kinds are rejected."""
        response = self.post_json(
            "/api/tracker/v1/resolve", {"kind": "game", "title": "X"}
        )
        self.assertEqual(response.status_code, 400)

    def test_mal_id_anime(self, *_mocks):
        """A MAL id resolves directly to an anime with a MAL target."""
        data = self.resolve(
            kind="episode", title="Frieren", episode=5, ids={"mal": "52991"}
        )
        self.assertEqual(data["confidence"], "id")
        self.assertEqual(data["target"]["media_type"], "anime")
        self.assertEqual(data["target"]["media_id"], "52991")
        self.assertEqual(data["target"]["unit_number"], 5)
        self.assertEqual(
            data["mal"], {"type": "anime", "id": "52991", "unit_number": 5}
        )

    def test_mal_id_manga_chapter(self, *_mocks):
        """Chapters with a MAL id resolve to manga."""
        data = self.resolve(
            kind="chapter", title="Frieren", chapter=21, ids={"mal": "126287"}
        )
        self.assertEqual(data["target"]["media_type"], "manga")
        self.assertEqual(data["mal"]["unit_number"], 21)

    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_anidb",
        return_value=("52991", 3),
    )
    def test_anidb(self, mock_anidb, *_mocks):
        """AniDB ids go through the AniBridge mapping."""
        data = self.resolve(
            kind="episode", title="X", episode=3, ids={"anidb": "17617"}
        )
        self.assertEqual(data["target"]["media_id"], "52991")
        self.assertEqual(data["target"]["unit_number"], 3)
        mock_anidb.assert_called_once_with({}, "17617", 3)

    @patch("tracker_api.resolve.tmdb.tv", return_value=TV_META)
    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tvdb",
        return_value=(None, None),
    )
    @patch("tracker_api.resolve.tmdb.find")
    @patch("tracker_api.resolve.tvdb.episode", return_value=None)
    def test_tvdb_episode_to_tv(self, _episode, mock_find, *_mocks):
        """TVDB episode ids are found on TMDB."""
        mock_find.return_value = {
            "tv_episode_results": [
                {"show_id": 1396, "season_number": 1, "episode_number": 2}
            ]
        }
        data = self.resolve(kind="episode", title="BB", ids={"tvdb_episode": "349232"})
        self.assertEqual(data["confidence"], "id")
        self.assertEqual(
            {
                key: data["target"][key]
                for key in ("media_type", "media_id", "season_number", "unit_number")
            },
            {
                "media_type": "tv",
                "media_id": "1396",
                "season_number": 1,
                "unit_number": 2,
            },
        )
        self.assertIsNone(data["mal"])

    @patch("tracker_api.resolve.tmdb.tv", return_value=TV_META)
    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tvdb",
        return_value=("52991", 7),
    )
    def test_tmdb_show_maps_to_anime(self, mock_tvdb, *_mocks):
        """A TMDB show with an AniBridge mapping becomes the MAL entry."""
        data = self.resolve(
            kind="episode",
            title="Frieren",
            season=1,
            episode=7,
            ids={"tmdb_show": "209867"},
        )
        self.assertEqual(data["target"]["media_type"], "anime")
        self.assertEqual(data["target"]["unit_number"], 7)
        mock_tvdb.assert_called_once_with({}, 81189, 1, 7)

    @patch("tracker_api.resolve.tmdb.tv", return_value=TV_META)
    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tvdb",
        return_value=("52991", 7),
    )
    def test_anime_disabled_stays_tv(self, mock_tvdb, *_mocks):
        """Users with anime disabled keep the TMDB show."""
        self.user.anime_enabled = False
        self.user.save()
        data = self.resolve(
            kind="episode",
            title="Frieren",
            season=1,
            episode=7,
            ids={"tmdb_show": "209867"},
        )
        self.assertEqual(data["target"]["media_type"], "tv")
        mock_tvdb.assert_not_called()

    @patch(
        "tracker_api.resolve.tmdb.movie",
        return_value={"title": "Inception", "image": "i.jpg"},
    )
    @patch("tracker_api.resolve.anime_mappings.get_mal_id_from_imdb", return_value=None)
    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tmdb_movie",
        return_value=None,
    )
    @patch(
        "tracker_api.resolve.tmdb.find", return_value={"movie_results": [{"id": 27205}]}
    )
    def test_imdb_movie(self, *_mocks):
        """IMDb movie ids are found on TMDB."""
        data = self.resolve(kind="movie", title="Inception", ids={"imdb": "tt1375666"})
        self.assertEqual(data["target"]["media_type"], "movie")
        self.assertEqual(data["target"]["media_id"], "27205")

    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tmdb_movie",
        return_value="199",
    )
    def test_anime_movie(self, *_mocks):
        """Anime films map to their MAL entry with one episode."""
        data = self.resolve(
            kind="movie", title="Spirited Away", ids={"tmdb_movie": "129"}
        )
        self.assertEqual(data["target"]["media_type"], "anime")
        self.assertEqual(data["target"]["unit_number"], 1)

    @patch("tracker_api.resolve.tmdb.tv", return_value=TV_META)
    @patch(
        "tracker_api.resolve.anime_mappings.get_mal_id_from_tvdb",
        return_value=(None, None),
    )
    @patch("tracker_api.resolve.services.search")
    def test_title_exact(self, mock_search, *_mocks):
        """A single exact title match is accepted."""
        mock_search.return_value = {
            "results": [
                {
                    "media_id": 1396,
                    "source": "tmdb",
                    "media_type": "tv",
                    "title": "Breaking Bad",
                    "image": "",
                    "release_year": 2008,
                },
                {
                    "media_id": 1,
                    "source": "tmdb",
                    "media_type": "tv",
                    "title": "Breaking Bad Recap",
                    "image": "",
                    "release_year": 2013,
                },
            ]
        }
        data = self.resolve(kind="episode", title="Breaking Bad", season=2, episode=3)
        self.assertEqual(data["confidence"], "title_exact")
        self.assertEqual(data["target"]["media_id"], "1396")
        self.assertEqual(data["target"]["season_number"], 2)

    @patch("tracker_api.resolve.services.search")
    def test_title_ambiguous_uses_year(self, mock_search, *_mocks):
        """Remakes with the same title are told apart by year, else fuzzy."""
        mock_search.return_value = {
            "results": [
                {
                    "media_id": 10,
                    "source": "tmdb",
                    "media_type": "movie",
                    "title": "Dune",
                    "image": "",
                    "release_year": 1984,
                },
                {
                    "media_id": 20,
                    "source": "tmdb",
                    "media_type": "movie",
                    "title": "Dune",
                    "image": "",
                    "release_year": 2021,
                },
            ]
        }
        with (
            patch(
                "tracker_api.resolve.tmdb.movie",
                return_value={"title": "Dune", "image": ""},
            ),
            patch(
                "tracker_api.resolve.anime_mappings.get_mal_id_from_tmdb_movie",
                return_value=None,
            ),
        ):
            data = self.resolve(kind="movie", title="Dune", year=2021)
            self.assertEqual(data["target"]["media_id"], "20")
            data = self.resolve(kind="movie", title="Dune")
        self.assertIsNone(data["target"])
        self.assertEqual(data["confidence"], "title_fuzzy")
        self.assertEqual(len(data["candidates"]), 2)

    @patch("tracker_api.resolve.services.search")
    def test_anime_hint_falls_back_to_mal(self, mock_search, *_mocks):
        """Anime titles TMDB does not know exactly are searched on MAL."""
        mock_search.side_effect = [
            {"results": []},
            {
                "results": [
                    {
                        "media_id": 52991,
                        "source": "mal",
                        "media_type": "anime",
                        "title": "Sousou no Frieren",
                        "image": "f.jpg",
                    }
                ]
            },
        ]
        data = self.resolve(
            kind="episode",
            media_hint="anime",
            title="Frieren: Beyond Journey's End",
            alt_titles=["Sousou no Frieren"],
            episode=4,
        )
        self.assertEqual(data["confidence"], "title_exact")
        self.assertEqual(data["target"]["media_id"], "52991")
        self.assertEqual(data["mal"]["unit_number"], 4)

    @patch("tracker_api.resolve.services.search", return_value={"results": []})
    def test_nothing_found(self, *_mocks):
        """No results means no target and no candidates."""
        data = self.resolve(kind="episode", title="Nope", episode=1)
        self.assertIsNone(data["target"])
        self.assertEqual(data["confidence"], "none")
