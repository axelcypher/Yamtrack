"""Twitch channels (yamtrack-plus).

Channel metadata normally comes from the clients that scrobble watch time
(login, display name, profile image) and is stored on the ``Item``. With the
optional ``TWITCH_CLIENT_ID`` / ``TWITCH_CLIENT_SECRET`` settings the Helix API
(app access token via client credentials) enriches the metadata, searches
channels and resolves channel logins to ids. Without them everything falls
back to the channels already known locally.

API documentation: https://dev.twitch.tv/docs/api/reference
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils.translation import gettext as _

from app import helpers
from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105
TOKEN_CACHE_KEY = f"{Sources.TWITCH.value}_access_token"
RESULTS_PER_PAGE = 20
HELIX_BATCH = 100  # Helix takes at most 100 ids or logins per users request


class TwitchNotConfiguredError(Exception):
    """Raised when a feature needs the Helix API but no credentials are set."""


def is_configured():
    """Whether the server has credentials for the Helix API."""
    return bool(settings.TWITCH_CLIENT_ID and settings.TWITCH_CLIENT_SECRET)


def channel_url(login):
    """Return the twitch.tv page of a channel."""
    return f"https://www.twitch.tv/{login}" if login else "https://www.twitch.tv"


def metadata_cache_key(media_id):
    """Cache key of the Helix metadata of a channel."""
    return f"{Sources.TWITCH.value}_{MediaTypes.TWITCH.value}_{media_id}"


def get_access_token():
    """Return an app access token for the Helix API."""
    access_token = cache.get(TOKEN_CACHE_KEY)
    if access_token is None:
        try:
            response = services.api_request(
                Sources.TWITCH.value,
                "POST",
                TOKEN_URL,
                params={
                    "client_id": settings.TWITCH_CLIENT_ID,
                    "client_secret": settings.TWITCH_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
            )
        except requests.exceptions.RequestException as error:
            raise services.ProviderAPIError(Sources.TWITCH.value, error) from None
        access_token = response["access_token"]
        # 1 min buffer to avoid using an expired token
        cache.set(TOKEN_CACHE_KEY, access_token, response["expires_in"] - 60)
    return access_token


def helix(path, params, *, retry=True):
    """GET a Helix endpoint and return its JSON."""
    if not is_configured():
        msg = "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set"
        raise TwitchNotConfiguredError(msg)
    headers = {
        "Client-Id": settings.TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {get_access_token()}",
    }
    try:
        return services.api_request(
            Sources.TWITCH.value,
            "GET",
            f"{base_url}/{path}",
            params=params,
            headers=headers,
        )
    except requests.exceptions.HTTPError as error:
        if error.response.status_code == requests.codes.unauthorized and retry:
            logger.warning("Twitch: invalid access token, refreshing")
            cache.delete(TOKEN_CACHE_KEY)
            return helix(path, params, retry=False)
        raise services.ProviderAPIError(Sources.TWITCH.value, error) from None
    except requests.exceptions.RequestException as error:
        raise services.ProviderAPIError(Sources.TWITCH.value, error) from None


def users(ids=(), logins=()):
    """Return Helix user objects for channel ids and/or logins."""
    keys = [("id", str(value)) for value in ids] + [
        ("login", str(value).lower()) for value in logins
    ]
    found = []
    for start in range(0, len(keys), HELIX_BATCH):
        response = helix("users", keys[start : start + HELIX_BATCH])
        found.extend(response.get("data", []))
    return found


def _local_channel(media_id):
    """Item and login of a channel that is already known locally."""
    from tracker_api.models import TwitchChannel  # noqa: PLC0415

    item = Item.objects.filter(
        media_type=MediaTypes.TWITCH.value,
        source=Sources.TWITCH.value,
        media_id=media_id,
    ).first()
    if item is None:
        return None, ""
    login = (
        TwitchChannel.objects.filter(item=item).values_list("login", flat=True).first()
    )
    return item, login or ""


def _helix_channel(media_id):
    """Helix user object of a channel, or None when unavailable."""
    if not is_configured() or not str(media_id).isdigit():
        return None
    data = cache.get(metadata_cache_key(media_id))
    if data is None:
        try:
            found = users(ids=[media_id])
        except services.ProviderAPIError:
            logger.warning("Twitch: couldn't fetch channel %s from Helix", media_id)
            return None
        data = found[0] if found else {}
        cache.set(metadata_cache_key(media_id), data)
    return data or None


def channel(media_id):
    """Return the metadata of a Twitch channel."""
    remote = _helix_channel(media_id)
    item, login = _local_channel(media_id)
    if remote is None and item is None:
        services.raise_not_found_error(Sources.TWITCH.value, media_id, "channel")

    details = {}
    if remote:
        login = remote.get("login") or login
        title = remote.get("display_name") or login
        image = remote.get("profile_image_url") or settings.IMG_NONE
        synopsis = remote.get("description") or ""
        if remote.get("broadcaster_type"):
            details["broadcaster_type"] = remote["broadcaster_type"].capitalize()
        if remote.get("created_at"):
            details["created_at"] = remote["created_at"][:10]
    else:
        title = item.title
        image = item.image or settings.IMG_NONE
        synopsis = ""
    if login:
        details = {"login": login, **details}

    return {
        "media_id": str(media_id),
        "source": Sources.TWITCH.value,
        "source_url": channel_url(login),
        "media_type": MediaTypes.TWITCH.value,
        "title": title,
        "max_progress": None,
        "image": image,
        "synopsis": synopsis or _("No synopsis available."),
        "genres": [],
        "score": None,
        "score_count": None,
        "details": details,
        "related": {},
    }


def _result(media_id, title, image):
    return {
        "media_id": str(media_id),
        "source": Sources.TWITCH.value,
        "media_type": MediaTypes.TWITCH.value,
        "title": title,
        "image": image or settings.IMG_NONE,
    }


def _local_search(query):
    from tracker_api.models import TwitchChannel  # noqa: PLC0415

    logins = TwitchChannel.objects.filter(login__icontains=query).values_list(
        "item_id", flat=True
    )
    items = Item.objects.filter(
        Q(title__icontains=query) | Q(id__in=logins),
        media_type=MediaTypes.TWITCH.value,
        source=Sources.TWITCH.value,
    ).order_by("title")
    return [_result(item.media_id, item.title, item.image) for item in items]


def search(query, page):
    """Search Twitch channels; without Helix credentials, known channels only."""
    if not is_configured():
        results = _local_search(query)
        start = (page - 1) * RESULTS_PER_PAGE
        return helpers.format_search_response(
            page,
            RESULTS_PER_PAGE,
            len(results),
            results[start : start + RESULTS_PER_PAGE],
        )

    cache_key = f"search_{Sources.TWITCH.value}_{query}_{page}"
    data = cache.get(cache_key)
    if data is None:
        # Helix paginates with cursors; fetch the pages up to the requested one.
        cursor, rows = None, []
        for current in range(1, page + 1):
            params = {"query": query, "first": RESULTS_PER_PAGE}
            if cursor:
                params["after"] = cursor
            response = helix("search/channels", params)
            rows = response.get("data", [])
            cursor = (response.get("pagination") or {}).get("cursor")
            if not cursor:
                if current < page:
                    rows = []  # the requested page is past the last one
                break
        results = [
            _result(
                row["id"],
                row.get("display_name") or row.get("broadcaster_login"),
                row.get("thumbnail_url"),
            )
            for row in rows
        ]
        # Helix doesn't report a total; offer a next page while there is a cursor.
        total = (page - 1) * RESULTS_PER_PAGE + len(results)
        if cursor:
            total += RESULTS_PER_PAGE
        data = helpers.format_search_response(page, RESULTS_PER_PAGE, total, results)
        cache.set(cache_key, data)
    return data
