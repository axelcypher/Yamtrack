from django.conf import settings
from django.db import models

from app.models import Item


class ScrobbleSession(models.Model):
    """Minutes already counted for one play session reported by a client.

    Clients such as Castellan report the cumulative minutes of a session; only
    the difference to ``minutes_counted`` is added to the entry, so retries
    and interim reports never count twice.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=64)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    client = models.CharField(max_length=32, blank=True, default="")
    minutes_counted = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            models.UniqueConstraint(
                fields=["user", "session_id"], name="tracker_session_unique_user_id"
            )
        ]

    def __str__(self):
        """Return the session id and item."""
        return f"{self.session_id} ({self.item})"


class UsageEvent(models.Model):
    """One recorded unit of media use, for the usage statistics.

    Written for every applied scrobble: an episode or chapter (``units=1``)
    with the watch time if the client knows it, or a slice of play time.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    media_type = models.CharField(max_length=20)
    client = models.CharField(max_length=32, blank=True, default="")
    platform = models.CharField(max_length=64, blank=True, default="")
    units = models.PositiveIntegerField(default=0)
    minutes = models.PositiveIntegerField(default=0)
    occurred_at = models.DateTimeField()
    session_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        """Meta options for the model."""

        indexes = [models.Index(fields=["user", "occurred_at"])]
        ordering = ["-occurred_at"]

    def __str__(self):
        """Return what was used and when."""
        return f"{self.item} on {self.platform or self.client} at {self.occurred_at}"
