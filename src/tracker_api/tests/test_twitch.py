import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from app.models import Item, Status, Twitch
from tracker_api.models import ScrobbleSession, TwitchChannel, UsageEvent

from .helpers import ApiTestCase

XQC = {"media_type": "twitch", "source": "twitch", "media_id": "71092938"}
CHANNEL = {
    "login": "xqc",
    "display_name": "xQc",
    "image": "https://static-cdn.jtvnw.net/xqc-300x300.png",
}
JUST_CHATTING = {"id": "509658", "name": "Just Chatting"}


def report(session_id, minutes, **extra):
    """Cavea-style report for a watch session of xQc."""
    return {
        **XQC,
        "session_id": session_id,
        "minutes": minutes,
        "started_at": "2026-11-14T18:00:00Z",
        "ended_at": "2026-11-14T18:42:00Z",
        "client": "cavea",
        "platform": "Cavea",
        "channel": CHANNEL,
        "category": JUST_CHATTING,
        "stream": {"id": "40000000000", "title": "stream"},
        **extra,
    }


@patch("events.tasks.reload_calendar.delay")
class TwitchScrobbleTests(ApiTestCase):
    """POST /api/tracker/v1/scrobble with media_type=twitch."""

    def setUp(self):
        """Start without cached Helix data."""
        super().setUp()
        cache.clear()

    def scrobble(self, data, status=200):
        """Post a scrobble and return the JSON response."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_first_report_creates_channel_entry(self, _calendar):
        """The first report creates the channel item and an entry in progress."""
        result = self.scrobble(report("cavea-1", 42))

        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "tracked")
        self.assertIsNotNone(result["undo"])
        self.assertEqual(result["state"]["progress"], 42)
        self.assertEqual(result["state"]["status"], Status.IN_PROGRESS.value)
        self.assertIsNone(result["state"]["max_progress"])
        self.assertEqual(result["state"]["start_date"], "2026-11-14T18:00:00+00:00")

        item = Item.objects.get(media_type="twitch")
        self.assertEqual(
            (item.source, item.media_id, item.title, item.image),
            ("twitch", "71092938", "xQc", CHANNEL["image"]),
        )
        self.assertEqual(TwitchChannel.objects.get(item=item).login, "xqc")
        event = UsageEvent.objects.get(user=self.user)
        self.assertEqual(
            (
                event.media_type,
                event.client,
                event.platform,
                event.minutes,
                event.units,
                event.session_id,
                event.category_id,
                event.category_name,
            ),
            ("twitch", "cavea", "Cavea", 42, 0, "cavea-1", "509658", "Just Chatting"),
        )
        self.assertEqual(
            event.occurred_at,
            datetime.datetime(2026, 11, 14, 18, 42, tzinfo=datetime.UTC),
        )

    def test_cumulative_minutes_and_categories(self, _calendar):
        """Only the delta is added; each delta keeps the category of its report."""
        self.scrobble(report("cavea-1", 30))
        repeat = self.scrobble(report("cavea-1", 30))
        self.assertFalse(repeat["applied"])
        self.assertEqual(repeat["status"], "already_tracked")
        self.assertIsNone(repeat["undo"])
        self.assertFalse(self.scrobble(report("cavea-1", 25))["applied"])

        grown = self.scrobble(
            report(
                "cavea-1", 45, category={"id": "32982", "name": "Grand Theft Auto V"}
            )
        )
        self.assertEqual(grown["state"]["progress"], 45)
        other = self.scrobble(report("ext-2", 10, client="yamtrack-tracker"))
        self.assertEqual(other["state"]["progress"], 55)

        self.assertEqual(Twitch.objects.get(user=self.user).progress, 55)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="cavea-1").minutes_counted, 45
        )
        self.assertEqual(
            sorted(
                UsageEvent.objects.values_list("category_name", "minutes", "client")
            ),
            [
                ("Grand Theft Auto V", 15, "cavea"),
                ("Just Chatting", 10, "yamtrack-tracker"),
                ("Just Chatting", 30, "cavea"),
            ],
        )

    def test_category_is_optional(self, _calendar):
        """Reports without category store an empty category."""
        data = report("cavea-1", 5)
        del data["category"]
        self.scrobble(data)
        event = UsageEvent.objects.get()
        self.assertEqual((event.category_id, event.category_name), ("", ""))

    def test_undo(self, _calendar):
        """Undo takes back one report, and the first report removes the entry."""
        first = self.scrobble(report("cavea-1", 30))
        second = self.scrobble(report("cavea-1", 50))
        response = self.post_json("/api/tracker/v1/undo", {"undo": second["undo"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Twitch.objects.get(user=self.user).progress, 30)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="cavea-1").minutes_counted, 30
        )
        self.assertEqual(UsageEvent.objects.count(), 1)

        self.post_json("/api/tracker/v1/undo", {"undo": first["undo"]})
        self.assertFalse(Twitch.objects.exists())
        self.assertFalse(ScrobbleSession.objects.exists())
        self.assertFalse(UsageEvent.objects.exists())

    def test_session_belongs_to_one_channel(self, _calendar):
        """A session id cannot move to another channel."""
        self.scrobble(report("cavea-1", 10))
        other = report("cavea-1", 20, media_id="12345", channel={"login": "other"})
        self.assertEqual(self.scrobble(other, status=400)["error"], "session_conflict")
        self.assertEqual(Twitch.objects.get().progress, 10)

    def test_disabled_media_type(self, _calendar):
        """Users who switched Twitch off get 403 and no scrobble_types entry."""
        self.user.twitch_enabled = False
        self.user.save()
        self.assertEqual(
            self.scrobble(report("cavea-1", 10), status=403)["error"],
            "media_type_disabled",
        )
        self.assertFalse(Item.objects.filter(media_type="twitch").exists())
        me = self.get_json("/api/tracker/v1/me").json()
        self.assertNotIn("twitch", me["scrobble_types"])

    def test_me_lists_twitch(self, _calendar):
        """Twitch is a scrobble type while it is enabled."""
        me = self.get_json("/api/tracker/v1/me").json()
        self.assertIn("twitch", me["scrobble_types"])
        self.assertIn("twitch", me["enabled_media_types"])
        self.assertEqual(me["api_version"], 5)

    def test_validation(self, _calendar):
        """Session, minutes, source and a numeric channel id are required."""
        no_session = report("cavea-1", 10)
        del no_session["session_id"]
        self.assertEqual(self.scrobble(no_session, status=400)["error"], "invalid_unit")
        self.assertEqual(
            self.scrobble(report("cavea-1", 0), status=400)["error"], "invalid_unit"
        )
        self.assertEqual(
            self.scrobble(report("cavea-1", 5, source="igdb"), status=400)["error"],
            "invalid_target",
        )
        self.assertEqual(
            self.scrobble(report("cavea-1", 5, media_id="xqc"), status=400)["error"],
            "invalid_target",
        )
        self.assertFalse(Twitch.objects.exists())

    def test_first_report_needs_channel(self, _calendar):
        """Without Helix credentials the first report must carry the channel."""
        data = report("cavea-1", 10)
        del data["channel"]
        self.assertEqual(self.scrobble(data, status=400)["error"], "missing_channel")

        self.scrobble(report("cavea-1", 10))
        # Later reports may leave it out.
        self.assertEqual(self.scrobble(data | {"minutes": 12})["state"]["progress"], 12)

    def test_channel_metadata_update(self, _calendar):
        """Changed channel data updates the item title, image and login."""
        self.scrobble(report("cavea-1", 10))
        renamed = {
            "login": "xqcow",
            "display_name": "xQcOW",
            "image": "https://static-cdn.jtvnw.net/new-300x300.png",
        }
        result = self.scrobble(report("cavea-1", 10, channel=renamed))
        self.assertFalse(result["applied"])  # metadata changes even without minutes

        item = Item.objects.get(media_type="twitch")
        self.assertEqual((item.title, item.image), ("xQcOW", renamed["image"]))
        self.assertEqual(TwitchChannel.objects.get(item=item).login, "xqcow")

        # Invalid images are ignored instead of replacing a good one.
        self.scrobble(report("cavea-1", 11, channel={**renamed, "image": "nope"}))
        item.refresh_from_db()
        self.assertEqual(item.image, renamed["image"])

    def test_status_rules(self, _calendar):
        """Paused or dropped channels resume, completed ones stay completed."""
        self.scrobble(report("cavea-1", 10))
        entry = Twitch.objects.get(user=self.user)
        entry.status = Status.DROPPED.value
        entry.save()
        self.assertEqual(
            self.scrobble(report("cavea-2", 5))["state"]["status"],
            Status.IN_PROGRESS.value,
        )

    def test_progress_endpoint(self, _calendar):
        """Progress reports the watched minutes of a channel."""
        self.assertEqual(
            self.get_json("/api/tracker/v1/progress", XQC).json(), {"tracked": False}
        )
        self.scrobble(report("cavea-1", 42))
        state = self.get_json("/api/tracker/v1/progress", XQC).json()
        self.assertEqual(
            (state["tracked"], state["progress"], state["status"]),
            (True, 42, "In progress"),
        )

    @override_settings(TWITCH_CLIENT_ID="id", TWITCH_CLIENT_SECRET="secret")  # noqa: S106
    @patch("app.providers.twitch.helix")
    def test_first_report_from_helix(self, mock_helix, _calendar):
        """With Helix credentials the channel data is looked up when missing."""
        mock_helix.return_value = {
            "data": [
                {
                    "id": "71092938",
                    "login": "xqc",
                    "display_name": "xQc",
                    "profile_image_url": "https://example.com/xqc.png",
                    "description": "Streamer",
                    "broadcaster_type": "partner",
                }
            ]
        }
        data = report("cavea-1", 10)
        del data["channel"]
        self.scrobble(data)
        item = Item.objects.get(media_type="twitch")
        self.assertEqual(
            (item.title, item.image), ("xQc", "https://example.com/xqc.png")
        )
        self.assertEqual(TwitchChannel.objects.get(item=item).login, "xqc")


@patch("events.tasks.reload_calendar.delay")
class TwitchStatisticsTests(ApiTestCase):
    """Twitch minutes in the activity endpoint, usage statistics and search."""

    def setUp(self):
        """Start without cached Helix data."""
        super().setUp()
        cache.clear()

    def watch(self, media_id, session_id, minutes, category, login):
        """Report a watch session."""
        response = self.post_json(
            "/api/tracker/v1/scrobble",
            report(
                session_id,
                minutes,
                media_id=media_id,
                ended_at=None,
                channel={"login": login, "display_name": login.title()},
                category={"id": "1", "name": category},
            ),
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_activity_endpoint(self, _calendar):
        """Twitch minutes add up per channel, the largest first."""
        self.watch("1", "s1", 30, "Just Chatting", "alpha")
        self.watch("1", "s1", 50, "Minecraft", "alpha")
        self.watch("2", "s2", 70, "Chess", "beta")
        today = timezone.localdate().isoformat()

        data = self.get_json(
            "/api/tracker/v1/activity",
            {"media_type": "twitch", "since": today, "until": today},
        ).json()

        self.assertEqual(
            [
                (row["media_id"], row["title"], row["amount"], row["unit"])
                for row in data["items"]
            ],
            [("2", "Beta", 70, "minutes"), ("1", "Alpha", 50, "minutes")],
        )

    def test_usage_statistics(self, _calendar):
        """The usage page shows Twitch hours and the time per category."""
        self.watch("1", "s1", 90, "Just Chatting", "alpha")
        self.watch("2", "s2", 30, "Chess", "beta")
        self.client.force_login(self.user)

        response = self.client.get("/tracker/statistics")

        self.assertEqual(response.status_code, 200)
        chart = response.context["chart"]
        self.assertEqual(
            chart["categories"],
            [
                {"label": "Just Chatting", "hours": 1.5},
                {"label": "Chess", "hours": 0.5},
            ],
        )
        self.assertIn("Twitch", [dataset["label"] for dataset in chart["hours"]])
        total = next(t for t in response.context["totals"] if t["label"] == "Twitch")
        self.assertEqual((total["units"], total["hours"]), (0, 2))
        self.assertContains(response, "usage-categories")

    def test_detail_page_and_list(self, _calendar):
        """Channel pages and the list work without Helix credentials."""
        self.watch("1", "s1", 90, "Just Chatting", "alpha")
        self.client.force_login(self.user)

        details = self.client.get("/details/twitch/twitch/1/alpha")
        self.assertEqual(details.status_code, 200)
        self.assertContains(details, "Just Chatting")
        self.assertContains(details, "https://www.twitch.tv/alpha")

        listing = self.client.get(f"/{self.user.username}/twitch")
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "Alpha")

    def test_search_known_channels(self, _calendar):
        """Without Helix credentials the search finds channels already known."""
        self.watch("1", "s1", 10, "Chess", "alpha")
        response = self.get_json(
            "/api/tracker/v1/search", {"media_type": "twitch", "q": "alp"}
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()["results"][0]
        self.assertEqual(
            (result["media_id"], result["title"], result["user_progress"]),
            ("1", "Alpha", 10),
        )


@override_settings(TWITCH_CLIENT_ID="id", TWITCH_CLIENT_SECRET="secret")  # noqa: S106
class TwitchProviderTests(ApiTestCase):
    """Helix lookups of the Twitch provider."""

    def setUp(self):
        """Start without a cached token."""
        super().setUp()
        cache.clear()

    @patch("app.providers.services.api_request")
    def test_search_and_metadata(self, mock_request):
        """Search and metadata use an app access token and Helix."""

        def respond(provider, method, url, **kwargs):
            del provider
            if method == "POST":
                return {"access_token": "token", "expires_in": 3600}
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")
            if url.endswith("search/channels"):
                return {
                    "data": [
                        {
                            "id": "71092938",
                            "broadcaster_login": "xqc",
                            "display_name": "xQc",
                            "thumbnail_url": "https://example.com/xqc.png",
                        }
                    ],
                    "pagination": {},
                }
            return {
                "data": [
                    {
                        "id": "71092938",
                        "login": "xqc",
                        "display_name": "xQc",
                        "profile_image_url": "https://example.com/xqc.png",
                        "description": "Streamer",
                        "broadcaster_type": "partner",
                        "created_at": "2014-09-12T23:50:05Z",
                    }
                ]
            }

        mock_request.side_effect = respond
        from app.providers import services  # noqa: PLC0415

        results = services.search("twitch", "xqc", 1)["results"]
        self.assertEqual(
            (results[0]["media_id"], results[0]["title"]), ("71092938", "xQc")
        )

        metadata = services.get_media_metadata("twitch", "71092938", "twitch")
        self.assertEqual(metadata["source_url"], "https://www.twitch.tv/xqc")
        self.assertEqual(metadata["synopsis"], "Streamer")
        self.assertEqual(
            metadata["details"],
            {"login": "xqc", "broadcaster_type": "Partner", "created_at": "2014-09-12"},
        )
        # One token request for both calls.
        posts = [c for c in mock_request.call_args_list if c.args[1] == "POST"]
        self.assertEqual(len(posts), 1)
