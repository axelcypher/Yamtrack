from unittest.mock import patch

from app.models import Game, Status
from app.providers.services import ProviderAPIError
from tracker_api.models import ScrobbleSession, UsageEvent

from .helpers import ApiTestCase, fake_metadata

HADES = {"media_type": "game", "source": "igdb", "media_id": "113112"}


def report(session_id, minutes, **extra):
    """Castellan-style report for a play session of Hades."""
    return {
        **HADES,
        "session_id": session_id,
        "minutes": minutes,
        "started_at": "2026-11-03T18:00:00Z",
        "ended_at": "2026-11-03T18:42:00Z",
        "client": "castellan",
        **extra,
    }


@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class GameScrobbleTests(ApiTestCase):
    """POST /api/tracker/v1/scrobble with media_type=game."""

    def scrobble(self, data, status=200):
        """Post a scrobble and return the JSON response."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_first_report_creates_entry(self, *_mocks):
        """A new game starts in progress with the session minutes."""
        result = self.scrobble(report("s1", 42))
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"]["progress"], 42)
        self.assertEqual(result["state"]["status"], Status.IN_PROGRESS.value)
        self.assertEqual(result["state"]["start_date"], "2026-11-03T18:00:00+00:00")
        event = UsageEvent.objects.get(user=self.user)
        self.assertEqual(
            (event.client, event.minutes, event.units, event.session_id),
            ("castellan", 42, 0, "s1"),
        )

    def test_cumulative_minutes_count_once(self, *_mocks):
        """Repeated and growing reports of one session only add the delta."""
        self.scrobble(report("s1", 30))
        repeat = self.scrobble(report("s1", 30))
        self.assertFalse(repeat["applied"])
        self.assertEqual(repeat["reason"], "already_tracked")
        self.assertIsNone(repeat["undo"])
        self.assertFalse(self.scrobble(report("s1", 20))["applied"])

        grown = self.scrobble(report("s1", 75))
        self.assertTrue(grown["applied"])
        self.assertEqual(grown["state"]["progress"], 75)

        other = self.scrobble(report("s2", 10))
        self.assertEqual(other["state"]["progress"], 85)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="s1").minutes_counted, 75
        )
        self.assertEqual(sum(UsageEvent.objects.values_list("minutes", flat=True)), 85)

    def test_status_rules(self, *_mocks):
        """Paused entries resume; completed entries stay completed."""
        self.scrobble(report("s1", 10))
        game = Game.objects.get(user=self.user)
        game.status = Status.PAUSED.value
        game.save()
        self.assertEqual(
            self.scrobble(report("s2", 5))["state"]["status"], Status.IN_PROGRESS.value
        )

        game.refresh_from_db()
        game.status = Status.COMPLETED.value
        game.save()
        result = self.scrobble(report("s3", 20))
        self.assertEqual(result["state"]["status"], Status.COMPLETED.value)
        self.assertEqual(result["state"]["progress"], 35)

    def test_undo_takes_back_only_that_report(self, *_mocks):
        """Undo removes the delta of one report and rewinds the session."""
        self.scrobble(report("s1", 30))
        second = self.scrobble(report("s1", 50))
        response = self.post_json("/api/tracker/v1/undo", {"undo": second["undo"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Game.objects.get(user=self.user).progress, 30)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="s1").minutes_counted, 30
        )
        self.assertEqual(UsageEvent.objects.count(), 1)
        # The session continues from its previous total afterwards.
        self.assertEqual(self.scrobble(report("s1", 40))["state"]["progress"], 40)

    def test_validation(self, *_mocks):
        """Session id and positive minutes are required."""
        no_session = report("s1", 10)
        del no_session["session_id"]
        self.assertEqual(self.scrobble(no_session, status=400)["error"], "invalid_unit")
        self.assertEqual(
            self.scrobble(report("s1", 0), status=400)["error"], "invalid_unit"
        )
        self.assertEqual(
            self.scrobble(report("s1", 5, started_at="yesterday"), status=400)["error"],
            "invalid_date",
        )

    def test_session_belongs_to_one_game(self, *_mocks):
        """A session id cannot move to another game."""
        self.scrobble(report("s1", 10))
        other = {**report("s1", 20), "media_id": "1"}
        self.assertEqual(self.scrobble(other, status=400)["error"], "session_conflict")

    def test_disabled_media_type(self, *_mocks):
        """Users who switched games off get 403 and no scrobble_types entry."""
        self.user.game_enabled = False
        self.user.save()
        self.assertEqual(
            self.scrobble(report("s1", 10), status=403)["error"],
            "media_type_disabled",
        )
        me = self.get_json("/api/tracker/v1/me").json()
        self.assertNotIn("game", me["scrobble_types"])

    def test_unknown_game(self, mock_metadata, _calendar):
        """IGDB 404s become an unknown_media error."""
        error = ProviderAPIError("igdb", Exception("nope"))
        error.status_code = 404
        mock_metadata.side_effect = error
        self.assertEqual(
            self.scrobble(report("s1", 10), status=404)["error"], "unknown_media"
        )

    def test_progress_endpoint(self, *_mocks):
        """Progress reports minutes and dates for games."""
        params = {**HADES}
        self.assertEqual(
            self.get_json("/api/tracker/v1/progress", params).json(),
            {"tracked": False},
        )
        self.scrobble(report("s1", 42))
        state = self.get_json("/api/tracker/v1/progress", params).json()
        self.assertEqual((state["progress"], state["status"]), (42, "In progress"))

    def test_video_usage_is_recorded(self, *_mocks):
        """Episode scrobbles store the platform and watch time from the client."""
        result = self.scrobble(
            {
                "media_type": "anime",
                "source": "mal",
                "media_id": "52991",
                "unit_number": 1,
                "usage": {
                    "client": "extension",
                    "platform": "crunchyroll",
                    "minutes": 24,
                },
            }
        )
        event = UsageEvent.objects.get(user=self.user)
        self.assertEqual(
            (event.platform, event.minutes, event.units, event.media_type),
            ("crunchyroll", 24, 1, "anime"),
        )
        self.post_json("/api/tracker/v1/undo", {"undo": result["undo"]})
        self.assertFalse(UsageEvent.objects.exists())


class GameSearchTests(ApiTestCase):
    """GET /api/tracker/v1/search?media_type=game."""

    @patch("tracker_api.views.services.search")
    def test_search_games(self, mock_search):
        """Games are searchable through IGDB."""
        mock_search.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": 113112,
                    "source": "igdb",
                    "media_type": "game",
                    "title": "Hades",
                    "image": "h.jpg",
                }
            ],
        }
        response = self.get_json(
            "/api/tracker/v1/search", {"media_type": "game", "q": "Hades"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["media_id"], "113112")
