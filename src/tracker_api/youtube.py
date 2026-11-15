"""YouTube channel items for scrobbles (yamtrack-plus).

A channel is identified by its channel id (``UC`` and 22 more characters,
``Item.media_id``). Name and avatar come from the extension's ``channel``
payload, so scrobbling needs no YouTube Data API and no credentials. The
handle is not stored: the channel id link always works.
"""

import re

from django.conf import settings

from app.models import Item, MediaTypes, Sources

from .auth import ApiError
from .twitch import image_url

CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
VIDEO_ID = re.compile(r"[0-9A-Za-z_-]{11}")
MAX_TITLE = 255


def _text(value, max_length):
    return str(value or "").strip()[:max_length]


def validate_target(source, media_id):
    """YouTube items are ``source=youtube`` with the ``UC…`` channel id."""
    if source != Sources.YOUTUBE.value:
        raise ApiError(400, "invalid_target", "YouTube channels use source=youtube")
    if not CHANNEL_ID.fullmatch(media_id):
        raise ApiError(
            400, "invalid_target", "media_id must be the YouTube channel id (UC…)"
        )


def parse_channel(payload):
    """Return name and image from a client ``channel`` payload."""
    if not isinstance(payload, dict):
        return None
    channel = {
        "name": _text(payload.get("name"), MAX_TITLE),
        "image": image_url(payload.get("image")),
    }
    return channel if channel["name"] or channel["image"] else None


def parse_video(payload):
    """Return (id, title) of a client ``video`` payload; empty when invalid."""
    if not isinstance(payload, dict):
        return "", ""
    video_id = str(payload.get("id") or "").strip()
    if not VIDEO_ID.fullmatch(video_id):
        return "", ""
    return video_id, _text(payload.get("title"), MAX_TITLE)


def update_channel(item, channel):
    """Store a changed channel name or avatar on the item."""
    changed = []
    if channel["name"] and item.title != channel["name"]:
        item.title = channel["name"]
        changed.append("title")
    if channel["image"] and item.image != channel["image"]:
        item.image = channel["image"]
        changed.append("image")
    if changed:
        item.save(update_fields=changed)
    return bool(changed)


def channel_item(media_id, payload):
    """Return the item of a channel, creating or updating it from the payload."""
    channel = parse_channel(payload)
    item = Item.objects.filter(
        media_id=media_id,
        source=Sources.YOUTUBE.value,
        media_type=MediaTypes.YOUTUBE.value,
    ).first()
    if item is None:
        if channel is None or not channel["name"]:
            raise ApiError(
                400,
                "missing_channel",
                "channel.name is required the first time a channel is scrobbled",
            )
        item, _created = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.YOUTUBE.value,
            media_type=MediaTypes.YOUTUBE.value,
            defaults={
                "title": channel["name"],
                "image": channel["image"] or settings.IMG_NONE,
            },
        )
    if channel:
        update_channel(item, channel)
    return item
