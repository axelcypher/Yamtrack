import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from app.models import Sources
from integrations.webhooks import anime_mappings

from . import sync

logger = logging.getLogger(__name__)


@shared_task(name="Reconcile anime and TV")
def reconcile_anime_tv():
    """Refresh anime/TV links and catch up progress the signals missed."""
    mapping = anime_mappings.fetch_mapping_data()
    users = (
        get_user_model()
        .objects.filter(anime__item__source=Sources.MAL.value, tv__isnull=False)
        .distinct()
    )
    changed = 0
    for user in users:
        changed += len(sync.reconcile_user(user, mapping))
    logger.info(
        "Reconciled anime and TV for %d users, %d pairs changed", users.count(), changed
    )
    return changed
