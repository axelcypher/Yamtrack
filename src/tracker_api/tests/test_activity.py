import datetime
from unittest.mock import patch

from django.utils import timezone

from app.models import TV, Game, Item
from tracker_api.models import AnimeTvLink

from .helpers import ApiTestCase, fake_metadata

URL = "/api/tracker/v1/activity"


@patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class ActivityTests(ApiTestCase):
    """GET /api/tracker/v1/activity."""

    def watch(self, **data):
        """Record an entry through the API."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, 200, response.content)

    def play(self, media_id, session_id, minutes):
        """Report a game session the way Castellan does."""
        self.watch(
            media_type="game",
            source="igdb",
            media_id=media_id,
            session_id=session_id,
            minutes=minutes,
            client="castellan",
        )

    def activity(self, media_type, since=None, until=None, status=200):
        """Fetch the activity of one media type; both days default to today."""
        today = timezone.localdate().isoformat()
        response = self.get_json(
            URL,
            {
                "media_type": media_type,
                "since": since or today,
                "until": until or today,
            },
        )
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_validation(self, *_mocks):
        """Media type and both days are checked."""
        self.assertEqual(
            self.activity("movie", status=400)["error"], "invalid_media_type"
        )
        for since, until in (
            ("yesterday", "2026-11-03"),
            ("2026-02-30", "2026-03-01"),
            ("2026-11-04", "2026-11-03"),
        ):
            data = self.activity("game", since, until, status=400)
            self.assertEqual(data["error"], "invalid_date")
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_game_minutes_sorted(self, *_mocks):
        """Session deltas add up per game, the largest amount first."""
        self.play("113112", "s1", 45)
        self.play("113112", "s1", 60)  # cumulative: adds 15
        self.play("1942", "s2", 20)

        data = self.activity("game", "2026-01-01", timezone.localdate().isoformat())

        self.assertEqual(data["media_type"], "game")
        self.assertEqual(data["since"], "2026-01-01")
        self.assertEqual(
            [(row["media_id"], row["amount"], row["unit"]) for row in data["items"]],
            [("113112", 60, "minutes"), ("1942", 20, "minutes")],
        )
        self.assertEqual(
            set(data["items"][0]),
            {"source", "media_id", "title", "image", "amount", "unit"},
        )
        self.assertEqual(data["items"][0]["source"], "igdb")

    def test_range_is_inclusive_and_drops_empty_items(self, *_mocks):
        """Only progress inside [since, until] counts; nothing else is listed."""
        self.play("113112", "s1", 30)
        ten_days_ago = timezone.now() - datetime.timedelta(days=10)
        Game.history.model.objects.update(history_date=ten_days_ago)
        self.play("1942", "s2", 5)

        day = timezone.localdate(ten_days_ago).isoformat()
        self.assertEqual(
            [row["media_id"] for row in self.activity("game", day, day)["items"]],
            ["113112"],
        )
        self.assertEqual(
            [row["media_id"] for row in self.activity("game")["items"]], ["1942"]
        )

    def test_anime_episodes_and_manga_chapters(self, *_mocks):
        """Anime counts episodes, manga chapters."""
        for episode in (1, 2):
            self.watch(
                media_type="anime", source="mal", media_id="52991", unit_number=episode
            )
        for chapter in (1, 2, 3):
            self.watch(
                media_type="manga", source="mal", media_id="2", unit_number=chapter
            )

        anime = self.activity("anime")["items"]
        self.assertEqual(
            [
                (row["source"], row["media_id"], row["amount"], row["unit"])
                for row in anime
            ],
            [("mal", "52991", 2, "episodes")],
        )
        manga = self.activity("manga")["items"]
        self.assertEqual(
            [(row["amount"], row["unit"]) for row in manga], [(3, "chapters")]
        )

    def test_linked_tv_episodes_count_once_for_the_anime(self, *_mocks):
        """Episodes tracked as TV and as anime count once, for the anime."""
        for episode in (1, 2):
            self.watch(
                media_type="anime", source="mal", media_id="52991", unit_number=episode
            )
            self.watch(
                media_type="tv",
                source="tmdb",
                media_id="1396",
                season_number=1,
                unit_number=episode,
            )
        AnimeTvLink.objects.create(
            user=self.user,
            tv=TV.objects.get(user=self.user),
            anime_item=Item.objects.get(media_type="anime", media_id="52991"),
            seasons=[1],
        )
        mapping = {
            "tmdb_show:1396:s1": {"mal:52991": {"1-3": "1-3"}},
            "mal:52991": {"tmdb_show:1396:s1": {"1-3": "1-3"}},
        }
        with patch(
            "integrations.webhooks.anime_mappings.fetch_mapping_data",
            return_value=mapping,
        ):
            self.assertEqual(
                [row["amount"] for row in self.activity("anime")["items"]], [2]
            )
            # Watched as TV: the anime progress the sync mirrors does not count
            # again, the TV episode counts for the anime.
            self.watch(
                media_type="tv",
                source="tmdb",
                media_id="1396",
                season_number=1,
                unit_number=3,
            )
            items = self.activity("anime")["items"]
        self.assertEqual(
            [(row["media_id"], row["amount"]) for row in items], [("52991", 3)]
        )
