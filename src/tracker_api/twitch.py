"""Twitch channel items for scrobbles and the data export import.

A channel is identified by its Twitch user id (``Item.media_id``). Title and
image come from the clients' ``channel`` payload, so scrobbling needs no
Twitch credentials; the login is kept in ``TwitchChannel``.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

from app.models import Item, MediaTypes, Sources
from app.providers import services
from app.providers import twitch as provider

from .auth import ApiError
from .models import TwitchChannel

IMPORT_CLIENT = "twitch-export"


def _text(value, max_length):
    return str(value or "").strip()[:max_length]


def image_url(value):
    """Return an image URL that fits the item, or an empty string."""
    url = str(value or "").strip()
    max_length = Item._meta.get_field("image").max_length
    valid = len(url) <= max_length and urlparse(url).scheme in ("http", "https")
    return url if valid else ""


def parse_channel(payload):
    """Return login, display name and image from a client ``channel`` payload."""
    if not isinstance(payload, dict):
        return None
    channel = {
        "login": _text(payload.get("login"), 64).lower(),
        "display_name": _text(payload.get("display_name"), 255),
        "image": image_url(payload.get("image")),
    }
    return channel if channel["login"] or channel["display_name"] else None


def parse_category(payload):
    """Return (id, name) of a client ``category`` payload."""
    if not isinstance(payload, dict):
        return "", ""
    return _text(payload.get("id"), 32), _text(payload.get("name"), 255)


def validate_target(source, media_id):
    """Twitch items are ``source=twitch`` with the numeric channel id."""
    if source != Sources.TWITCH.value:
        raise ApiError(400, "invalid_target", "Twitch channels use source=twitch")
    if not media_id.isdigit() or len(media_id) > 36:  # noqa: PLR2004 - Item.media_id
        raise ApiError(400, "invalid_target", "media_id must be the Twitch user id")


def update_channel(item, channel):
    """Store changed channel data on the item; returns whether anything changed."""
    changed = []
    title = channel["display_name"] or channel["login"]
    if title and item.title != title:
        item.title = title
        changed.append("title")
    if channel["image"] and item.image != channel["image"]:
        item.image = channel["image"]
        changed.append("image")
    if changed:
        item.save(update_fields=changed)
    if channel["login"]:
        TwitchChannel.objects.update_or_create(
            item=item, defaults={"login": channel["login"]}
        )
    cache.delete(provider.metadata_cache_key(item.media_id))
    return bool(changed)


def channel_item(media_id, payload):
    """Return the item of a channel, creating or updating it from the payload."""
    channel = parse_channel(payload)
    item = Item.objects.filter(
        media_id=media_id,
        source=Sources.TWITCH.value,
        media_type=MediaTypes.TWITCH.value,
    ).first()
    if item is None:
        if channel is None:
            # Without channel data only Helix (if configured) knows the channel.
            try:
                metadata = services.get_media_metadata(
                    MediaTypes.TWITCH.value, media_id, Sources.TWITCH.value
                )
            except services.ProviderAPIError as error:
                raise ApiError(
                    400,
                    "missing_channel",
                    "channel (login, display_name, image) is required "
                    "the first time a channel is scrobbled",
                ) from error
            channel = {
                "login": metadata["details"].get("login", ""),
                "display_name": metadata["title"],
                "image": metadata["image"],
            }
        item, _created = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TWITCH.value,
            media_type=MediaTypes.TWITCH.value,
            defaults={
                "title": channel["display_name"] or channel["login"],
                "image": channel["image"] or settings.IMG_NONE,
            },
        )
    if channel:
        update_channel(item, channel)
    return item
