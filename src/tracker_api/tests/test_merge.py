from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from app.models import TV, Anime, Episode, Season, Status

from .helpers import ApiTestCase, fake_metadata

FRIEREN = "52991"


def fake_tmdb_mapping(_mapping, tmdb_id, season, episode):
    """Map season 1 to Frieren and season 2 episodes to MAL eps 10-12."""
    del tmdb_id
    if season == 1 and episode <= 2:  # noqa: PLR2004
        return FRIEREN, episode
    if season == 2:  # noqa: PLR2004
        return FRIEREN, episode + 9
    return None, None


@patch("tracker_api.merge.mal_from_tmdb_show", fake_tmdb_mapping)
@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class MergeAnimeTvTests(ApiTestCase):
    """manage.py merge_anime_tv."""

    def watch(self, **data):
        """Record an entry through the API."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, 200, response.content)

    def watch_tv(self, season, *episodes):
        """Watch TMDB TV episodes of show 1396."""
        for episode in episodes:
            self.watch(
                media_type="tv",
                source="tmdb",
                media_id="1396",
                season_number=season,
                unit_number=episode,
            )

    def run_merge(self, *args):
        """Run the command for the test user and return its output."""
        out = StringIO()
        call_command("merge_anime_tv", "--user", "tracker", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self, *_mocks):
        """Without --apply the plan is only printed."""
        self.watch_tv(1, 1, 2)
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=1)
        output = self.run_merge()
        self.assertIn("Show S1 -> A anime (MAL 52991)", output)
        self.assertIn("Dry run", output)
        self.assertTrue(TV.objects.filter(user=self.user).exists())
        self.assertEqual(Anime.objects.get(user=self.user).progress, 1)

    def test_merges_duplicate_and_removes_show(self, *_mocks):
        """Watched TV episodes move into the anime entry; the show goes away."""
        self.watch_tv(1, 1, 2)
        first_watch = timezone.now() - timedelta(days=30)
        Episode.objects.filter(item__episode_number=1).update(end_date=first_watch)
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=1)

        self.run_merge("--apply")

        anime = Anime.objects.get(user=self.user)
        self.assertEqual((anime.progress, anime.status), (2, Status.IN_PROGRESS.value))
        self.assertEqual(anime.start_date, first_watch)
        self.assertFalse(TV.objects.filter(user=self.user).exists())
        self.assertFalse(
            Episode.objects.filter(related_season__user=self.user).exists()
        )

    def test_completion_keeps_last_watch_date(self, *_mocks):
        """Completing through the merge uses the TV watch date as end date."""
        self.watch_tv(2, 1, 2, 3)
        last_watch = timezone.now().replace(second=0, microsecond=0) - timedelta(days=3)
        Episode.objects.update(end_date=last_watch - timedelta(days=1))
        Episode.objects.filter(item__episode_number=3).update(end_date=last_watch)
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=9)

        self.run_merge("--apply")

        anime = Anime.objects.get(user=self.user)
        self.assertEqual((anime.progress, anime.status), (12, Status.COMPLETED.value))
        self.assertEqual(anime.end_date, last_watch)

    def test_tv_only_is_skipped_unless_requested(self, *_mocks):
        """Anime tracked only as TV stay unless --include-tv-only is given."""
        self.watch_tv(1, 1, 2)
        output = self.run_merge("--apply")
        self.assertIn("skip Show S1 -> MAL 52991", output)
        self.assertTrue(TV.objects.filter(user=self.user).exists())
        self.assertFalse(Anime.objects.filter(user=self.user).exists())

        self.run_merge("--apply", "--include-tv-only")
        anime = Anime.objects.get(user=self.user)
        self.assertEqual((anime.progress, anime.status), (2, Status.IN_PROGRESS.value))
        self.assertFalse(TV.objects.filter(user=self.user).exists())

    def test_unmapped_episodes_stay(self, *_mocks):
        """Episodes AniBridge cannot map keep their season and show."""
        self.watch_tv(1, 1, 2, 3)  # episode 3 has no mapping
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=1)

        self.run_merge("--apply")

        self.assertEqual(Anime.objects.get(user=self.user).progress, 2)
        season = Season.objects.get(user=self.user)
        self.assertEqual(
            list(season.episodes.values_list("item__episode_number", flat=True)), [3]
        )
        self.assertTrue(TV.objects.filter(user=self.user).exists())

    def test_start_date_never_after_end_date(self, *_mocks):
        """A completed entry keeps an empty start date over later TV dates."""
        self.watch_tv(1, 1, 2)
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=12)
        finished = timezone.now() - timedelta(days=200)
        Anime.objects.filter(user=self.user).update(start_date=None, end_date=finished)

        self.run_merge("--apply")

        anime = Anime.objects.get(user=self.user)
        self.assertIsNone(anime.start_date)
        self.assertEqual(anime.end_date, finished)

    def test_never_lowers_anime_progress(self, *_mocks):
        """An anime entry ahead of the TV side keeps its progress."""
        self.watch_tv(1, 1)
        self.watch(media_type="anime", source="mal", media_id=FRIEREN, unit_number=8)
        self.run_merge("--apply")
        self.assertEqual(Anime.objects.get(user=self.user).progress, 8)
        self.assertFalse(TV.objects.filter(user=self.user).exists())
