"""JSON endpoints used by the yamtrack-tracker browser extension."""

from django.apps import apps
from django.http import JsonResponse

from app.models import MediaTypes
from app.providers import services

from . import scrobble as scrobble_service
from .auth import ApiError, api_view, json_body
from .resolve import Resolver

# 2: game scrobbling (session minutes), usage data, ``scrobble_types`` in me
API_VERSION = 2
SEARCH_TYPES = (
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.TV.value,
    MediaTypes.MOVIE.value,
    MediaTypes.GAME.value,
)


@api_view(["GET"])
def me(request):
    """Return who the token belongs to and what the server supports."""
    user = request.user
    return JsonResponse(
        {
            "api_version": API_VERSION,
            "username": user.username,
            "enabled_media_types": user.get_enabled_media_types(),
            "scrobble_types": scrobble_service.enabled_scrobble_types(user),
            "anime_enabled": user.anime_enabled,
        }
    )


@api_view(["GET"])
def search(request):
    """Search a provider, marking results the user already tracks."""
    media_type = request.GET.get("media_type")
    query = request.GET.get("q", "").strip()
    if media_type not in SEARCH_TYPES:
        raise ApiError(400, "invalid_media_type")
    if not query:
        raise ApiError(400, "missing_query")
    try:
        page = max(int(request.GET.get("page", 1)), 1)
    except ValueError:
        page = 1

    data = services.search(
        media_type,
        query,
        page,
        show_adult_titles=request.user.show_adult_titles,
    )
    results = _with_user_entries(request.user, media_type, data["results"])
    return JsonResponse(
        {"page": data["page"], "total_pages": data["total_pages"], "results": results}
    )


def _with_user_entries(user, media_type, results):
    model = apps.get_model(app_label="app", model_name=media_type)
    entries = {}
    rows = (
        model.objects.filter(
            user=user,
            item__media_type=media_type,
            item__media_id__in=[str(result["media_id"]) for result in results],
        )
        .select_related("item")
        .order_by("-created_at")
    )
    for row in rows:
        entries.setdefault((row.item.source, row.item.media_id), row)

    enriched = []
    for result in results:
        entry = entries.get((result["source"], str(result["media_id"])))
        enriched.append(
            {
                **result,
                "media_id": str(result["media_id"]),
                "user_status": entry.status if entry else None,
                # TV progress is computed across seasons; not worth it in a list.
                "user_progress": entry.progress
                if entry and media_type != MediaTypes.TV.value
                else None,
            }
        )
    return enriched


@api_view(["POST"])
def resolve(request):
    """Map a site detection to a Yamtrack item."""
    return JsonResponse(Resolver(request.user, json_body(request)).resolve())


@api_view(["GET"])
def progress(request):
    """Return the user's current entry for one item."""
    media_type = request.GET.get("media_type")
    if media_type == MediaTypes.SEASON.value:
        media_type = MediaTypes.TV.value
    source = request.GET.get("source")
    media_id = request.GET.get("media_id")
    if not source or not media_id:
        raise ApiError(400, "invalid_target", "source and media_id are required")
    season = request.GET.get("season_number")
    return JsonResponse(
        scrobble_service.progress_state(
            request.user,
            media_type,
            source,
            media_id,
            int(season) if season and season.isdigit() else None,
        )
    )


@api_view(["POST"])
def scrobble(request):
    """Record a watched episode, read chapter or watched movie."""
    return JsonResponse(scrobble_service.scrobble(request.user, json_body(request)))


@api_view(["POST"])
def undo(request):
    """Revert a scrobble using the token it returned."""
    return JsonResponse(
        scrobble_service.undo(request.user, json_body(request).get("undo"))
    )
