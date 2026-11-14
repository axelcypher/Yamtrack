"""Usage statistics: activity per week, watch/play time, platforms, genres.

Units per week come from what Yamtrack already stores: watched TV episodes
(``Episode.end_date``), progress increases in the anime/manga/game history
and completed movies. Episodes of TV shows linked to an anime entry count as
anime, and writes mirrored by the anime/TV sync are skipped, so nothing is
counted twice. A newly created entry counts as one unit, however much
progress it starts with, and bulk marks of many episodes at once do not
count, so imports do not show up as a binge.

Twitch watch time comes from the usage events instead of the progress
history: scrobbles and the data export import both write one event per slice
with its real time and stream category, while the progress of a channel only
holds the total. Manual progress edits of a channel therefore don't count.
"""

import datetime
from collections import Counter, defaultdict

from django.db.models import Count, Sum
from django.utils import timezone

from app import config
from app.models import Anime, Episode, Game, Manga, MediaTypes, Movie, Status

from .hooks import linked_tv_ids
from .models import ItemFacts, UsageEvent
from .sync import SYNC_REASON

VIDEO_TYPES = (MediaTypes.TV.value, MediaTypes.ANIME.value, MediaTypes.MOVIE.value)
UNIT_TYPES = (*VIDEO_TYPES, MediaTypes.MANGA.value)
# Types counted in minutes only, not units.
TIME_TYPES = (MediaTypes.GAME.value, MediaTypes.TWITCH.value)
MAX_WEEKS = 260
TOP_GENRES = 10
TOP_CATEGORIES = 10
# Bigger jumps in one history row are imports or catch-up corrections, not
# watching; they set a new baseline instead of counting as activity.
MAX_STEP = 50
MAX_GAME_STEP = 24 * 60  # minutes


def _week(moment):
    day = (
        timezone.localdate(moment) if isinstance(moment, datetime.datetime) else moment
    )
    return day - datetime.timedelta(days=day.weekday())


def _in_range(moment, start, end):
    return (
        moment is not None
        and (start is None or moment >= start)
        and (end is None or moment <= end)
    )


def history_increases(model, user, start, end, *, max_step=MAX_STEP, units=True):
    """(moment, item_id, first, last) for every progress increase in the history.

    ``first`` to ``last`` (inclusive) are the units the row added, e.g. the
    episode numbers.
    """
    items = dict(model.objects.filter(user=user).values_list("id", "item_id"))
    if not items:
        return []
    rows = (
        model.history.model.objects.filter(id__in=items)
        .order_by("id", "history_date")
        .values_list(
            "id", "history_date", "progress", "history_type", "history_change_reason"
        )
    )
    increases, previous = [], {}
    for instance_id, moment, progress, history_type, reason in rows:
        value = progress or 0
        delta = value - previous.get(instance_id, 0)
        previous[instance_id] = value
        if units and history_type == "+":
            delta = min(delta, 1)
        if (
            0 < delta <= max_step
            and reason != SYNC_REASON
            and _in_range(moment, start, end)
        ):
            increases.append((moment, items[instance_id], value - delta + 1, value))
    return increases


def history_deltas(model, user, start, end, **options):
    """(moment, item_id, delta) for every progress increase in the history."""
    return [
        (moment, item_id, last - first + 1)
        for moment, item_id, first, last in history_increases(
            model, user, start, end, **options
        )
    ]


def usage_events(user, start, end, media_type=None):
    """Return the usage events in the range, optionally of one media type."""
    events = UsageEvent.objects.filter(user=user)
    if media_type:
        events = events.filter(media_type=media_type)
    if start:
        events = events.filter(occurred_at__gte=start)
    if end:
        events = events.filter(occurred_at__lte=end)
    return events


def twitch_minutes(user, start, end):
    """(moment, item_id, minutes) for every Twitch usage event in the range."""
    return list(
        usage_events(user, start, end, MediaTypes.TWITCH.value)
        .filter(minutes__gt=0)
        .values_list("occurred_at", "item_id", "minutes")
    )


def twitch_categories(user, start, end, limit=TOP_CATEGORIES):
    """Twitch watch time per stream category, the largest first."""
    rows = (
        usage_events(user, start, end, MediaTypes.TWITCH.value)
        .exclude(category_name="")
        .values("category_name")
        .annotate(minutes=Sum("minutes"))
        .order_by("-minutes", "category_name")[:limit]
    )
    return [{"label": row["category_name"], "minutes": row["minutes"]} for row in rows]


def watched_episodes(user, start, end, *fields):
    """``fields`` of each episode watched in the range.

    Leaves out episodes the anime/TV sync mirrored, and bulk marks: more than
    ``MAX_STEP`` episodes of one show with the same end date are an import or
    a "mark all as watched", not watching.
    """
    mirrored = Episode.history.model.objects.filter(
        history_type="+", history_change_reason=SYNC_REASON
    ).values_list("id", flat=True)
    episodes = (
        Episode.objects.filter(related_season__user=user)
        .exclude(id__in=mirrored)
        .exclude(end_date=None)
    )
    if start:
        episodes = episodes.filter(end_date__gte=start)
    if end:
        episodes = episodes.filter(end_date__lte=end)
    rows = list(
        episodes.values_list(*fields, "related_season__related_tv_id", "end_date")
    )
    sizes = Counter(row[-2:] for row in rows)
    return [row[:-2] for row in rows if sizes[row[-2:]] <= MAX_STEP]


