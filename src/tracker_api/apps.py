from django.apps import AppConfig


class TrackerApiConfig(AppConfig):
    """Token API for the yamtrack-tracker browser extension."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "tracker_api"

    def ready(self):
        """Connect the anime/TV progress sync to Episode and Anime saves."""
        from django.db.models.signals import post_save  # noqa: PLC0415

        from app.models import Anime, Episode  # noqa: PLC0415

        from . import signals  # noqa: PLC0415

        post_save.connect(
            signals.episode_saved, sender=Episode, dispatch_uid="tracker_api_episode"
        )
        post_save.connect(
            signals.anime_saved, sender=Anime, dispatch_uid="tracker_api_anime"
        )
