from unittest.mock import patch

from django.contrib.auth import get_user_model

from app.models import Game, Item, MediaTypes, Sources, Status
from tracker_api.models import UsageEvent

from .helpers import ApiTestCase

MANUAL_ID = "5f0c2a8e-7d1b-4e2a-9c3f-0123456789ab"


def report(session_id, minutes, media_id=MANUAL_ID):
    """Castellan-style report for a manually created game."""
    return {
        "media_type": "game",
        "source": "manual",
        "media_id": media_id,
        "session_id": session_id,
        "minutes": minutes,
        "started_at": "2026-11-15T18:00:00Z",
        "ended_at": "2026-11-15T18:42:00Z",
        "client": "castellan",
    }


@patch("events.tasks.reload_calendar.delay")
class ManualGameScrobbleTests(ApiTestCase):
    """Session scrobbles against a game the user created manually.

    Runs the real manual provider (it only reads the database), so the
    path Castellan takes for such games is covered end to end.
    """

    def setUp(self):
        """Create the manual game item, as the web form does."""
        super().setUp()
        self.item = Item.objects.create(
            media_id=MANUAL_ID,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="World of Warcraft: Forever",
            image="",
        )
        # Creating a manual game in the web UI also creates the user's entry.
        self.entry = Game.objects.bulk_create(
            [
                Game(
                    item=self.item,
                    user=self.user,
                    progress=0,
                    status=Status.PLANNING.value,
                )
            ]
        )[0]

    def scrobble(self, data, status=200):
        """Post a scrobble and return the JSON response."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_first_report_updates_entry(self, _reload):
        """The first session counts on the user's entry for the manual item."""
        result = self.scrobble(report("m1", 42))
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"]["progress"], 42)
        game = Game.objects.get(user=self.user)
        self.assertEqual(game.item, self.item)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(Item.objects.filter(media_id=MANUAL_ID).count(), 1)

    def test_cumulative_minutes_and_existing_entry(self, _reload):
        """Later reports add only the difference to an existing entry."""
        Game.objects.filter(pk=self.entry.pk).update(
            progress=100, status=Status.PAUSED.value
        )
        self.scrobble(report("m1", 30))
        result = self.scrobble(report("m1", 45))
        self.assertEqual(result["state"]["progress"], 145)
        game = Game.objects.get(user=self.user)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(
            list(
                UsageEvent.objects.filter(item=self.item)
                .order_by("id")
                .values_list("minutes", flat=True)
            ),
            [30, 15],
        )

    def test_repeat_is_idempotent(self, _reload):
        """The same cumulative total twice counts once."""
        self.scrobble(report("m1", 20))
        result = self.scrobble(report("m1", 20))
        self.assertFalse(result["applied"])
        self.assertEqual(Game.objects.get(user=self.user).progress, 20)

    def test_unknown_manual_id(self, _reload):
        """A manual id that does not exist is a 404, and nothing is created."""
        result = self.scrobble(
            report("m1", 10, media_id="00000000-0000-0000-0000-000000000000"),
            status=404,
        )
        self.assertEqual(result["error"], "not_found")
        self.assertEqual(Game.objects.get(user=self.user).progress, 0)
        self.assertFalse(UsageEvent.objects.exists())

    def test_other_users_manual_item(self, _reload):
        """A manual UUID of another user's game is a 404 for everyone else."""
        other = get_user_model().objects.create_user(
            username="other",
            password="12345",  # noqa: S106
        )
        Game.objects.filter(pk=self.entry.pk).update(user=other)
        result = self.scrobble(report("m1", 10), status=404)
        self.assertEqual(result["error"], "not_found")
        self.assertFalse(Game.objects.filter(user=self.user).exists())
        self.assertEqual(Game.objects.get(user=other).progress, 0)
        self.assertFalse(UsageEvent.objects.exists())

    def test_undo(self, _reload):
        """Undo restores the entry and keeps the manual item."""
        result = self.scrobble(report("m1", 42))
        response = self.post_json("/api/tracker/v1/undo", {"undo": result["undo"]})
        self.assertEqual(response.status_code, 200, response.content)
        game = Game.objects.get(user=self.user)
        self.assertEqual(game.progress, 0)
        self.assertEqual(game.status, Status.PLANNING.value)
        self.assertTrue(Item.objects.filter(media_id=MANUAL_ID).exists())