def _activity(user, start, end):
    """Collect (moment, media type, item id, units, minutes) records."""
    records = []
    linked = set(linked_tv_ids(user))
    records.extend(
        (
            moment,
            MediaTypes.ANIME.value if tv_id in linked else MediaTypes.TV.value,
            item_id,
            1,
            None,
        )
        for moment, tv_id, item_id in watched_episodes(
            user,
            start,
            end,
            "end_date",
            "related_season__related_tv_id",
            "related_season__related_tv__item_id",
        )
    )
    for media_type, model in (
        (MediaTypes.ANIME.value, Anime),
        (MediaTypes.MANGA.value, Manga),
    ):
        records.extend(
            (moment, media_type, item_id, delta, None)
            for moment, item_id, delta in history_deltas(model, user, start, end)
        )
    records.extend(
        (moment, MediaTypes.GAME.value, item_id, 0, delta)
        for moment, item_id, delta in history_deltas(
            Game, user, start, end, max_step=MAX_GAME_STEP, units=False
        )
    )
    records.extend(
        (moment, MediaTypes.TWITCH.value, item_id, 0, minutes)
        for moment, item_id, minutes in twitch_minutes(user, start, end)
    )
    movies = Movie.objects.filter(user=user, status=Status.COMPLETED.value)
    records.extend(
        (moment, MediaTypes.MOVIE.value, item_id, 1, None)
        for moment, item_id in movies.values_list("end_date", "item_id")
        if _in_range(moment, start, end)
    )
    return records


def _weeks(records, start, end):
    first = _week(start) if start else min((_week(r[0]) for r in records), default=None)
    last = _week(end) if end else _week(timezone.now())
    if first is None:
        return []
    weeks, current = [], max(first, last - datetime.timedelta(weeks=MAX_WEEKS))
    while current <= last:
        weeks.append(current)
        current += datetime.timedelta(weeks=1)
    return weeks


def _color(media_type):
    return config.get_property(media_type, "stats_color")


def _label(media_type):
    return str(MediaTypes(media_type).label)


def build(user, start, end):
    """Return the statistics page context for a date range."""
    records = _activity(user, start, end)
    facts = {
        fact.item_id: fact
        for fact in ItemFacts.objects.filter(item_id__in={r[2] for r in records})
    }
    weeks = _weeks(records, start, end)
    index = {week: position for position, week in enumerate(weeks)}

    units = defaultdict(lambda: [0] * len(weeks))
    hours = defaultdict(lambda: [0.0] * len(weeks))
    totals = Counter()
    minutes_total = Counter()
    genre_units = Counter()
    for moment, media_type, item_id, count, tracked_minutes in records:
        position = index.get(_week(moment))
        fact = facts.get(item_id)
        minutes = tracked_minutes
        if minutes is None and fact and fact.runtime_minutes:
            minutes = count * fact.runtime_minutes
        if count:
            totals[media_type] += count
            if fact:
                genre_units.update(dict.fromkeys(fact.genres, count))
        if minutes:
            minutes_total[media_type] += minutes
        if position is None:
            continue
        units[media_type][position] += count
        hours[media_type][position] += (minutes or 0) / 60

    def datasets(series, types):
        return [
            {
                "label": _label(media_type),
                "data": [round(value, 1) for value in series[media_type]],
                "backgroundColor": _color(media_type),
            }
            for media_type in types
            if any(series[media_type])
        ]

    usage = usage_events(user, start, end)
    platforms = [
        {
            "label": row["platform"] or row["client"] or "?",
            "units": row["units"],
            "minutes": row["minutes"],
        }
        for row in usage.values("platform", "client")
        .annotate(units=Sum("units"), minutes=Sum("minutes"), events=Count("id"))
        .order_by("-units", "-minutes")
    ]

    item_ids = {r[2] for r in records}
    return {
        "chart": {
            "weeks": [week.isoformat() for week in weeks],
            "units": datasets(units, UNIT_TYPES),
            "hours": datasets(hours, (*VIDEO_TYPES, *TIME_TYPES)),
            "split": {
                "labels": [_label(t) for t in UNIT_TYPES if totals[t]],
                "data": [totals[t] for t in UNIT_TYPES if totals[t]],
                "colors": [_color(t) for t in UNIT_TYPES if totals[t]],
            },
            "platforms": platforms,
            "genres": [
                {"label": genre, "units": count}
                for genre, count in genre_units.most_common(TOP_GENRES)
            ],
            "categories": [
                {"label": row["label"], "hours": round(row["minutes"] / 60, 1)}
                for row in twitch_categories(user, start, end)
            ],
        },
        "totals": [
            {
                "label": _label(media_type),
                "units": totals[media_type],
                "hours": round(minutes_total[media_type] / 60),
                "color": _color(media_type),
            }
            for media_type in (*UNIT_TYPES, *TIME_TYPES)
            if totals[media_type] or minutes_total[media_type]
        ],
        "facts_known": len(item_ids & set(facts)),
        "facts_total": len(item_ids),
    }
