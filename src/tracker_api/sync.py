"""Keep linked TV shows and anime entries at the same progress.

Episodes recorded on the TV side move the anime forward, and anime progress
records the missing TMDB episodes of the linked show. Both directions only
move forward and never touch deletions; bulk writes (imports, "mark season
completed") send no signals and are caught up by ``reconcile_user``.
"""

import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.utils import timezone

from app.models import TV, Anime, Episode, Item, MediaTypes, Season, Sources, Status
from app.providers import services
from integrations.webhooks import anime_mappings

from .links import plan_links, refresh_links
from .mappings import mal_from_tmdb_show, tmdb_from_mal
from .models import AnimeTvLink

logger = logging.getLogger(__name__)

# Upper bound of TV episodes created for one anime in one run.
MAX_EPISODES_PER_RUN = 300
RESUMABLE = (Status.PLANNING.value, Status.PAUSED.value, Status.DROPPED.value)
# History change reason of mirrored writes; the usage statistics skip them.
SYNC_REASON = "yamtrack-plus sync"
SNAPSHOT_FIELDS = ("progress", "status", "start_date", "end_date")

_syncing = ContextVar("tracker_api_syncing", default=False)
# A scrobble's ChangeSet, so its undo also reverts the mirrored side.
change_sink = ContextVar("tracker_api_change_sink", default=None)


@contextmanager
def syncing():
    """Mark mirror writes so the signals they cause do not sync back."""
    token = _syncing.set(True)
    try:
        yield
    finally:
        _syncing.reset(token)


def _active():
    return not _syncing.get() and not Item._disable_calendar_triggers


def _created(obj):
    sink = change_sink.get()
    if sink is not None:
        sink.add_created(obj)


def _updated(obj, previous):
    sink = change_sink.get()
    if sink is not None:
        sink.add_updated(obj, previous)


def _newest_anime(user, item):
    return Anime.objects.filter(user=user, item=item).order_by("-created_at").first()


def advance_anime(anime, mal_episode, when=None):
    """Move an anime entry forward to ``mal_episode``; never backwards."""
    if anime.status == Status.COMPLETED.value or anime.progress >= mal_episode:
        return False
    _updated(anime, {field: getattr(anime, field) for field in SNAPSHOT_FIELDS})
    if anime.status in RESUMABLE:
        anime.status = Status.IN_PROGRESS.value
        anime.start_date = anime.start_date or when or timezone.now()
    anime.progress = mal_episode
    anime._change_reason = SYNC_REASON
    anime.save()  # Media.save() completes it at max_progress
    return True


def _season(tv, season_number, *, dry_run):
    """Return the season of a linked show, creating it unless this is a dry run."""
    season = Season.objects.filter(
        related_tv=tv, item__season_number=season_number
    ).first()
    metadata = services.get_media_metadata(
        "tv_with_seasons", tv.item.media_id, tv.item.source, [season_number]
    ).get(f"season/{season_number}")
    if season or dry_run or not metadata:
        return season, metadata
    item, _ = Item.objects.get_or_create(
        media_id=tv.item.media_id,
        source=tv.item.source,
        media_type=MediaTypes.SEASON.value,
        season_number=season_number,
        defaults={"title": tv.item.title, "image": metadata["image"]},
    )
    season = Season.objects.create(
        item=item, user=tv.user, related_tv=tv, status=Status.IN_PROGRESS.value
    )
    _created(season)
    return season, metadata


def fill_tv_episodes(links, mapping, mal_id, mal_episodes, when, *, dry_run=False):
    """Record the TMDB episodes behind ``mal_episodes`` on the linked shows.

    Returns how many episodes were (or, in a dry run, would be) created.
    """
    shows = {link.tv.item.media_id: link.tv for link in links}
    targets = defaultdict(set)
    for mal_episode in mal_episodes:
        mapped = tmdb_from_mal(mapping, mal_id, mal_episode)
        if mapped and mapped[0] in shows:
            targets[(mapped[0], mapped[1])].add(mapped[2])

    created = 0
    for (tmdb_id, season_number), episodes in sorted(targets.items()):
        season, metadata = _season(shows[tmdb_id], season_number, dry_run=dry_run)
        if not metadata:
            continue
        known = {episode["episode_number"] for episode in metadata.get("episodes", [])}
        watched = (
            set(
                Episode.objects.filter(related_season=season).values_list(
                    "item__episode_number", flat=True
                )
            )
            if season
            else set()
        )
        for number in sorted((episodes & known) - watched):
            if created >= MAX_EPISODES_PER_RUN:
                logger.info("Episode limit reached for MAL %s, rest next run", mal_id)
                return created
            created += 1
            if dry_run:
                continue
            episode = Episode(
                item=season.get_episode_item(number, metadata),
                related_season=season,
                end_date=when,
            )
            episode._change_reason = SYNC_REASON
            episode.save()
            _created(episode)
    return created


