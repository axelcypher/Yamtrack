"""Record a watched episode, chapter or movie, and undo it again.

Progress only moves forward: a unit that is already recorded is reported as
``already_tracked`` instead of being written twice. Every write is captured in
a signed undo token so the extension can revert exactly what it changed.
"""

import logging
from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from simple_history.utils import bulk_update_with_history

from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Status,
)
from app.providers import services

from .auth import ApiError

logger = logging.getLogger(__name__)

UNDO_MAX_AGE = 60 * 60
# A completed movie seen again this soon is the same viewing, not a rewatch.
MOVIE_REWATCH_GAP = timedelta(hours=6)
MEDIA_FIELDS = ("progress", "status", "start_date", "end_date")
DATE_FIELDS = ("start_date", "end_date")
MODELS = {model.__name__: model for model in (TV, Season, Episode, Anime, Manga, Movie)}
PROGRESS_MODELS = {MediaTypes.ANIME.value: Anime, MediaTypes.MANGA.value: Manga}
SCROBBLE_TYPES = (
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.MOVIE.value,
)

_signer = signing.TimestampSigner(salt="tracker_api.undo")


def _now():
    return timezone.now().replace(second=0, microsecond=0)


def _int(value):
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class ChangeSet:
    """Rows created and fields overwritten by one scrobble."""

    def __init__(self):
        """Start with no changes."""
        self.created = []
        self.updated = []

    def add_created(self, obj):
        """Remember a row to delete on undo."""
        self.created.append([type(obj).__name__, obj.pk])

    def add_updated(self, obj, previous):
        """Remember the previous field values to restore on undo."""
        serialized = {
            field: value.isoformat() if hasattr(value, "isoformat") else value
            for field, value in previous.items()
        }
        self.updated.append([type(obj).__name__, obj.pk, serialized])

    def token(self, user):
        """Signed undo token, or None when nothing changed."""
        if not self.created and not self.updated:
            return None
        return _signer.sign_object(
            {"user": user.pk, "created": self.created, "updated": self.updated}
        )


def _snapshot(instance, fields):
    return {field: getattr(instance, field) for field in fields}


def _get_or_create_item(media_type, source, media_id, metadata, season_number=None):
    item, _ = Item.objects.get_or_create(
        media_id=media_id,
        source=source,
        media_type=media_type,
        season_number=season_number,
        defaults={"title": metadata["title"], "image": metadata["image"]},
    )
    return item


def scrobble(user, data):
    """Apply a scrobble request and return the API response."""
    media_type = data.get("media_type")
    if media_type == MediaTypes.SEASON.value:
        media_type = MediaTypes.TV.value
    source = data.get("source")
    media_id = str(data.get("media_id") or "")
    unit = _int(data.get("unit_number"))
    season_number = _int(data.get("season_number"))

    if media_type not in SCROBBLE_TYPES:
        raise ApiError(400, "invalid_media_type")
    if not source or not media_id:
        raise ApiError(400, "invalid_target", "source and media_id are required")
    if media_type != MediaTypes.MOVIE.value and (unit is None or unit < 1):
        raise ApiError(400, "invalid_unit", "unit_number must be a positive integer")

    changes = ChangeSet()
    with transaction.atomic():
        if media_type == MediaTypes.TV.value:
            season_number = season_number if season_number is not None else 1
            applied = _scrobble_episode(
                user, source, media_id, season_number, unit, changes
            )
        elif media_type == MediaTypes.MOVIE.value:
            applied = _scrobble_movie(user, source, media_id, changes)
        else:
            applied = _scrobble_progress(
                user, media_type, source, media_id, unit, changes
            )

    return {
        "applied": applied,
        "reason": None if applied else "already_tracked",
        "state": progress_state(user, media_type, source, media_id, season_number),
        "undo": changes.token(user) if applied else None,
    }


