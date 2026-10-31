import json

from django.contrib.auth import get_user_model
from django.test import TestCase


def season_metadata(episodes):
    """Minimal TMDB season metadata with the given episode numbers."""
    return {
        "title": "Show",
        "image": "season.jpg",
        "episodes": [
            {"episode_number": number, "air_date": "2020-01-01"} for number in episodes
        ],
        "max_progress": len(episodes),
    }


def fake_metadata(
    media_type, media_id, source, season_numbers=None, episode_number=None
):
    """Stand-in for services.get_media_metadata without network access."""
    del media_id, source, episode_number
    if media_type in ("anime", "manga"):
        return {"title": f"A {media_type}", "image": "a.jpg", "max_progress": 12}
    if media_type == "movie":
        return {"title": "A movie", "image": "m.jpg", "max_progress": 1}
    if media_type == "tv":
        return {
            "title": "Show",
            "image": "tv.jpg",
            "max_progress": 3,
            "tvdb_id": 81189,
            "related": {"seasons": []},
        }
    if media_type == "season":
        return season_metadata([1, 2, 3])
    if media_type == "tv_with_seasons":
        data = {"title": "Show", "image": "tv.jpg", "max_progress": 3}
        for number in season_numbers or [1]:
            data[f"season/{number}"] = season_metadata([1, 2, 3])
        return data
    raise AssertionError(media_type)


class ApiTestCase(TestCase):
    """Test case with a user and authenticated JSON helpers."""

    def setUp(self):
        """Create a user with a known token."""
        self.user = get_user_model().objects.create_user(
            username="tracker",
            password="12345",  # noqa: S106
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.user.token}"}

    def get_json(self, path, params=None, **extra):
        """GET with the user's token."""
        return self.client.get(path, params or {}, **{**self.auth, **extra})

    def post_json(self, path, data, **extra):
        """POST JSON with the user's token."""
        return self.client.post(
            path,
            json.dumps(data),
            content_type="application/json",
            **{**self.auth, **extra},
        )
