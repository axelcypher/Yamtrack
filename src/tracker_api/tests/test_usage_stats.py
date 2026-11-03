from unittest.mock import patch

from django.test import SimpleTestCase

from app.models import Item
from tracker_api import usage_stats
from tracker_api.facts import facts_from_metadata, parse_runtime
from tracker_api.models import AnimeTvLink, ItemFacts

from .helpers import ApiTestCase, fake_metadata


class RuntimeParsingTests(SimpleTestCase):
    """parse_runtime / facts_from_metadata."""

    def test_formats(self):
        """Provider runtime strings become minutes."""
        self.assertEqual(parse_runtime("1h 30m"), 90)
        self.assertEqual(parse_runtime("45m"), 45)
        self.assertEqual(parse_runtime("24 min"), 24)
        self.assertEqual(parse_runtime("2h 0m"), 120)
        self.assertIsNone(parse_runtime(None))
        self.assertIsNone(parse_runtime(""))

    def test_metadata(self):
        """Genres and runtime come from metadata."""
        self.assertEqual(
            facts_from_metadata(
                {"genres": ["Fantasy"], "details": {"runtime": "24 min"}}
            ),
            (["Fantasy"], 24),
        )
        self.assertEqual(facts_from_metadata({}), ([], None))


@patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class UsageStatsTests(ApiTestCase):
    """usage_stats.build and the page."""

    def watch(self, **data):
        """Record an entry through the API."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, 200, response.content)

    def seed(self):
        """Two TV episodes, two anime episodes, a movie and 45 game minutes."""
        for episode in (1, 2):
            self.watch(
                media_type="tv",
                source="tmdb",
                media_id="1396",
                season_number=1,
                unit_number=episode,
                usage={"client": "extension", "platform": "netflix", "minutes": 47},
            )
        for episode in (1, 2):
            self.watch(
                media_type="anime",
                source="mal",
                media_id="52991",
                unit_number=episode,
                usage={"client": "extension", "platform": "crunchyroll", "minutes": 24},
            )
        self.watch(media_type="movie", source="tmdb", media_id="27205")
        self.watch(
            media_type="game",
            source="igdb",
            media_id="113112",
            session_id="s1",
            minutes=45,
            client="castellan",
        )

    def test_totals_platforms_and_estimates(self, *_mocks):
        """Units per type, platforms and runtime-based hours."""
        self.seed()
        anime_item = Item.objects.get(media_type="anime", media_id="52991")
        ItemFacts.objects.create(
            item=anime_item, genres=["Fantasy"], runtime_minutes=24
        )

        stats = usage_stats.build(self.user, None, None)

        totals = {row["label"]: row for row in stats["totals"]}
        self.assertEqual(totals["TV Show"]["units"], 2)
        self.assertEqual(totals["Anime"]["units"], 2)
        self.assertEqual(totals["Movie"]["units"], 1)
        self.assertEqual(totals["Game"]["hours"], 1)  # 45 min rounds to 1 h
        platforms = {row["label"]: row for row in stats["chart"]["platforms"]}
        self.assertEqual(platforms["netflix"]["minutes"], 94)
        self.assertEqual(platforms["crunchyroll"]["units"], 2)
        self.assertEqual(platforms["castellan"]["minutes"], 45)
        self.assertEqual(stats["chart"]["genres"], [{"label": "Fantasy", "units": 2}])
        self.assertEqual(stats["facts_known"], 1)
        self.assertTrue(stats["chart"]["weeks"])

    def test_linked_tv_counts_as_anime(self, *_mocks):
        """Episodes of a linked show are left to the anime entry."""
        self.seed()
        from app.models import TV  # noqa: PLC0415

        AnimeTvLink.objects.create(
            user=self.user,
            tv=TV.objects.get(user=self.user),
            anime_item=Item.objects.get(media_type="anime", media_id="52991"),
            seasons=[1],
        )
        totals = {
            row["label"] for row in usage_stats.build(self.user, None, None)["totals"]
        }
        self.assertNotIn("TV Show", totals)

    @patch("tracker_api.facts.refresh_item_facts.delay")
    def test_page_renders_and_requests_facts(self, mock_delay, *_mocks):
        """The page renders and asks for missing facts in the background."""
        self.seed()
        self.client.force_login(self.user)
        response = self.client.get("/tracker/statistics?start-date=all&end-date=all")
        self.assertContains(response, "Activity per week")
        self.assertContains(response, "usage-data")
        mock_delay.assert_called_once_with(self.user.pk)
