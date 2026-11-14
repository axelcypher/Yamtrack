from django import template
from django.db.models import Max, Sum

from app.helpers import minutes_to_hhmm
from app.models import MediaTypes
from tracker_api.links import links_for_media
from tracker_api.models import UsageEvent

register = template.Library()


@register.inclusion_tag("tracker_api/linked_entry.html", takes_context=True)
def linked_entry(context, media):
    """Show the anime or TV entry linked to the media on a detail page."""
    request = context.get("request")
    if not request or not request.user.is_authenticated or not isinstance(media, dict):
        return {"entries": []}
    links = links_for_media(
        request.user,
        media.get("media_type"),
        media.get("source"),
        media.get("media_id"),
    )
    if media.get("source") == "mal":
        entries = [{"item": link.tv.item, "kind": "tv"} for link in links]
    else:
        entries = [{"item": link.anime_item, "kind": "anime"} for link in links]
    return {"entries": entries}


@register.inclusion_tag("tracker_api/twitch_watchtime.html", takes_context=True)
def twitch_watchtime(context, media):
    """Show the user's watch time per stream category on a Twitch channel page."""
    request = context.get("request")
    if (
        not request
        or not request.user.is_authenticated
        or not isinstance(media, dict)
        or media.get("media_type") != MediaTypes.TWITCH.value
    ):
        return {"categories": []}
    events = UsageEvent.objects.filter(
        user=request.user,
        media_type=MediaTypes.TWITCH.value,
        item__source=media.get("source"),
        item__media_id=media.get("media_id"),
    )
    rows = (
        events.values("category_name")
        .annotate(minutes=Sum("minutes"))
        .order_by("-minutes")
    )
    return {
        "categories": [
            {"name": row["category_name"], "time": minutes_to_hhmm(row["minutes"])}
            for row in rows
            if row["minutes"]
        ],
        "last_watched": events.aggregate(last=Max("occurred_at"))["last"],
        "user": request.user,
    }
