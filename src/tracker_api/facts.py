"""Cache genres and runtimes of library items for the usage statistics.

Provider metadata only lives in the cache for a day, so the statistics page
reads ``ItemFacts`` instead and a background task fills the missing rows.
"""

import logging
import re

from celery import shared_task

from app.models import TV, Anime, Game, Item, Manga, MediaTypes, Movie
from app.providers import services

from .models import ItemFacts

logger = logging.getLogger(__name__)

FACT_MODELS = (TV, Anime, Manga, Movie, Game)
BATCH_SIZE = 200


def parse_runtime(text):
    """Turn "1h 30m", "45m" or "24 min" into minutes; None when unknown."""
    if not text:
        return None
    hours = re.search(r"(\d+)\s*h", str(text))
    minutes = re.search(r"(\d+)\s*m", str(text))
    total = (int(hours.group(1)) * 60 if hours else 0) + (
        int(minutes.group(1)) if minutes else 0
    )
    return total or None


def facts_from_metadata(metadata):
    """Extract genres and runtime from provider metadata."""
    genres = [str(genre) for genre in metadata.get("genres") or []]
    runtime = parse_runtime((metadata.get("details") or {}).get("runtime"))
    return genres, runtime


def missing_items(user=None, limit=BATCH_SIZE):
    """Library items without cached facts, optionally of one user."""
    item_ids = set()
    for model in FACT_MODELS:
        entries = (
            model.objects.all() if user is None else model.objects.filter(user=user)
        )
        item_ids.update(entries.values_list("item_id", flat=True))
    known = set(ItemFacts.objects.values_list("item_id", flat=True))
    return list(Item.objects.filter(id__in=item_ids - known)[:limit])


def refresh_item(item):
    """Fetch and store the facts of one item."""
    media_type = MediaTypes.TV.value if item.media_type == "tv" else item.media_type
    metadata = services.get_media_metadata(media_type, item.media_id, item.source)
    genres, runtime = facts_from_metadata(metadata)
    ItemFacts.objects.update_or_create(
        item=item, defaults={"genres": genres, "runtime_minutes": runtime}
    )


@shared_task(name="Refresh item facts")
def refresh_item_facts(user_id=None, limit=BATCH_SIZE):
    """Fill missing genres/runtimes, a batch at a time."""
    from django.contrib.auth import get_user_model  # noqa: PLC0415

    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    done = 0
    for item in missing_items(user, limit):
        try:
            refresh_item(item)
            done += 1
        except services.ProviderAPIError:
            logger.info("No facts for %s yet, retrying on the next run", item)
    return done
