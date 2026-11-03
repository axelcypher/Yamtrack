from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from app.models import TV, Anime, BasicMedia, Episode, Item
from events.models import Event
from tracker_api import hooks
from tracker_api.models import AnimeTvLink

from .helpers import ApiTestCase, fake_metadata

FRIEREN = "52991"
SHOW = "1396"  # the TMDB id the scrobble helpers use

# AniBridge-shaped mapping: TMDB S1 E1-3 <-> MAL 52991 E1-3
MAPPING = {
    f"tmdb_show:{SHOW}:s1": {f"mal:{FRIEREN}": {"1-3": "1-3"}},
    f"mal:{FRIEREN}": {f"tmdb_show:{SHOW}:s1": {"1-3": "1-3"}},
}


@patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value=MAPPING)
@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class AnimeTvLinkTests(ApiTestCase):
    """Linking, hiding and syncing anime tracked as TV and anime."""

    def watch(self, **data):
        """Record an entry through the API and return the response JSON."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def watch_tv(self, episode):
        """Watch a TMDB episode of the test show."""
        return self.watch(
            media_type="tv",
            source="tmdb",
            media_id=SHOW,
            season_number=1,
            unit_number=episode,
        )

    def watch_anime(self, episode):
        """Watch a MAL episode of Frieren."""
        return self.watch(
            media_type="anime", source="mal", media_id=FRIEREN, unit_number=episode
        )

    def link(self):
        """Run the link command with --apply and return its output."""
        out = StringIO()
        call_command("link_anime_tv", "--user", "tracker", "--apply", stdout=out)
        return out.getvalue()

    def episodes(self):
        """Watched TV episode numbers of the test show."""
        return sorted(
            Episode.objects.filter(related_season__user=self.user).values_list(
                "item__episode_number", flat=True
            )
        )

    def test_command_links_and_dry_run_changes_nothing(self, *_mocks):
        """The dry run reports the link; --apply stores it."""
        self.watch_tv(1)
        self.watch_anime(1)
        out = StringIO()
        call_command("link_anime_tv", "--user", "tracker", stdout=out)
        self.assertIn("link Show (S1) -> A anime", out.getvalue())
        self.assertFalse(AnimeTvLink.objects.exists())

        self.link()
        link = AnimeTvLink.objects.get(user=self.user)
        self.assertEqual((link.anime_item.media_id, link.seasons), (FRIEREN, [1]))
        # Both entries stay.
        self.assertTrue(TV.objects.filter(user=self.user).exists())
        self.assertTrue(Anime.objects.filter(user=self.user).exists())

    def test_linked_tv_is_hidden_from_lists(self, *_mocks):
        """TV and season lists (and so the home page) skip linked shows."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        tv_list = BasicMedia.objects.get_media_list(self.user, "tv", "All", "title")
        season_list = BasicMedia.objects.get_media_list(
            self.user, "season", "All", "title"
        )
        anime_list = BasicMedia.objects.get_media_list(
            self.user, "anime", "All", "title"
        )
        self.assertEqual((tv_list.count(), season_list.count()), (0, 0))
        self.assertEqual(anime_list.count(), 1)

    def test_tv_episode_moves_anime_forward(self, *_mocks):
        """Watching on the TV side advances the linked anime."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        self.watch_tv(2)
        self.assertEqual(Anime.objects.get(user=self.user).progress, 2)

    def test_anime_progress_records_tv_episodes(self, *_mocks):
        """Watching on the anime side records the TMDB episodes."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        self.watch_anime(3)
        self.assertEqual(self.episodes(), [1, 2, 3])

    def test_undo_reverts_both_sides(self, *_mocks):
        """One undo token covers the mirrored writes."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        result = self.watch_anime(3)
        self.post_json("/api/tracker/v1/undo", {"undo": result["undo"]})
        self.assertEqual(Anime.objects.get(user=self.user).progress, 1)
        self.assertEqual(self.episodes(), [1])

    def test_reconcile_catches_up_bulk_gaps(self, *_mocks):
        """The command fills progress that bypassed the signals."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        Anime.objects.filter(user=self.user).update(progress=3)  # e.g. an import
        out = self.link()
        self.assertIn("anime unchanged, 2 TV episodes", out)
        self.assertEqual(self.episodes(), [1, 2, 3])

    def test_unlinked_users_are_untouched(self, *_mocks):
        """Without an anime entry nothing is linked or synced."""
        self.watch_tv(1)
        self.watch_tv(2)
        self.assertFalse(AnimeTvLink.objects.exists())
        self.assertFalse(Anime.objects.exists())

    def test_calendar_shows_one_event(self, *_mocks):
        """TV events are dropped only when the anime has one that day."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        season_item = Item.objects.get(media_type="season", media_id=SHOW)
        anime_item = Item.objects.get(media_type="anime", media_id=FRIEREN)
        tomorrow = timezone.now() + timedelta(days=1)
        later = timezone.now() + timedelta(days=5)
        duplicate = Event.objects.create(
            item=season_item, content_number=2, datetime=tomorrow
        )
        Event.objects.create(item=anime_item, content_number=2, datetime=tomorrow)
        tv_only = Event.objects.create(
            item=season_item, content_number=3, datetime=later
        )

        dropped = hooks.duplicate_tv_event_ids(self.user, Event.objects.all())
        self.assertEqual(dropped, [duplicate.id])
        self.assertNotIn(tv_only.id, dropped)

    def test_detail_page_links_both_ways(self, *_mocks):
        """Detail pages show the linked entry."""
        self.watch_tv(1)
        self.watch_anime(1)
        self.link()
        self.client.force_login(self.user)
        with patch("app.views.services.get_media_metadata", side_effect=self.details):
            response = self.client.get(f"/details/mal/anime/{FRIEREN}/frieren")
        self.assertContains(response, "Also tracked as")

    @staticmethod
    def details(media_type, media_id, source, *_args, **_kwargs):
        """Metadata rich enough for the detail template."""
        data = fake_metadata(media_type, media_id, source)
        data.update(
            {
                "media_id": media_id,
                "source": source,
                "media_type": media_type,
                "details": {},
                "related": {},
                "synopsis": "",
            }
        )
        return data
