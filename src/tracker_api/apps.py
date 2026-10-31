from django.apps import AppConfig


class TrackerApiConfig(AppConfig):
    """Token API for the yamtrack-tracker browser extension."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "tracker_api"
