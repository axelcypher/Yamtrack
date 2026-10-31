from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone

from api import scrobble as scrobble_module
from app.models import TV, Anime, Episode, Manga, Movie, Season, Status

from .helpers import ApiTestCase, fake_metadata


@patch("events.tasks.reload_calendar.delay")
@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class ScrobbleTests(ApiTestCase):
    """POST /api/v1/scrobble and /api/v1/undo."""

    def scrobble(self, **data):
        """Post a scrobble and return the JSON response."""
        response = self.post_json("/api/v1/scrobble", data)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def undo(self, token):
        """Post an undo token."""
        return self.post_json("/api/v1/undo", {"undo": token})

    def test_anime_progress_and_undo(self, *_mocks):
        """Anime progress moves forward, completes at the end and undoes cleanly."""
        first = self.scrobble(
            media_type="anime", source="mal", media_id="52991", unit_number=3
        )
        self.assertTrue(first["applied"])
        self.assertEqual(first["state"]["progress"], 3)
        self.assertEqual(first["state"]["status"], Status.IN_PROGRESS.value)

        backwards = self.scrobble(
            media_type="anime", source="mal", media_id="52991", unit_number=2
        )
        self.assertFalse(backwards["applied"])
        self.assertEqual(backwards["reason"], "already_tracked")
        self.assertIsNone(backwards["undo"])

        last = self.scrobble(
            media_type="anime", source="mal", media_id="52991", unit_number=12
        )
        self.assertEqual(last["state"]["status"], Status.COMPLETED.value)

        self.assertEqual(self.undo(last["undo"]).status_code, 200)
        anime = Anime.objects.get(user=self.user)
        self.assertEqual((anime.progress, anime.status), (3, Status.IN_PROGRESS.value))
        self.assertIsNone(anime.end_date)

        self.undo(first["undo"])
        self.assertFalse(Anime.objects.filter(user=self.user).exists())

    def test_planning_entry_starts(self, *_mocks):
        """A planned entry switches to in progress with a start date."""
        self.scrobble(media_type="anime", source="mal", media_id="1", unit_number=1)
        anime = Anime.objects.get(user=self.user)
        anime.status = Status.PLANNING.value
        anime.progress = 0
        anime.start_date = None
        anime.save()

        result = self.scrobble(
            media_type="anime", source="mal", media_id="1", unit_number=1
        )
        anime.refresh_from_db()
        self.assertTrue(result["applied"])
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertIsNotNone(anime.start_date)

        self.undo(result["undo"])
        anime.refresh_from_db()
        self.assertEqual(
            (anime.status, anime.progress, anime.start_date),
            (Status.PLANNING.value, 0, None),
        )

    def test_completed_entry_restarts_only_at_one(self, *_mocks):
        """A completed entry starts a rewatch on unit 1 and ignores others."""
        self.scrobble(media_type="anime", source="mal", media_id="1", unit_number=12)
        self.assertFalse(
            self.scrobble(
                media_type="anime", source="mal", media_id="1", unit_number=5
            )["applied"]
        )
        self.assertTrue(
            self.scrobble(
                media_type="anime", source="mal", media_id="1", unit_number=1
            )["applied"]
        )
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 2)

    def test_manga(self, *_mocks):
        """Manga chapters update progress."""
        result = self.scrobble(
            media_type="manga", source="mal", media_id="126287", unit_number=4
        )
        self.assertTrue(result["applied"])
        self.assertEqual(Manga.objects.get(user=self.user).progress, 4)

    def test_movie(self, *_mocks):
        """Movies are completed once; a quick repeat is not a rewatch."""
        result = self.scrobble(media_type="movie", source="tmdb", media_id="27205")
        self.assertTrue(result["applied"])
        self.assertEqual(
            Movie.objects.get(user=self.user).status, Status.COMPLETED.value
        )
        self.assertFalse(
            self.scrobble(media_type="movie", source="tmdb", media_id="27205")[
                "applied"
            ]
        )

        Movie.objects.filter(user=self.user).update(
            end_date=timezone.now() - timedelta(days=2)
        )
        self.assertTrue(
            self.scrobble(media_type="movie", source="tmdb", media_id="27205")[
                "applied"
            ]
        )
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 2)

    def test_tv_episode_and_undo(self, *_mocks):
        """TV episodes create the show, season and episode; undo removes them."""
        result = self.scrobble(
            media_type="tv",
            source="tmdb",
            media_id="1396",
            season_number=1,
            unit_number=2,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"]["watched_episodes"], [2])
        self.assertTrue(Episode.objects.filter(related_season__user=self.user).exists())

        repeat = self.scrobble(
            media_type="tv",
            source="tmdb",
            media_id="1396",
            season_number=1,
            unit_number=2,
        )
        self.assertFalse(repeat["applied"])

        self.undo(result["undo"])
        self.assertFalse(
            Episode.objects.filter(related_season__user=self.user).exists()
        )
        self.assertFalse(Season.objects.filter(user=self.user).exists())
        self.assertFalse(TV.objects.filter(user=self.user).exists())

    def test_tv_undo_keeps_existing_show(self, *_mocks):
        """Undo on an existing show only removes the new episode."""
        first = self.scrobble(
            media_type="tv",
            source="tmdb",
            media_id="1396",
            season_number=1,
            unit_number=1,
        )
        second = self.scrobble(
            media_type="tv",
            source="tmdb",
            media_id="1396",
            season_number=1,
            unit_number=2,
        )
        self.undo(second["undo"])
        self.assertEqual(
            list(
                Episode.objects.filter(related_season__user=self.user).values_list(
                    "item__episode_number", flat=True
                )
            ),
            [1],
        )
        self.assertTrue(first["applied"])
        self.assertTrue(TV.objects.filter(user=self.user).exists())

    def test_unknown_episode(self, *_mocks):
        """Episodes the season does not have are rejected."""
        response = self.post_json(
            "/api/v1/scrobble",
            {
                "media_type": "tv",
                "source": "tmdb",
                "media_id": "1396",
                "season_number": 1,
                "unit_number": 9,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unknown_episode")

    def test_validation(self, *_mocks):
        """Episodes and chapters need a positive unit number."""
        response = self.post_json(
            "/api/v1/scrobble",
            {"media_type": "anime", "source": "mal", "media_id": "1"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.post_json(
            "/api/v1/scrobble",
            {"media_type": "game", "source": "igdb", "media_id": "1", "unit_number": 1},
        )
        self.assertEqual(response.status_code, 400)

    def test_undo_rejects_foreign_and_bad_tokens(self, *_mocks):
        """Undo tokens are bound to the user and must be valid."""
        result = self.scrobble(
            media_type="anime", source="mal", media_id="1", unit_number=1
        )
        other = get_user_model().objects.create_user(username="other", password="12345")  # noqa: S106
        response = self.client.post(
            "/api/v1/undo",
            {"undo": result["undo"]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {other.token}",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.undo("garbage").status_code, 400)
        self.assertTrue(Anime.objects.filter(user=self.user).exists())

    def test_undo_expired(self, *_mocks):
        """Undo tokens expire after an hour."""
        result = self.scrobble(
            media_type="anime", source="mal", media_id="1", unit_number=1
        )
        with patch.object(
            scrobble_module._signer,
            "unsign_object",
            side_effect=signing.SignatureExpired("old"),
        ):
            response = self.undo(result["undo"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "undo_expired")

    def test_progress_endpoint(self, *_mocks):
        """The progress endpoint reports untracked and tracked items."""
        response = self.get_json(
            "/api/v1/progress",
            {"media_type": "anime", "source": "mal", "media_id": "1"},
        )
        self.assertEqual(response.json(), {"tracked": False})
        self.scrobble(media_type="anime", source="mal", media_id="1", unit_number=2)
        data = self.get_json(
            "/api/v1/progress",
            {"media_type": "anime", "source": "mal", "media_id": "1"},
        ).json()
        self.assertEqual(
            (data["tracked"], data["progress"], data["max_progress"]), (True, 2, 12)
        )
