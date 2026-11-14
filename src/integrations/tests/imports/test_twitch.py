import datetime
import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import Item, MediaTypes, Sources, Twitch
from integrations.imports import twitch
from integrations.imports.helpers import MediaImportError
from tracker_api.models import TwitchChannel, UsageEvent

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
UTC = datetime.UTC

# The raw layout of older exports: one row per watched minute, many columns.
RAW_CSV = """time,channel,channel_id,minutes_logged,live,game,platform,player
2024-02-01 20:00:10.123,pokimane,44445592,1,t,Just Chatting,web,site
2024-02-01 20:01:10.456,pokimane,44445592,1,t,Just Chatting,web,site
2024-02-01 20:02:10.789,pokimane,44445592,1,t,Fortnite,web,site
2024-02-02 08:00:00,pokimane,44445592,1,f,Fortnite,ios,mobile
"""


def channel_item(media_id, title, login=None):
    """Create a known Twitch channel item."""
    item = Item.objects.create(
        media_id=media_id,
        source=Sources.TWITCH.value,
        media_type=MediaTypes.TWITCH.value,
        title=title,
        image="https://example.com/a.png",
    )
    if login:
        TwitchChannel.objects.create(item=item, login=login)
    return item


def zipped(path, content):
    """Return a zip archive with one file, like the Twitch export."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("request/account/info/login.csv", "login\nme\n")
        archive.writestr(path, content)
    buffer.seek(0)
    return buffer


@patch("events.tasks.reload_calendar.delay")
class ImportTwitchExportTests(TestCase):
    """Import the watch history of the Twitch data export."""

    def setUp(self):
        """Create a user and the channels already known locally."""
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",  # noqa: S106
        )
        self.xqc = channel_item("71092938", "xQc", login="xqc")
        # Known by title only (e.g. imported from a Yamtrack CSV).
        self.shroud = channel_item("37402112", "shroud")

    def run_import(self, file=None):
        """Import the aggregated fixture (or another file)."""
        if file is None:
            with Path(mock_path / "import_twitch_minute_watched.csv").open("rb") as f:
                return twitch.importer(f, self.user, "new")
        return twitch.importer(file, self.user, "new")

    def test_aggregated_layout(self, _calendar):
        """Minutes are summed per channel, with one event per day and category."""
        counts, warnings = self.run_import()

        self.assertEqual(counts, {"twitch": 2})
        xqc = Twitch.objects.get(user=self.user, item=self.xqc)
        self.assertEqual(xqc.progress, 90 + 30 + 16)
        self.assertEqual(xqc.status, "In progress")
        self.assertEqual(xqc.start_date, datetime.datetime(2024, 3, 1, 12, tzinfo=UTC))
        self.assertEqual(Twitch.objects.get(item=self.shroud).progress, 60)

        events = UsageEvent.objects.filter(item=self.xqc).order_by(
            "occurred_at", "category_name"
        )
        self.assertEqual(
            [
                (e.occurred_at.date().isoformat(), e.category_name, e.minutes)
                for e in events
            ],
            [
                ("2024-03-01", "Grand Theft Auto V", 30),
                ("2024-03-01", "Just Chatting", 90),
                ("2024-03-02", "Just Chatting", 16),
            ],
        )
        self.assertEqual(
            {(e.client, e.platform, e.session_id) for e in events},
            {("twitch-export", "web", ""), ("twitch-export", "android", "")},
        )
        # unknownguy can't be resolved without Helix credentials.
        self.assertIn("unknownguy", warnings)
        self.assertIn("TWITCH_CLIENT_ID", warnings)
        self.assertFalse(Item.objects.filter(title="unknownguy").exists())

    def test_reimport_replaces_previous_import(self, _calendar):
        """Importing the same export twice doesn't count it twice."""
        self.run_import()
        entry = Twitch.objects.get(item=self.xqc)
        entry.progress += 5  # e.g. a manual edit stays
        entry.save()

        self.run_import()

        self.assertEqual(Twitch.objects.get(item=self.xqc).progress, 136 + 5)
        self.assertEqual(UsageEvent.objects.filter(item=self.xqc).count(), 3)

    def test_only_before_first_scrobble(self, _calendar):
        """Watching after the first client session is left to the clients."""
        Twitch.objects.create(
            user=self.user, item=self.xqc, progress=40, status="In progress"
        )
        UsageEvent.objects.create(
            user=self.user,
            item=self.xqc,
            media_type=MediaTypes.TWITCH.value,
            client="cavea",
            minutes=40,
            session_id="cavea-1",
            occurred_at=datetime.datetime(2024, 3, 2, 10, tzinfo=UTC),
        )

        counts, warnings = self.run_import()

        # Only 2024-03-01 lies before the day of the first session.
        self.assertEqual(counts, {"twitch": 1})
        self.assertEqual(Twitch.objects.get(item=self.xqc).progress, 40 + 120)
        self.assertFalse(Twitch.objects.filter(item=self.shroud).exists())
        self.assertIn("Skipped 96 minutes", warnings)

    def test_raw_layout_in_zip(self, _calendar):
        """The per-minute layout with channel ids works inside the zip archive."""
        archive = zipped("request/site_history/minute_watched.csv", RAW_CSV)

        counts, warnings = self.run_import(archive)

        self.assertEqual(counts, {"twitch": 1})
        self.assertIsNone(warnings)
        item = Item.objects.get(media_id="44445592")
        self.assertEqual((item.title, item.image), ("pokimane", settings.IMG_NONE))
        self.assertEqual(TwitchChannel.objects.get(item=item).login, "pokimane")
        entry = Twitch.objects.get(item=item)
        self.assertEqual(entry.progress, 4)
        self.assertEqual(
            entry.start_date,
            datetime.datetime(2024, 2, 1, 20, 1, 10, 456000, tzinfo=UTC),
        )
        events = {
            (e.category_name, e.platform): (e.minutes, e.occurred_at)
            for e in UsageEvent.objects.filter(item=item)
        }
        self.assertEqual(
            events[("Just Chatting", "web")],
            (2, datetime.datetime(2024, 2, 1, 20, 1, 10, 456000, tzinfo=UTC)),
        )
        self.assertEqual(events[("Fortnite", "web")][0], 1)
        self.assertEqual(events[("Fortnite", "ios")][0], 1)

    @override_settings(TWITCH_CLIENT_ID="id", TWITCH_CLIENT_SECRET="secret")  # noqa: S106
    @patch("app.providers.twitch.helix")
    def test_resolve_logins_with_helix(self, mock_helix, _calendar):
        """With Helix credentials unknown logins become new channels."""
        mock_helix.return_value = {
            "data": [
                {
                    "id": "999",
                    "login": "unknownguy",
                    "display_name": "UnknownGuy",
                    "profile_image_url": "https://example.com/u.png",
                }
            ]
        }

        counts, warnings = self.run_import()

        self.assertEqual(counts, {"twitch": 3})
        self.assertIsNone(warnings)
        item = Item.objects.get(media_id="999")
        self.assertEqual(
            (item.title, item.image), ("UnknownGuy", "https://example.com/u.png")
        )
        self.assertEqual(Twitch.objects.get(item=item).progress, 20)
        lookups = [call.args[1] for call in mock_helix.call_args_list]
        self.assertTrue(any(("login", "unknownguy") in params for params in lookups))

    def test_invalid_files(self, _calendar):
        """Files without the watch history give a clear error."""
        with self.assertRaises(MediaImportError):
            self.run_import(io.BytesIO(b"foo,bar\n1,2\n"))
        with self.assertRaises(MediaImportError):
            self.run_import(zipped("request/other.csv", "a\n1\n"))
        with self.assertRaises(MediaImportError):
            self.run_import(io.BytesIO(b"\xff\xfe\x00bad"))

    def test_import_view(self, _calendar):
        """The import page queues the import (eager in tests)."""
        self.client.force_login(self.user)
        upload = SimpleUploadedFile(
            "minute_watched.csv",
            (mock_path / "import_twitch_minute_watched.csv").read_bytes(),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("import_twitch"), {"twitch_export": upload, "mode": "new"}
        )

        self.assertRedirects(response, reverse("import_data"))
        self.assertEqual(Twitch.objects.get(item=self.xqc).progress, 136)

    def test_import_view_requires_file(self, _calendar):
        """Posting without a file shows an error."""
        self.client.force_login(self.user)
        response = self.client.post(reverse("import_twitch"), {"mode": "new"})
        self.assertRedirects(response, reverse("import_data"))
        self.assertFalse(Twitch.objects.exists())
