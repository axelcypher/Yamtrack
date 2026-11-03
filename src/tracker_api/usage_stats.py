"""Usage statistics: activity per week, watch/play time, platforms, genres.

Units per week come from what Yamtrack already stores: watched TV episodes
(``Episode.end_date``), progress increases in the anime/manga/game history
and completed movies. TV shows linked to an anime entry count as anime only,
so synced progress is not counted twice.
"""

import datetime
from collections import Counter, defaultdict

from django.db.models import Count, Sum
from django.utils import timezone

from app import config
from app.models import Anime, Episode, Game, Manga, MediaTypes, Movie, Status

from .hooks import linked_tv_ids
from .models import ItemFacts, UsageEvent

VIDEO_TYPES = (MediaTypes.TV.value, MediaTypes.ANIME.value, MediaTypes.MOVIE.value)
UNIT_TYPES = (*VIDEO_TYPES, MediaTypes.MANGA.value)
MAX_WEEKS = 260
TOP_GENRES = 10
# Bigger jumps in one history row are imports or catch-up corrections, not
# watching; they set a new baseline instead of counting as activity.
MAX_STEP = 50


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


def _history_deltas(model, user, start, end, max_step=MAX_STEP):
    """(moment, item_id, delta) for every progress increase in the history."""
    items = dict(model.objects.filter(user=user).values_list("id", "item_id"))
    if not items:
        return []
    rows = (
        model.history.model.objects.filter(id__in=items)
        .order_by("id", "history_date")
        .values_list("id", "history_date", "progress")
    )
    deltas, previous = [], {}
    for instance_id, moment, progress in rows:
        delta = (progress or 0) - previous.get(instance_id, 0)
        previous[instance_id] = progress or 0
        if 0 < delta <= max_step and _in_range(moment, start, end):
            deltas.append((moment, items[instance_id], delta))
    return deltas


def _activity(user, start, end):
    """Collect (moment, media type, item id, units, minutes) records."""
    records = []
    hidden = set(linked_tv_ids(user))
    episodes = Episode.objects.filter(related_season__user=user).exclude(
        related_season__related_tv_id__in=hidden
    )
    if start:
        episodes = episodes.filter(end_date__gte=start)
    if end:
        episodes = episodes.filter(end_date__lte=end)
    records.extend(
        (moment, MediaTypes.TV.value, item_id, 1, None)
        for moment, item_id in episodes.exclude(end_date=None).values_list(
            "end_date", "related_season__related_tv__item_id"
        )
    )
    for media_type, model in (
        (MediaTypes.ANIME.value, Anime),
        (MediaTypes.MANGA.value, Manga),
    ):
        records.extend(
            (moment, media_type, item_id, delta, None)
            for moment, item_id, delta in _history_deltas(model, user, start, end)
        )
    records.extend(
        (moment, MediaTypes.GAME.value, item_id, 0, delta)
        for moment, item_id, delta in _history_deltas(
            Game, user, start, end, max_step=24 * 60
        )
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

    usage = UsageEvent.objects.filter(user=user)
    if start:
        usage = usage.filter(occurred_at__gte=start)
    if end:
        usage = usage.filter(occurred_at__lte=end)
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
            "hours": datasets(hours, (*VIDEO_TYPES, MediaTypes.GAME.value)),
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
        },
        "totals": [
            {
                "label": _label(media_type),
                "units": totals[media_type],
                "hours": round(minutes_total[media_type] / 60),
                "color": _color(media_type),
            }
            for media_type in (*UNIT_TYPES, MediaTypes.GAME.value)
            if totals[media_type] or minutes_total[media_type]
        ],
        "facts_known": len(item_ids & set(facts)),
        "facts_total": len(item_ids),
    }
