import datetime
from unittest.mock import patch

from django.utils import timezone

from app.models import Item, Status, YouTube
from app.providers import services
from tracker_api.models import ScrobbleSession, UsageEvent

from .helpers import ApiTestCase

FOO_ID = "UCaaaaaaaaaaaaaaaaaaaaaa"
BAR_ID = "UCbbbbbbbbbbbbbbbbbbbb-_"
FOO = {"media_type": "youtube", "source": "youtube", "media_id": FOO_ID}
CHANNEL = {
    "name": "Foo",
    "handle": "@foo",
    "image": "https://yt3.ggpht.com/foo=s176",
}
VIDEO = {"id": "dQw4w9WgXcQ", "title": "First video", "kind": "video"}


def report(session_id, minutes, **extra):
    """Extension report for a watch session on the channel Foo."""
    return {
        **FOO,
        "session_id": session_id,
        "minutes": minutes,
        "started_at": "2026-11-15T12:00:00Z",
        "ended_at": "2026-11-15T12:07:00Z",
        "client": "yamtrack-tracker",
        "platform": "youtube",
        "channel": CHANNEL,
        "video": VIDEO,
        **extra,
    }


@patch("events.tasks.reload_calendar.delay")
class YouTubeScrobbleTests(ApiTestCase):
    """POST /api/tracker/v1/scrobble with media_type=youtube."""

    def scrobble(self, data, status=200):
        """Post a scrobble and return the JSON response."""
        response = self.post_json("/api/tracker/v1/scrobble", data)
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_first_report_creates_channel_entry(self, _calendar):
        """The first report creates the channel item and an entry in progress."""
        result = self.scrobble(report("yt-1", 7))

        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "tracked")
        self.assertIsNotNone(result["undo"])
        self.assertEqual(result["state"]["progress"], 7)
        self.assertEqual(result["state"]["status"], Status.IN_PROGRESS.value)
        self.assertIsNone(result["state"]["max_progress"])
        self.assertEqual(result["state"]["start_date"], "2026-11-15T12:00:00+00:00")

        item = Item.objects.get(media_type="youtube")
        self.assertEqual(
            (item.source, item.media_id, item.title, item.image),
            ("youtube", FOO_ID, "Foo", CHANNEL["image"]),
        )
        event = UsageEvent.objects.get(user=self.user)
        self.assertEqual(
            (
                event.media_type,
                event.client,
                event.platform,
                event.minutes,
                event.units,
                event.session_id,
                event.video_id,
                event.video_title,
                event.category_id,
                event.category_name,
            ),
            (
                "youtube",
                "yamtrack-tracker",
                "youtube",
                7,
                0,
                "yt-1",
                "dQw4w9WgXcQ",
                "First video",
                "",
                "",
            ),
        )
        self.assertEqual(
            event.occurred_at,
            datetime.datetime(2026, 11, 15, 12, 7, tzinfo=datetime.UTC),
        )

    def test_cumulative_minutes_per_video(self, _calendar):
        """Only the delta is added; each delta keeps the video of its report."""
        self.scrobble(report("yt-1", 5))
        repeat = self.scrobble(report("yt-1", 5))
        self.assertFalse(repeat["applied"])
        self.assertEqual(repeat["status"], "already_tracked")
        self.assertIsNone(repeat["undo"])
        self.assertFalse(self.scrobble(report("yt-1", 3))["applied"])

        # The video changed inside the session: the next minutes land on it.
        second = {"id": "abcdefghijk", "title": "Second video", "kind": "short"}
        grown = self.scrobble(report("yt-1", 8, video=second))
        self.assertEqual(grown["state"]["progress"], 8)
        other = self.scrobble(report("yt-2", 4))
        self.assertEqual(other["state"]["progress"], 12)

        self.assertEqual(YouTube.objects.get(user=self.user).progress, 12)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="yt-1").minutes_counted, 8
        )
        self.assertEqual(
            sorted(UsageEvent.objects.values_list("video_id", "minutes", "session_id")),
            [
                ("abcdefghijk", 3, "yt-1"),
                ("dQw4w9WgXcQ", 4, "yt-2"),
                ("dQw4w9WgXcQ", 5, "yt-1"),
            ],
        )

    def test_video_fields(self, _calendar):
        """Invalid video ids are ignored; titles are cut to 255 characters."""
        self.scrobble(report("yt-1", 2, video={"id": "not-a-valid-id", "title": "x"}))
        no_video = report("yt-1", 3)
        del no_video["video"]
        self.scrobble(no_video)
        self.scrobble(report("yt-1", 4, video={"id": "dQw4w9WgXc", "title": "short"}))
        self.scrobble(
            report("yt-1", 5, video={"id": "abcdefghijk", "title": "t" * 300})
        )

        events = UsageEvent.objects.order_by("minutes", "id")
        self.assertEqual(
            [(e.video_id, e.video_title, e.minutes) for e in events if not e.video_id],
            [("", "", 1), ("", "", 1), ("", "", 2)],
        )
        titled = UsageEvent.objects.get(video_id="abcdefghijk")
        self.assertEqual(titled.video_title, "t" * 255)
        # The minutes still count when the video is ignored.
        self.assertEqual(YouTube.objects.get(user=self.user).progress, 5)

    def test_undo(self, _calendar):
        """Undo takes back one report, and the first report removes the entry."""
        first = self.scrobble(report("yt-1", 5))
        second = self.scrobble(report("yt-1", 9))
        response = self.post_json("/api/tracker/v1/undo", {"undo": second["undo"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(YouTube.objects.get(user=self.user).progress, 5)
        self.assertEqual(
            ScrobbleSession.objects.get(session_id="yt-1").minutes_counted, 5
        )
        self.assertEqual(UsageEvent.objects.count(), 1)

        self.post_json("/api/tracker/v1/undo", {"undo": first["undo"]})
        self.assertFalse(YouTube.objects.exists())
        self.assertFalse(ScrobbleSession.objects.exists())
        self.assertFalse(UsageEvent.objects.exists())

    def test_session_belongs_to_one_channel(self, _calendar):
        """A session id cannot move to another channel."""
        self.scrobble(report("yt-1", 5))
        other = report("yt-1", 9, media_id=BAR_ID, channel={"name": "Bar"})
        self.assertEqual(self.scrobble(other, status=400)["error"], "session_conflict")
        self.assertEqual(YouTube.objects.get().progress, 5)

    def test_disabled_media_type(self, _calendar):
        """Users who switched YouTube off get 403 and no scrobble_types entry."""
        self.user.youtube_enabled = False
        self.user.save()
        self.assertEqual(
            self.scrobble(report("yt-1", 5), status=403)["error"],
            "media_type_disabled",
        )
        self.assertFalse(Item.objects.filter(media_type="youtube").exists())
        me = self.get_json("/api/tracker/v1/me").json()
        self.assertNotIn("youtube", me["scrobble_types"])

    def test_me_lists_youtube(self, _calendar):
        """YouTube is a scrobble type while it is enabled."""
        me = self.get_json("/api/tracker/v1/me").json()
        self.assertIn("youtube", me["scrobble_types"])
        self.assertIn("youtube", me["enabled_media_types"])
        self.assertEqual(me["api_version"], 5)

    def test_validation(self, _calendar):
        """Session, minutes, source=youtube and a UC channel id are required."""
        no_session = report("yt-1", 5)
        del no_session["session_id"]
        self.assertEqual(self.scrobble(no_session, status=400)["error"], "invalid_unit")
        self.assertEqual(
            self.scrobble(report("yt-1", 0), status=400)["error"], "invalid_unit"
        )
        self.assertEqual(
            self.scrobble(report("yt-1", "x"), status=400)["error"], "invalid_unit"
        )
        self.assertEqual(
            self.scrobble(report("yt-1", 5, source="twitch"), status=400)["error"],
            "invalid_target",
        )
        for media_id in (
            "foo",
            "@foo",
            "UCaaaaaaaaaaaaaaaaaaaaa",  # 21 characters after UC
            "UCaaaaaaaaaaaaaaaaaaaaaaa",  # 23 characters after UC
            "UXaaaaaaaaaaaaaaaaaaaaaa",
            "UCaaaaaaaaaaaaaaaaaaaaa!",
            f"{FOO_ID}\n",
        ):
            with self.subTest(media_id=media_id):
                self.assertEqual(
                    self.scrobble(report("yt-1", 5, media_id=media_id), status=400)[
                        "error"
                    ],
                    "invalid_target",
                )
        self.assertFalse(YouTube.objects.exists())
        self.assertFalse(Item.objects.filter(media_type="youtube").exists())

    def test_first_report_needs_channel_name(self, _calendar):
        """The first report of a channel must carry its name."""
        data = report("yt-1", 5)
        del data["channel"]
        self.assertEqual(self.scrobble(data, status=400)["error"], "missing_channel")
        nameless = report("yt-1", 5, channel={"handle": "@foo", "image": "https://a/b"})
        self.assertEqual(
            self.scrobble(nameless, status=400)["error"], "missing_channel"
        )
        self.assertFalse(Item.objects.filter(media_type="youtube").exists())

        self.scrobble(report("yt-1", 5))
        # Later reports may leave it out.
        self.assertEqual(self.scrobble(data | {"minutes": 6})["state"]["progress"], 6)

    def test_channel_metadata_update(self, _calendar):
        """Changed channel data updates the item title and image."""
        self.scrobble(report("yt-1", 5))
        renamed = {
            "name": "Foo Renamed",
            "handle": "@foorenamed",
            "image": "https://yt3.ggpht.com/new=s176",
        }
        result = self.scrobble(report("yt-1", 5, channel=renamed))
        self.assertFalse(result["applied"])  # metadata changes even without minutes

        item = Item.objects.get(media_type="youtube")
        self.assertEqual((item.title, item.image), ("Foo Renamed", renamed["image"]))

        # Invalid images are ignored instead of replacing a good one.
        for image in ("nope", "javascript:alert(1)", "https://a/" + "x" * 300):
            self.scrobble(report("yt-1", 6, channel={**renamed, "image": image}))
            item.refresh_from_db()
            self.assertEqual(item.image, renamed["image"])

    def test_first_report_with_invalid_image(self, _calendar):
        """A channel without a usable image gets the placeholder."""
        self.scrobble(report("yt-1", 5, channel={"name": "Foo", "image": "ftp://x"}))
        item = Item.objects.get(media_type="youtube")
        self.assertNotEqual(item.image, "ftp://x")
        self.assertTrue(item.image)

    def test_status_rules(self, _calendar):
        """Planned, paused or dropped channels resume, completed ones stay."""
        self.scrobble(report("yt-1", 5))
        entry = YouTube.objects.get(user=self.user)
        for number, status in enumerate(
            (Status.DROPPED.value, Status.PAUSED.value, Status.PLANNING.value), 2
        ):
            entry.status = status
            entry.save()
            self.assertEqual(
                self.scrobble(report(f"yt-{number}", 1))["state"]["status"],
                Status.IN_PROGRESS.value,
            )
            entry.refresh_from_db()

        entry.status = Status.COMPLETED.value
        entry.save()
        state = self.scrobble(report("yt-9", 2))["state"]
        self.assertEqual(
            (state["status"], state["progress"]), (Status.COMPLETED.value, 10)
        )

    def test_progress_endpoint(self, _calendar):
        """Progress reports the watched minutes of a channel."""
        self.assertEqual(
            self.get_json("/api/tracker/v1/progress", FOO).json(), {"tracked": False}
        )
        self.scrobble(report("yt-1", 7))
        state = self.get_json("/api/tracker/v1/progress", FOO).json()
        self.assertEqual(
            (state["tracked"], state["progress"], state["status"]),
            (True, 7, "In progress"),
        )


@patch("events.tasks.reload_calendar.delay")
class YouTubeStatisticsTests(ApiTestCase):
    """YouTube minutes in the activity endpoint, statistics, pages and search."""

    def watch(self, media_id, session_id, minutes, video, name):
        """Report a watch session on a video."""
        response = self.post_json(
            "/api/tracker/v1/scrobble",
            report(
                session_id,
                minutes,
                media_id=media_id,
                ended_at=None,
                channel={"name": name},
                video=video,
            ),
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_activity_endpoint(self, _calendar):
        """YouTube minutes add up per channel, the largest first."""
        self.watch(FOO_ID, "s1", 30, VIDEO, "Foo")
        self.watch(FOO_ID, "s1", 50, VIDEO, "Foo")
        self.watch(BAR_ID, "s2", 70, VIDEO, "Bar")
        today = timezone.localdate().isoformat()

        data = self.get_json(
            "/api/tracker/v1/activity",
            {"media_type": "youtube", "since": today, "until": today},
        ).json()

        self.assertEqual(
            [
                (row["media_id"], row["title"], row["amount"], row["unit"])
                for row in data["items"]
            ],
            [(BAR_ID, "Bar", 70, "minutes"), (FOO_ID, "Foo", 50, "minutes")],
        )

    def test_usage_statistics(self, _calendar):
        """The usage page shows minutes per channel, per week and top videos."""
        second = {"id": "abcdefghijk", "title": "Second video"}
        self.watch(FOO_ID, "s1", 60, VIDEO, "Foo")
        self.watch(FOO_ID, "s1", 90, second, "Foo")
        self.watch(BAR_ID, "s2", 20, {"id": "zzzzzzzzzzz", "title": "Bar video"}, "Bar")
        self.client.force_login(self.user)

        response = self.client.get("/tracker/statistics")

        self.assertEqual(response.status_code, 200)
        youtube = response.context["chart"]["youtube"]
        self.assertEqual(
            youtube["channels"],
            [{"label": "Foo", "minutes": 90}, {"label": "Bar", "minutes": 20}],
        )
        self.assertEqual(len(youtube["weeks"]), 1)
        self.assertEqual(youtube["weeks"][0]["label"], "YouTube")
        self.assertEqual(sum(youtube["weeks"][0]["data"]), 110)
        self.assertEqual(
            [
                (video["id"], video["title"], video["channel"], video["minutes"])
                for video in response.context["youtube_videos"]
            ],
            [
                ("dQw4w9WgXcQ", "First video", "Foo", 60),
                ("abcdefghijk", "Second video", "Foo", 30),
                ("zzzzzzzzzzz", "Bar video", "Bar", 20),
            ],
        )
        chart = response.context["chart"]
        self.assertIn("YouTube", [dataset["label"] for dataset in chart["hours"]])
        total = next(t for t in response.context["totals"] if t["label"] == "YouTube")
        self.assertEqual((total["units"], total["hours"]), (0, 2))
        self.assertContains(response, "usage-youtube-channels")
        self.assertContains(response, "usage-youtube-weeks")
        self.assertContains(response, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_usage_statistics_without_youtube(self, _calendar):
        """Without YouTube watch time the section stays hidden."""
        self.client.force_login(self.user)
        response = self.client.get("/tracker/statistics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["chart"]["youtube"]["channels"], [])
        self.assertEqual(response.context["youtube_videos"], [])
        self.assertNotContains(response, "usage-youtube-channels")

    def test_detail_page_and_list(self, _calendar):
        """Channel pages show the top videos; the list shows the watch time."""
        self.watch(FOO_ID, "s1", 60, VIDEO, "Foo")
        self.watch(FOO_ID, "s1", 90, {"id": "abcdefghijk", "title": "Next"}, "Foo")
        self.client.force_login(self.user)

        details = self.client.get(f"/details/youtube/youtube/{FOO_ID}/foo")
        self.assertEqual(details.status_code, 200)
        self.assertContains(details, f"https://www.youtube.com/channel/{FOO_ID}")
        self.assertContains(details, "Most watched videos")
        self.assertContains(details, "First video")
        self.assertContains(details, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertContains(details, "https://www.youtube.com/watch?v=abcdefghijk")

        listing = self.client.get(f"/{self.user.username}/youtube")
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "Foo")
        self.assertContains(listing, "1h 30min")

    def test_other_pages_render(self, _calendar):
        """Home, statistics and the CSV export work with a YouTube entry."""
        self.watch(FOO_ID, "s1", 120, VIDEO, "Foo")
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/").status_code, 200)
        statistics = self.client.get("/statistics")
        self.assertEqual(statistics.status_code, 200)
        self.assertContains(statistics, "YouTube hours watched")

        export = self.client.get("/export/csv")
        self.assertEqual(export.status_code, 200)
        content = b"".join(export.streaming_content).decode()
        row = next(line for line in content.splitlines() if FOO_ID in line)
        self.assertIn("2h 00min", row)

    def test_detail_page_without_videos(self, _calendar):
        """Channels watched without video data show no video block."""
        no_video = report("s1", 15, ended_at=None)
        del no_video["video"]
        self.post_json("/api/tracker/v1/scrobble", no_video)
        self.client.force_login(self.user)
        details = self.client.get(f"/details/youtube/youtube/{FOO_ID}/foo")
        self.assertEqual(details.status_code, 200)
        self.assertNotContains(details, "Most watched videos")

    def test_search_known_channels(self, _calendar):
        """The search finds channels already known locally."""
        self.watch(FOO_ID, "s1", 10, VIDEO, "Foo Science")
        self.watch(BAR_ID, "s2", 10, VIDEO, "Bar")
        response = self.get_json(
            "/api/tracker/v1/search", {"media_type": "youtube", "q": "scien"}
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(
            [(r["media_id"], r["title"], r["user_progress"]) for r in results],
            [(FOO_ID, "Foo Science", 10)],
        )

    def test_unknown_channel_metadata(self, _calendar):
        """Channels never scrobbled are unknown to the provider."""
        with self.assertRaises(services.ProviderAPIError):
            services.get_media_metadata("youtube", BAR_ID, "youtube")