def _scrobble_episode(user, source, media_id, season_number, episode_number, changes):
    tv_metadata = services.get_media_metadata(
        "tv_with_seasons", media_id, source, [season_number]
    )
    season_metadata = tv_metadata.get(f"season/{season_number}")
    known_episodes = {
        episode["episode_number"]
        for episode in (season_metadata or {}).get("episodes", [])
    }
    if episode_number not in known_episodes:
        raise ApiError(
            400,
            "unknown_episode",
            f"S{season_number}E{episode_number} does not exist for this show",
        )

    tv_item = _get_or_create_item(MediaTypes.TV.value, source, media_id, tv_metadata)
    tv = TV.objects.filter(item=tv_item, user=user).first()
    season_item = _get_or_create_item(
        MediaTypes.SEASON.value, source, media_id, season_metadata, season_number
    )

    if (
        tv
        and Episode.objects.filter(
            related_season__related_tv=tv,
            related_season__item=season_item,
            item__episode_number=episode_number,
        ).exists()
    ):
        return False

    tv_before = tv.status if tv else None
    seasons_before = (
        dict(Season.objects.filter(related_tv=tv).values_list("pk", "status"))
        if tv
        else {}
    )

    if tv is None:
        tv = TV.objects.create(item=tv_item, user=user, status=Status.IN_PROGRESS.value)
    elif tv.status != Status.IN_PROGRESS.value:
        tv.status = Status.IN_PROGRESS.value
        tv.save()

    season, _ = Season.objects.get_or_create(
        item=season_item,
        user=user,
        related_tv=tv,
        defaults={"status": Status.IN_PROGRESS.value},
    )
    if season.status != Status.IN_PROGRESS.value:
        season.status = Status.IN_PROGRESS.value
        season.save()

    episode = Episode.objects.create(
        item=season.get_episode_item(episode_number, season_metadata),
        related_season=season,
        end_date=_now(),
    )

    _record_tv_changes(changes, tv, tv_before, seasons_before)
    changes.add_created(episode)
    return True


def _record_tv_changes(changes, tv, tv_before, seasons_before):
    """Record what saving TV, Season and Episode changed.

    Those saves can start or complete other seasons, so compare against the
    state before instead of tracking each step. ``tv_before`` is None when the
    show was created by this scrobble.
    """
    tv.refresh_from_db()
    if tv_before is None:
        changes.add_created(tv)
    elif tv.status != tv_before:
        changes.add_updated(tv, {"status": tv_before})
    for season_pk, status in Season.objects.filter(related_tv=tv).values_list(
        "pk", "status"
    ):
        if season_pk not in seasons_before:
            changes.created.append(["Season", season_pk])
        elif status != seasons_before[season_pk]:
            changes.updated.append(
                ["Season", season_pk, {"status": seasons_before[season_pk]}]
            )


def _scrobble_progress(user, media_type, source, media_id, unit, changes):
    model = PROGRESS_MODELS[media_type]
    metadata = services.get_media_metadata(media_type, media_id, source)
    item = _get_or_create_item(media_type, source, media_id, metadata)
    instance = (
        model.objects.filter(item=item, user=user).order_by("-created_at").first()
    )

    if instance and instance.status == Status.COMPLETED.value:
        if unit != 1:
            return False
        instance = None  # starting over: a new instance, like the webhooks do

    if instance:
        if instance.progress >= unit:
            return False
        changes.add_updated(instance, _snapshot(instance, MEDIA_FIELDS))
        if instance.status != Status.IN_PROGRESS.value:
            instance.status = Status.IN_PROGRESS.value
            instance.start_date = instance.start_date or _now()
        # Media.save() clamps to max_progress and completes the entry.
        instance.progress = unit
        instance.save()
    else:
        instance = model.objects.create(
            item=item,
            user=user,
            progress=unit,
            status=Status.IN_PROGRESS.value,
            start_date=_now(),
        )
        changes.add_created(instance)
    return True


