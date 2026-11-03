from django import template

from tracker_api.links import links_for_media

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