def episode_created(episode):
    """Move the linked anime forward after a TV episode was recorded."""
    if not _active():
        return
    season = episode.related_season
    tv = season.related_tv
    if tv.item.source != Sources.TMDB.value:
        return
    if not Anime.objects.filter(user=tv.user, item__source=Sources.MAL.value).exists():
        return

    mapping = anime_mappings.fetch_mapping_data()
    mal_id, mal_episode = mal_from_tmdb_show(
        mapping,
        tv.item.media_id,
        season.item.season_number,
        episode.item.episode_number,
    )
    if not mal_id or not mal_episode:
        return
    anime = (
        Anime.objects.filter(
            user=tv.user,
            item__media_id=str(mal_id),
            item__source=Sources.MAL.value,
            item__media_type=MediaTypes.ANIME.value,
        )
        .order_by("-created_at")
        .first()
    )
    if anime is None:
        return
    refresh_links(tv.user, mapping, TV.objects.filter(pk=tv.pk))
    with syncing():
        advance_anime(anime, mal_episode, episode.end_date)


def anime_progressed(anime, previous_progress):
    """Anime progress went up: record the missing episodes on linked shows."""
    if not _active() or anime.item.source != Sources.MAL.value:
        return
    links = list(
        AnimeTvLink.objects.filter(
            user=anime.user, anime_item=anime.item
        ).select_related("tv__item", "tv__user")
    )
    if not links:
        return
    when = anime.end_date if anime.status == Status.COMPLETED.value else None
    with syncing():
        fill_tv_episodes(
            links,
            anime_mappings.fetch_mapping_data(),
            anime.item.media_id,
            range(previous_progress + 1, anime.progress + 1),
            when or timezone.now(),
        )


@dataclass
class PairResult:
    """What reconciling one anime with its linked shows changed."""

    title: str
    anime_before: int
    anime_after: int
    tv_episodes: int


def _tv_max_episode(links, mapping, mal_id):
    """Highest MAL episode, and its watch date, recorded on the linked shows."""
    best, best_date = 0, None
    episodes = Episode.objects.filter(
        related_season__related_tv__in=[link.tv for link in links]
    ).select_related("item", "related_season__related_tv__item")
    for episode in episodes:
        tv_item = episode.related_season.related_tv.item
        mapped_id, mal_episode = mal_from_tmdb_show(
            mapping,
            tv_item.media_id,
            episode.item.season_number,
            episode.item.episode_number,
        )
        if str(mapped_id) == str(mal_id) and mal_episode and mal_episode > best:
            best, best_date = mal_episode, episode.end_date
    return best, best_date


def _current_links(user, mapping, *, dry_run):
    """Return the links after a refresh; a dry run only plans the refresh."""
    if not dry_run:
        refresh_links(user, mapping)
        return list(
            AnimeTvLink.objects.filter(user=user).select_related(
                "tv__item", "tv__user", "anime_item"
            )
        )
    plan = plan_links(user, mapping)
    planned = [
        AnimeTvLink(user=user, tv=tv, anime_item=item, seasons=seasons)
        for tv, item, seasons in plan.create
    ]
    return planned + plan.keep + [link for link, _seasons in plan.update]


def reconcile_user(user, mapping, *, dry_run=False):
    """Refresh a user's links and bring every linked pair to the same progress."""
    by_anime = defaultdict(list)
    for link in _current_links(user, mapping, dry_run=dry_run):
        by_anime[link.anime_item].append(link)

    results = []
    for item, links in by_anime.items():
        anime = _newest_anime(user, item)
        if anime is None:
            continue
        before = anime.progress
        tv_max, tv_date = _tv_max_episode(links, mapping, item.media_id)
        after = before
        with syncing():
            if tv_max > before and anime.status != Status.COMPLETED.value:
                after = tv_max
                if not dry_run:
                    advance_anime(anime, tv_max, tv_date)
                    after = anime.progress
            when = anime.end_date if anime.status == Status.COMPLETED.value else None
            filled = fill_tv_episodes(
                links,
                mapping,
                item.media_id,
                range(1, after + 1),
                when or timezone.now(),
                dry_run=dry_run,
            )
        if after != before or filled:
            results.append(PairResult(item.title, before, after, filled))
    return results
