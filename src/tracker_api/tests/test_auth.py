from unittest.mock import patch

from app.providers.services import ProviderAPIError

from .helpers import ApiTestCase


class ApiAuthTests(ApiTestCase):
    """Token authentication and error handling."""

    def test_missing_token(self):
        """Requests without a token are rejected."""
        response = self.client.get("/api/tracker/v1/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_token")

    def test_invalid_token(self):
        """Requests with an unknown token are rejected."""
        response = self.client.get(
            "/api/tracker/v1/me", HTTP_AUTHORIZATION="Bearer nope"
        )
        self.assertEqual(response.status_code, 401)

    def test_me(self):
        """The connection test returns the user and API version."""
        response = self.get_json("/api/tracker/v1/me")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["api_version"], 2)
        self.assertIn("game", data["scrobble_types"])
        self.assertEqual(data["username"], "tracker")
        self.assertIn("anime", data["enabled_media_types"])

    def test_wrong_method(self):
        """Only the declared methods are allowed."""
        response = self.post_json("/api/tracker/v1/me", {})
        self.assertEqual(response.status_code, 405)

    def test_invalid_json(self):
        """Malformed bodies return a JSON 400."""
        response = self.client.post(
            "/api/tracker/v1/resolve", "{", content_type="application/json", **self.auth
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_json")

    @patch("tracker_api.resolve.services.get_media_metadata")
    def test_provider_error(self, mock_metadata):
        """Provider failures become a JSON 502 instead of an HTML page."""
        mock_metadata.side_effect = ProviderAPIError("mal", Exception("down"))
        response = self.post_json(
            "/api/tracker/v1/resolve",
            {"kind": "episode", "title": "X", "episode": 1, "ids": {"mal": "1"}},
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "provider_error")

    @patch("tracker_api.views.services.search")
    def test_search(self, mock_search):
        """Search wraps the provider search."""
        mock_search.return_value = {
            "page": 1,
            "total_pages": 1,
            "total_results": 1,
            "results": [
                {
                    "media_id": 52991,
                    "source": "mal",
                    "media_type": "anime",
                    "title": "Sousou no Frieren",
                    "image": "f.jpg",
                }
            ],
        }
        response = self.get_json(
            "/api/tracker/v1/search", {"media_type": "anime", "q": "frieren"}
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["results"][0]
        self.assertEqual(result["media_id"], "52991")
        self.assertIsNone(result["user_status"])
        mock_search.assert_called_once_with(
            "anime", "frieren", 1, show_adult_titles=self.user.show_adult_titles
        )

    def test_search_validates_type(self):
        """Unsupported media types are rejected."""
        response = self.get_json(
            "/api/tracker/v1/search", {"media_type": "boardgame", "q": "x"}
        )
        self.assertEqual(response.status_code, 400)
