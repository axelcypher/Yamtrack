import logging

from . import sync

logger = logging.getLogger(__name__)


def episode_saved(sender, instance, created, **kwargs):  # noqa: ARG001
    """Mirror a newly recorded TV episode onto the linked anime."""
    if not created or kwargs.get("raw"):
        return
    try:
        sync.episode_created(instance)
    except Exception:
        # The sync is a convenience; never break the save that triggered it.
        logger.exception("Anime/TV sync failed for episode %s", instance.pk)


def anime_saved(sender, instance, created, **kwargs):  # noqa: ARG001
    """Mirror raised anime progress onto the linked TV shows."""
    if kwargs.get("raw"):
        return
    previous = 0 if created else instance.tracker.previous("progress") or 0
    if instance.progress <= previous:
        return
    try:
        sync.anime_progressed(instance, previous)
    except Exception:
        logger.exception("Anime/TV sync failed for anime %s", instance.pk)
