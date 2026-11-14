"""Import Twitch watch time from the Twitch data export (yamtrack-plus).

The export ("Request your data" on twitch.tv) is a zip archive. The watch
history lives in ``request/site_history/minute_watched.csv``. Two layouts
are known and both are read:

- raw: one row per watched minute with ``time`` / ``time_utc``, ``channel``,
  ``channel_id``, ``minutes_logged``, ``game``, ``platform`` and many more;
- aggregated: one row per day, channel and game with ``day``,
  ``channel_name``, ``minutes_watched_unadjusted``, ``platform``, ``player``,
  ``game_name`` and ``content_mode``.

Columns are matched case-insensitively against several known names. The
minutes are summed per channel, day, category and platform; each sum becomes a
usage event (client ``twitch-export``) and is added to the channel's entry.

To avoid counting minutes twice, only rows before the user's first scrobbled
Twitch session are imported; from then on the clients report the watch time.
Days that only have a date are imported when they lie before that day.
Importing again replaces what the previous Twitch import added.

Rows only name the channel login in the aggregated layout. Logins are mapped
to channel ids through the channels already known, then through the Helix API
when ``TWITCH_CLIENT_ID`` / ``TWITCH_CLIENT_SECRET`` are set.
"""

import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from datetime import UTC, datetime, time

from django.conf import settings
from django.db import transaction
from django.db.models import Min, Sum
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.translation import gettext as _

from app.models import Item, MediaTypes, Sources, Status, Twitch
from app.providers import services
from app.providers import twitch as provider
from integrations.imports.helpers import MediaImportError
from tracker_api.models import TwitchChannel, UsageEvent
from tracker_api.twitch import IMPORT_CLIENT, image_url

logger = logging.getLogger(__name__)

EXPORT_FILES = ("minute_watched.csv", "minutes_watched.csv", "_minutes_watched.csv")
COLUMNS = {
    "login": ("channel_name", "channel", "channel_login", "broadcaster_login"),
    "channel_id": ("channel_id", "broadcaster_id"),
    "minutes": (
        "minutes_watched_unadjusted",
        "minutes_watched",
        "minutes_logged",
        "minutes",
    ),
    "time": ("time_utc", "time", "timestamp", "day", "date"),
    "category": ("game_name", "game", "category_name", "category"),
    "category_id": ("game_id", "category_id"),
    "platform": ("platform",),
}
MAX_MINUTES_PER_ROW = 24 * 60
DATE_ONLY = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
# Day-only rows are placed at noon UTC, inside that day in most time zones.
DAY_TIME = time(12, 0, tzinfo=UTC)


def importer(file, user, mode):
    """Import Twitch watch time from a data export zip or its CSV file."""
    return TwitchExportImporter(file, user, mode).import_data()


