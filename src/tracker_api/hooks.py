"""Small entry points called from upstream Yamtrack code.

Each upstream file gets a single, marked call into this module, so merges
from upstream stay easy and the fork's behaviour lives in one place.
"""

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from app.models import MediaTypes

from .models import AnimeTvLink


def linked_tv_ids(user):
    """Primary keys of the user's TV shows that are listed as anime."""
    return AnimeTvLink.objects.filter(user=user).values_list("tv_id", flat=True)


def exclude_linked_tv(queryset, user, media_type):
    """Hide linked TV shows (and their seasons) from TV lists and the home page."""
    if media_type == MediaTypes.TV.value:
        return queryset.exclude(id__in=linked_tv_ids(user))
    if media_type == MediaTypes.SEASON.value:
        return queryset.exclude(related_tv_id__in=linked_tv_ids(user))
    return queryset


def duplicate_tv_event_ids(user, events):
    """Ids of TV release events that the linked anime already shows that day.

    A TV event is only dropped when the linked anime has its own event on the
    same local day; anime without a calendar keep their TV releases.
    """
    from events.models import Event  # noqa: PLC0415 - events imports app models

    anime_by_show = defaultdict(set)
    for tmdb_id, anime_item_id in AnimeTvLink.objects.filter(user=user).values_list(
        "tv__item__media_id", "anime_item_id"
    ):
        anime_by_show[tmdb_id].add(anime_item_id)
    if not anime_by_show:
        return []

    candidates = [
        event
        for event in events
        if event.item.media_type == MediaTypes.SEASON.value
        and event.item.media_id in anime_by_show
    ]
    if not candidates:
        return []

    first = min(event.datetime for event in candidates) - timedelta(days=1)
    last = max(event.datetime for event in candidates) + timedelta(days=1)
    anime_days = defaultdict(set)
    for item_id, moment in Event.objects.filter(
        item_id__in=set().union(*anime_by_show.values()),
        datetime__range=(first, last),
    ).values_list("item_id", "datetime"):
        anime_days[item_id].add(timezone.localdate(moment))

    return [
        event.id
        for event in candidates
        if any(
            timezone.localdate(event.datetime) in anime_days[item_id]
            for item_id in anime_by_show[event.item.media_id]
        )
    ]
