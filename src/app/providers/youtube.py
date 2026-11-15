"""YouTube channels (yamtrack-plus).

There is no YouTube Data API here: channel metadata (name and avatar) comes
only from the browser extension that scrobbles watch time and is stored on the
``Item``. Metadata and search therefore only know channels that were scrobbled
at least once.
"""

from django.conf import settings
from django.utils.translation import gettext as _

from app import helpers
from app.models import Item, MediaTypes, Sources
from app.providers import services

RESULTS_PER_PAGE = 20


def channel_url(media_id):
    """Return the youtube.com page of a channel; the id link always works."""
    return f"https://www.youtube.com/channel/{media_id}"


def video_url(video_id):
    """Return the youtube.com page of a video."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _items():
    return Item.objects.filter(
        media_type=MediaTypes.YOUTUBE.value,
        source=Sources.YOUTUBE.value,
    )


def channel(media_id):
    """Return the metadata of a YouTube channel that is known locally."""
    item = _items().filter(media_id=media_id).first()
    if item is None:
        services.raise_not_found_error(Sources.YOUTUBE.value, media_id, "channel")

    return {
        "media_id": str(media_id),
        "source": Sources.YOUTUBE.value,
        "source_url": channel_url(media_id),
        "media_type": MediaTypes.YOUTUBE.value,
        "title": item.title,
        "max_progress": None,
        "image": item.image or settings.IMG_NONE,
        "synopsis": _("No synopsis available."),
        "genres": [],
        "score": None,
        "score_count": None,
        # Also makes the detail page show the provider link to the channel.
        "details": {"channel_id": str(media_id)},
        "related": {},
    }


def search(query, page):
    """Search the YouTube channels already known locally."""
    items = _items().filter(title__icontains=query).order_by("title", "id")
    results = [
        {
            "media_id": item.media_id,
            "source": Sources.YOUTUBE.value,
            "media_type": MediaTypes.YOUTUBE.value,
            "title": item.title,
            "image": item.image or settings.IMG_NONE,
        }
        for item in items
    ]
    start = (page - 1) * RESULTS_PER_PAGE
    return helpers.format_search_response(
        page,
        RESULTS_PER_PAGE,
        len(results),
        results[start : start + RESULTS_PER_PAGE],
    )