def _scrobble_movie(user, source, media_id, changes):
    metadata = services.get_media_metadata(MediaTypes.MOVIE.value, media_id, source)
    item = _get_or_create_item(MediaTypes.MOVIE.value, source, media_id, metadata)
    instance = (
        Movie.objects.filter(item=item, user=user).order_by("-created_at").first()
    )
    now = _now()

    if instance and instance.status == Status.COMPLETED.value:
        if instance.end_date and now - instance.end_date < MOVIE_REWATCH_GAP:
            return False
        instance = None

    if instance:
        changes.add_updated(instance, _snapshot(instance, MEDIA_FIELDS))
        instance.progress = 1
        instance.status = Status.COMPLETED.value
        instance.end_date = now
        instance.save()
    else:
        instance = Movie.objects.create(
            item=item,
            user=user,
            progress=1,
            status=Status.COMPLETED.value,
            end_date=now,
        )
        changes.add_created(instance)
    return True


def undo(user, token):
    """Revert the changes recorded in an undo token."""
    if not isinstance(token, str) or not token:
        raise ApiError(400, "invalid_undo")
    try:
        data = _signer.unsign_object(token, max_age=UNDO_MAX_AGE)
    except signing.SignatureExpired as error:
        raise ApiError(400, "undo_expired") from error
    except signing.BadSignature as error:
        raise ApiError(400, "invalid_undo") from error
    if data.get("user") != user.pk:
        raise ApiError(403, "forbidden")

    with transaction.atomic():
        for model_name, pk in reversed(data["created"]):
            obj = (
                MODELS[model_name]
                .objects.filter(pk=pk, **_owner(model_name, user))
                .first()
            )
            if obj:
                obj.delete()
        for model_name, pk, previous in data["updated"]:
            model = MODELS[model_name]
            obj = model.objects.filter(pk=pk, **_owner(model_name, user)).first()
            if not obj:
                continue
            for field, value in previous.items():
                setattr(
                    obj,
                    field,
                    parse_datetime(value) if field in DATE_FIELDS and value else value,
                )
            bulk_update_with_history(
                [obj], model, fields=list(previous), default_user=user
            )
    logger.info("Undid API scrobble for %s", user)
    return {"undone": True}


def _owner(model_name, user):
    if model_name == "Episode":
        return {"related_season__user": user}
    return {"user": user}


def progress_state(user, media_type, source, media_id, season_number=None):
    """Return the user's newest entry for an item, as the extension shows it."""
    if media_type == MediaTypes.TV.value:
        return _tv_state(user, source, media_id, season_number)

    model = PROGRESS_MODELS.get(media_type) or (
        Movie if media_type == MediaTypes.MOVIE.value else None
    )
    if model is None:
        raise ApiError(400, "invalid_media_type")
    instance = (
        model.objects.filter(
            user=user,
            item__media_id=media_id,
            item__source=source,
            item__media_type=media_type,
        )
        .order_by("-created_at")
        .first()
    )
    if not instance:
        return {"tracked": False}
    metadata = services.get_media_metadata(media_type, media_id, source)
    return {
        "tracked": True,
        "status": instance.status,
        "progress": instance.progress,
        "max_progress": metadata.get("max_progress"),
        "score": float(instance.score) if instance.score is not None else None,
    }


def _tv_state(user, source, media_id, season_number):
    tv = TV.objects.filter(
        user=user,
        item__media_id=media_id,
        item__source=source,
        item__media_type=MediaTypes.TV.value,
    ).first()
    if not tv:
        return {"tracked": False}
    state = {
        "tracked": True,
        "status": tv.status,
        "score": float(tv.score) if tv.score is not None else None,
    }
    if season_number is None:
        return state

    season = Season.objects.filter(
        related_tv=tv, item__season_number=season_number
    ).first()
    if not season:
        return {**state, "progress": 0, "watched_episodes": []}
    watched = sorted(
        set(
            Episode.objects.filter(related_season=season).values_list(
                "item__episode_number", flat=True
            )
        )
    )
    metadata = services.get_media_metadata(
        MediaTypes.SEASON.value, media_id, source, [season_number]
    )
    return {
        **state,
        "status": season.status,
        "progress": season.progress,
        "max_progress": len(metadata.get("episodes", [])),
        "watched_episodes": watched,
    }
