import datetime
from unittest.mock import patch

from app.models import Game, Item, MediaTypes, Sources, Status
from app.providers.services import ProviderAPIError

from .helpers import ApiTestCase

# Explicit, so the order does not depend on the clock resolution.
OLDER = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
NEWER = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)


def _igdb_page(*games, total_pages=1):
    return {
        "page": 1,
        "total_pages": total_pages,
        "total_results": len(games),
        "results": [
            {
                "media_id": media_id,
                "source": "igdb",
                "media_type": "game",
                "title": title,
                "image": "cover.jpg",
            }
            for media_id, title in games
        ],
    }


class GameSearchTests(ApiTestCase):
    """Game search covers the user's library, including manual entries."""

    def setUp(self):
        """Track one IGDB game and one manually created game."""
        super().setUp()
        hades = Item.objects.create(
            media_id="113112",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Hades",
            image="hades.jpg",
        )
        # bulk_create skips Media.save, which would ask IGDB for metadata.
        Game.objects.bulk_create(
            [
                Game(
                    item=hades,
                    user=self.user,
                    progress=600,
                    status=Status.IN_PROGRESS.value,
                )
            ]
        )
        homebrew = Item.objects.create(
            media_id="0b6f6d1e-1111-4c1a-9f3e-123456789abc",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Hades Homebrew Mod",
            image="",
        )
        Game.objects.bulk_create(
            [
                Game(
                    item=homebrew,
                    user=self.user,
                    progress=30,
                    status=Status.PLANNING.value,
                )
            ]
        )

        # created_at is auto_now_add, so it can only be set afterwards.
        Game.objects.filter(item=hades).update(created_at=OLDER)
        Game.objects.filter(item=homebrew).update(created_at=NEWER)

    def search(self, query, **params):
        """Run a game search as the test user."""
        return self.get_json(
            "/api/tracker/v1/search", {"media_type": "game", "q": query, **params}
        )

    @patch("tracker_api.views.services.search")
    def test_library_first_without_duplicates(self, mock_search):
        """Own entries come first; IGDB results already listed are dropped."""
        mock_search.return_value = _igdb_page((113112, "Hades"), (1, "Hades II"))
        response = self.search("hades")
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(
            [(r["source"], r["title"]) for r in results],
            [
                ("manual", "Hades Homebrew Mod"),
                ("igdb", "Hades"),
                ("igdb", "Hades II"),
            ],
        )
        self.assertTrue(results[0]["in_library"])
        self.assertEqual(results[0]["user_status"], Status.PLANNING.value)
        self.assertEqual(results[1]["user_progress"], 600)
        self.assertFalse(results[2]["in_library"])

    @patch("tracker_api.views.services.search")
    def test_matches_case_insensitive_substring(self, mock_search):
        """The query matches anywhere in the title, ignoring case."""
        mock_search.return_value = _igdb_page()
        results = self.search("HOMEBREW").json()["results"]
        self.assertEqual([r["title"] for r in results], ["Hades Homebrew Mod"])

    @patch("tracker_api.views.services.search")
    def test_library_only_on_first_page(self, mock_search):
        """Later pages are the provider's pages alone."""
        mock_search.return_value = _igdb_page((2, "Hades Fan Game"), total_pages=2)
        results = self.search("hades", page=2).json()["results"]
        self.assertEqual([r["title"] for r in results], ["Hades Fan Game"])

    @patch("tracker_api.views.services.search")
    def test_provider_down_returns_library(self, mock_search):
        """When IGDB fails, own entries are still returned."""
        mock_search.side_effect = ProviderAPIError("igdb", Exception("down"))
        response = self.search("hades")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["total_pages"], 1)

    @patch("tracker_api.views.services.search")
    def test_provider_down_without_library_match(self, mock_search):
        """Without own matches a provider failure stays a 502."""
        mock_search.side_effect = ProviderAPIError("igdb", Exception("down"))
        response = self.search("celeste")
        self.assertEqual(response.status_code, 502)

    @patch("tracker_api.views.services.search")
    def test_other_users_entries_hidden(self, mock_search):
        """Only the requesting user's library is searched."""
        mock_search.return_value = _igdb_page()
        Game.objects.filter(user=self.user).delete()
        self.assertEqual(self.search("hades").json()["results"], [])