class TwitchExportImporter:
    """Read ``minute_watched.csv`` and turn it into Twitch entries."""

    def __init__(self, file, user, mode):
        """Initialize the importer.

        Args:
            file: Uploaded zip archive or CSV file
            user: Django user object to import data for
            mode (str): Import mode; the Twitch import always replaces its
                previous result, so "new" and "overwrite" behave the same
        """
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []
        self.cutoff = (
            UsageEvent.objects.filter(user=user, media_type=MediaTypes.TWITCH.value)
            .exclude(session_id="")
            .aggregate(first=Min("occurred_at"))["first"]
        )
        # (login, channel id, day, category id, category, platform) -> minutes
        self.buckets = defaultdict(float)
        self.moments = {}  # bucket -> latest timestamp, None for day-only rows
        self.skipped_minutes = 0.0

    def import_data(self):
        """Import the export and return (imported counts, warnings)."""
        with self._open_csv() as handle:
            reader = csv.DictReader(handle)
            columns = self._columns(reader.fieldnames or [])
            for row in reader:
                self._add_row(row, columns)

        if self.skipped_minutes:
            self.warnings.append(
                _(
                    "Skipped %(minutes)s minutes watched after your first Twitch "
                    "session tracked by a client (%(date)s), so they are not "
                    "counted twice."
                )
                % {
                    "minutes": round(self.skipped_minutes),
                    "date": self.cutoff.date().isoformat(),
                }
            )

        channels = self._resolve_channels()
        with transaction.atomic():
            self._remove_previous_import()
            imported = self._save(channels)

        warnings = "\n".join(dict.fromkeys(self.warnings))
        return {MediaTypes.TWITCH.value: imported}, warnings or None

    def _open_csv(self):
        """Return a text stream of the watch history CSV."""
        if zipfile.is_zipfile(self.file):
            self.file.seek(0)
            archive = zipfile.ZipFile(self.file)
            names = [
                name
                for name in archive.namelist()
                if name.rsplit("/", 1)[-1].lower() in EXPORT_FILES
            ]
            if not names:
                msg = _(
                    "The archive has no site_history/minute_watched.csv. "
                    "Upload the zip file of your Twitch data export."
                )
                raise MediaImportError(msg)
            # Prefer the file in site_history if there are several.
            names.sort(key=lambda name: "site_history" not in name.lower())
            stream = archive.open(names[0])
            return io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")

        self.file.seek(0)
        try:
            text = self.file.read().decode("utf-8-sig")
        except UnicodeDecodeError as error:
            msg = _("Invalid file format. Upload the zip or CSV file of the export.")
            raise MediaImportError(msg) from error
        return io.StringIO(text, newline="")

    def _columns(self, fieldnames):
        """Map the known columns to the file's header names."""
        normalized = {name.strip().lower(): name for name in fieldnames if name}
        columns = {
            key: next((normalized[c] for c in candidates if c in normalized), None)
            for key, candidates in COLUMNS.items()
        }
        if not (columns["login"] or columns["channel_id"]) or not columns["time"]:
            msg = _(
                "This doesn't look like minute_watched.csv of the Twitch data "
                "export: a channel column (channel_name or channel) and a time "
                "column (day, time or time_utc) are required. Found: %(columns)s"
            ) % {"columns": ", ".join(fieldnames) or "-"}
            raise MediaImportError(msg)
        return columns

    def _value(self, row, columns, key):
        column = columns[key]
        return str(row.get(column) or "").strip() if column else ""

    def _moment(self, value):
        """Return (datetime or None, date) of a time column value."""
        value = value.removesuffix(" UTC").strip()
        if DATE_ONLY.fullmatch(value):
            return None, parse_date(value)
        moment = parse_datetime(value)
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            moment = moment.astimezone(UTC)
            return moment, moment.date()
        return None, parse_date(value[:10])

    def _add_row(self, row, columns):
        login = self._value(row, columns, "login").lower()
        channel_id = self._value(row, columns, "channel_id")
        if not channel_id.isdigit():
            channel_id = ""
        if not login and not channel_id:
            return
        try:
            moment, day = self._moment(self._value(row, columns, "time"))
        except ValueError:
            moment, day = None, None
        if day is None:
            return
        raw_minutes = self._value(row, columns, "minutes")
        try:
            minutes = float(raw_minutes) if raw_minutes else 1.0
        except ValueError:
            return
        if not 0 < minutes <= MAX_MINUTES_PER_ROW:
            return

        if self.cutoff is not None:
            before = moment < self.cutoff if moment else day < self.cutoff.date()
            if not before:
                self.skipped_minutes += minutes
                return

        key = (
            login,
            channel_id,
            day,
            self._value(row, columns, "category_id")[:32],
            self._value(row, columns, "category")[:255],
            self._value(row, columns, "platform")[:64],
        )
        self.buckets[key] += minutes
        if moment and (self.moments.get(key) is None or moment > self.moments[key]):
            self.moments[key] = moment
        else:
            self.moments.setdefault(key, None)

    def _resolve_channels(self):
        """Return {(login, channel id): (channel id, Helix user or {})}."""
        pairs = {(key[0], key[1]) for key in self.buckets}
        logins = {login for login, _channel_id in pairs if login}
        known = self._known_channels(logins)
        helix_users = self._helix_users(
            logins, {channel_id for _login, channel_id in pairs if channel_id}
        )
        for login, user in helix_users.items():
            known.setdefault(login, user["id"])

        missing = {
            login
            for login, channel_id in pairs
            if not channel_id and login not in known
        }
        if missing:
            message = _("Couldn't find the Twitch channel id of: %(logins)s.") % {
                "logins": ", ".join(sorted(missing))
            }
            if not provider.is_configured():
                message += " " + _(
                    "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET on the server to "
                    "look up channels that aren't tracked yet."
                )
            self.warnings.append(message)

        resolved = {}
        for login, channel_id in pairs:
            found = channel_id or known.get(login)
            if found:
                resolved[login, channel_id] = (found, helix_users.get(login, {}))
        return resolved

    def _known_channels(self, logins):
        """Map logins to the ids of channels that are already tracked."""
        known = dict(
            TwitchChannel.objects.filter(login__in=logins).values_list(
                "login", "item__media_id"
            )
        )
        # Items without a stored login (e.g. from a Yamtrack CSV) by their title
        rest = logins - set(known)
        if rest:
            for media_id, title in Item.objects.filter(
                media_type=MediaTypes.TWITCH.value, source=Sources.TWITCH.value
            ).values_list("media_id", "title"):
                if title.lower() in rest:
                    known.setdefault(title.lower(), media_id)
        return known

    def _helix_users(self, logins, ids):
        """Helix users by login for the channels of the export, if configured."""
        if not provider.is_configured():
            return {}
        try:
            found = provider.users(ids=sorted(ids), logins=sorted(logins))
        except services.ProviderAPIError as error:
            self.warnings.append(str(error))
            return {}
        return {user["login"].lower(): user for user in found}

    def _remove_previous_import(self):
        """Take back what an earlier Twitch import added."""
        previous = UsageEvent.objects.filter(
            user=self.user, media_type=MediaTypes.TWITCH.value, client=IMPORT_CLIENT
        )
        totals = dict(
            previous.values("item_id")
            .annotate(minutes=Sum("minutes"))
            .values_list("item_id", "minutes")
        )
        for entry in Twitch.objects.filter(user=self.user, item_id__in=totals):
            entry.progress = max(0, entry.progress - totals[entry.item_id])
            entry.save()
        previous.delete()

    def _channel_item(self, channel_id, login, helix_user):
        item = Item.objects.filter(
            media_id=channel_id,
            source=Sources.TWITCH.value,
            media_type=MediaTypes.TWITCH.value,
        ).first()
        if item is None:
            item = Item.objects.create(
                media_id=channel_id,
                source=Sources.TWITCH.value,
                media_type=MediaTypes.TWITCH.value,
                title=helix_user.get("display_name") or login or channel_id,
                image=image_url(helix_user.get("profile_image_url"))
                or settings.IMG_NONE,
            )
        login = (helix_user.get("login") or login).lower()
        if login:
            TwitchChannel.objects.update_or_create(item=item, defaults={"login": login})
        return item

    def _save(self, channels):
        """Create the usage events and add the minutes to the entries."""
        per_channel = defaultdict(list)  # channel id -> [(bucket, minutes)]
        logins = {}
        for key, minutes in self.buckets.items():
            resolved = channels.get((key[0], key[1]))
            if resolved is None:
                continue
            channel_id, helix_user = resolved
            per_channel[channel_id].append((key, minutes))
            if key[0] or helix_user:
                logins[channel_id] = (key[0], helix_user)

        events = []
        for channel_id, buckets in per_channel.items():
            login, helix_user = logins.get(channel_id, ("", {}))
            item = self._channel_item(channel_id, login, helix_user)
            slices = []
            for key, minutes in buckets:
                rounded = round(minutes)
                if rounded <= 0:
                    continue
                moment = self.moments.get(key) or datetime.combine(key[2], DAY_TIME)
                slices.append((moment, key, rounded))
            if not slices:
                continue
            events.extend(
                UsageEvent(
                    user=self.user,
                    item=item,
                    media_type=MediaTypes.TWITCH.value,
                    client=IMPORT_CLIENT,
                    platform=key[5],
                    units=0,
                    minutes=rounded,
                    occurred_at=moment,
                    category_id=key[3],
                    category_name=key[4],
                )
                for moment, key, rounded in slices
            )
            self._add_to_entry(item, slices)

        UsageEvent.objects.bulk_create(events, batch_size=1000)
        logger.info(
            "Imported %d Twitch usage events for %s", len(events), self.user.username
        )
        return len({event.item_id for event in events})

    def _add_to_entry(self, item, slices):
        total = sum(minutes for _moment, _key, minutes in slices)
        first = min(moment for moment, _key, _minutes in slices)
        last = max(moment for moment, _key, _minutes in slices)
        entry = (
            Twitch.objects.filter(item=item, user=self.user)
            .order_by("-created_at")
            .first()
        )
        if entry:
            entry.progress += total
            if entry.start_date is None or first < entry.start_date:
                entry.start_date = first
        else:
            entry = Twitch(
                item=item,
                user=self.user,
                progress=total,
                status=Status.IN_PROGRESS.value,
                start_date=first,
            )
            entry._history_date = last
        entry.save()
