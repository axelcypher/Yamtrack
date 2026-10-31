"""Merge anime that are tracked as TMDB TV shows into the user's anime entries.

Watched episodes are mapped through AniBridge's TMDB entries (TMDB
season/episode → MAL entry/episode), matching the numbering Yamtrack stores.
A TV season or show is only removed when everything in it was mapped and
merged; anything AniBridge cannot map stays where it is.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction
from simple_history.utils import bulk_update_with_history

from app.models import TV, Anime, Item, MediaTypes, Sources, Status
from app.providers import services

from .mappings import mal_from_tmdb_show

logger = logging.getLogger(__name__)


@dataclass
class AnimeMerge:
    """Everything from TV entries that belongs to one MAL entry."""

    mal_id: str
    anime: Anime | None
    # MAL episode number -> latest watch date of the TV episodes mapped to it
    episodes: dict = field(default_factory=dict)
    episode_rows: list = field(default_factory=list)
    # Seasons (or shows without seasons) the entry came from
    parts: list = field(default_factory=list)
    labels: list = field(default_factory=list)

    @property
    def max_episode(self):
        """Highest MAL episode watched on the TV side (0 when none)."""
        return max(self.episodes, default=0)

    @property
    def dates(self):
        """Watch dates of the mapped TV episodes."""
        return [date for date in self.episodes.values() if date]

    def add_part(self, part, label):
        """Remember a season or show that maps to this entry."""
        if part not in self.parts:
            self.parts.append(part)
            self.labels.append(label)

    def add_episode(self, mal_episode, episode):
        """Remember a watched TV episode mapped to ``mal_episode``."""
        self.episode_rows.append(episode)
        previous = self.episodes.get(mal_episode)
        if previous is None or (episode.end_date and episode.end_date > previous):
            self.episodes[mal_episode] = episode.end_date


@dataclass
class MergePlan:
    """What a merge for one user would change."""

    user: object
    merges: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)
    delete_episodes: list = field(default_factory=list)
    delete_seasons: list = field(default_factory=list)
    delete_tvs: list = field(default_factory=list)


def _newest_anime(user, mal_id):
    return (
        Anime.objects.filter(
            user=user,
            item__media_id=mal_id,
            item__source=Sources.MAL.value,
            item__media_type=MediaTypes.ANIME.value,
        )
        .order_by("-created_at")
        .first()
    )


class _Planner:
    def __init__(self, user, mapping):
        self.user = user
        self.mapping = mapping
        self.candidates = {}
        # season/show pk -> MAL ids it maps to; pk -> whether something was unmapped
        self.targets = defaultdict(set)
        self.incomplete = set()

    def candidate(self, mal_id):
        mal_id = str(mal_id)
        if mal_id not in self.candidates:
            self.candidates[mal_id] = AnimeMerge(
                mal_id=mal_id, anime=_newest_anime(self.user, mal_id)
            )
        return self.candidates[mal_id]

    def map_show(self, tv, seasons):
        tmdb_id = tv.item.media_id
        if not seasons:
            # A planned show without seasons: its first episode decides.
            mal_id, _ = mal_from_tmdb_show(self.mapping, tmdb_id, 1, 1)
            if mal_id:
                self.candidate(mal_id).add_part(tv, tv.item.title)
                self.targets[("tv", tv.pk)].add(str(mal_id))
            return
        for season in seasons:
            self.map_season(tv, tmdb_id, season)

    def map_season(self, tv, tmdb_id, season):
        key = ("season", season.pk)
        number = season.item.season_number
        label = f"{tv.item.title} S{number}"
        episodes = list(season.episodes.select_related("item"))
        if not episodes:
            mal_id, _ = mal_from_tmdb_show(self.mapping, tmdb_id, number, 1)
            if mal_id:
                self.candidate(mal_id).add_part(season, label)
                self.targets[key].add(str(mal_id))
            else:
                self.incomplete.add(key)
            return
        for episode in episodes:
            mal_id, mal_episode = mal_from_tmdb_show(
                self.mapping, tmdb_id, number, episode.item.episode_number
            )
            if not (mal_id and mal_episode):
                self.incomplete.add(key)
                continue
            merge = self.candidate(mal_id)
            merge.add_part(season, label)
            merge.add_episode(mal_episode, episode)
            self.targets[key].add(str(mal_id))

    def removable(self, key, merged_ids):
        return (
            bool(self.targets.get(key))
            and self.targets[key] <= merged_ids
            and key not in self.incomplete
        )


def build_plan(user, mapping, *, include_tv_only=False):
    """Collect the TV entries of ``user`` that AniBridge maps to MAL entries.

    Only MAL entries the user already tracks as anime are merged, unless
    ``include_tv_only`` also converts anime tracked solely as TV.
    """
    planner = _Planner(user, mapping)
    shows = []
    tvs = TV.objects.filter(user=user, item__source=Sources.TMDB.value).select_related(
        "item"
    )
    for tv in tvs:
        seasons = list(tv.seasons.select_related("item", "related_tv"))
        planner.map_show(tv, seasons)
        shows.append((tv, seasons))

    plan = MergePlan(user=user)
    for mal_id, merge in planner.candidates.items():
        if merge.anime or include_tv_only:
            plan.merges[mal_id] = merge
            plan.delete_episodes.extend(merge.episode_rows)
        else:
            plan.skipped.append(merge)

    merged_ids = set(plan.merges)
    for tv, seasons in shows:
        if not seasons:
            if planner.removable(("tv", tv.pk), merged_ids):
                plan.delete_tvs.append(tv)
            continue
        removable = [
            season
            for season in seasons
            if planner.removable(("season", season.pk), merged_ids)
        ]
        plan.delete_seasons.extend(removable)
        if len(removable) == len(seasons):
            plan.delete_tvs.append(tv)
    return plan


def describe_plan(plan):
    """Return human-readable lines for a plan."""
    lines = []
    for merge in plan.merges.values():
        if merge.anime:
            target = f"{merge.anime.item.title} (MAL {merge.mal_id})"
            before = f"{merge.anime.status}, {merge.anime.progress} eps"
        else:
            target = f"MAL {merge.mal_id}"
            before = "new anime entry"
        lines.append(
            f"  {', '.join(merge.labels)} -> {target}: {before}; "
            f"watched as TV up to ep {merge.max_episode}"
        )
    lines.extend(
        f"  skip {', '.join(merge.labels)} -> MAL {merge.mal_id}: "
        "not tracked as anime (--include-tv-only converts it)"
        for merge in plan.skipped
    )
    lines.append(
        f"  remove {len(plan.delete_tvs)} shows, {len(plan.delete_seasons)} seasons, "
        f"{len(plan.delete_episodes)} episode records"
    )
    return lines


def _first(values):
    return next((value for value in values if value not in (None, "")), None)


def _notes(merge, existing=""):
    notes = [part.notes for part in merge.parts if part.notes]
    extra = [note for note in dict.fromkeys(notes) if note not in existing]
    return "\n\n".join([existing, *extra]).strip()


def _fix_end_date(anime, merge, was_completed):
    """Use the last watch date instead of "now" when the merge completed it."""
    if anime.status == Status.COMPLETED.value and not was_completed and merge.dates:
        anime.end_date = max(merge.dates)
        bulk_update_with_history([anime], Anime, fields=["end_date"])


def _merge_into_existing(merge):
    anime = merge.anime
    was_completed = anime.status == Status.COMPLETED.value
    if merge.max_episode > anime.progress:
        anime.progress = merge.max_episode
        if anime.status == Status.PLANNING.value:
            anime.status = Status.IN_PROGRESS.value
    if merge.dates and (
        anime.start_date is None or min(merge.dates) < anime.start_date
    ):
        anime.start_date = min(merge.dates)
    if anime.score is None:
        anime.score = _first(part.score for part in merge.parts)
    anime.notes = _notes(merge, anime.notes)
    if anime.tracker.changed():
        anime.save()  # Media.save() completes the entry at max_progress
        _fix_end_date(anime, merge, was_completed)


def _created_status(merge):
    statuses = {part.status for part in merge.parts}
    if not merge.max_episode:
        return Status.PLANNING.value
    if Status.COMPLETED.value in statuses:
        return Status.COMPLETED.value
    if len(statuses) == 1 and statuses <= {Status.PAUSED.value, Status.DROPPED.value}:
        return statuses.pop()
    return Status.IN_PROGRESS.value


def _create_anime(user, merge):
    metadata = services.get_media_metadata(
        MediaTypes.ANIME.value, merge.mal_id, Sources.MAL.value
    )
    item, _ = Item.objects.get_or_create(
        media_id=merge.mal_id,
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        defaults={"title": metadata["title"], "image": metadata["image"]},
    )
    status = _created_status(merge)
    anime = Anime.objects.create(
        item=item,
        user=user,
        progress=merge.max_episode,
        status=status,
        start_date=min(merge.dates) if merge.dates else None,
        end_date=max(merge.dates)
        if merge.dates and status == Status.COMPLETED.value
        else None,
        score=_first(part.score for part in merge.parts),
        notes=_notes(merge),
    )
    _fix_end_date(anime, merge, was_completed=status == Status.COMPLETED.value)


def apply_plan(plan):
    """Merge into anime entries and remove what was fully merged."""
    deleted_seasons = {season.pk for season in plan.delete_seasons}
    deleted_tvs = {tv.pk for tv in plan.delete_tvs}
    with transaction.atomic():
        for merge in plan.merges.values():
            if merge.anime:
                _merge_into_existing(merge)
            else:
                _create_anime(plan.user, merge)
        for episode in plan.delete_episodes:
            # Whole seasons and shows are deleted below; single episodes one by
            # one so the remaining season and show statuses get updated.
            if episode.related_season_id not in deleted_seasons:
                episode.delete()
        for season in plan.delete_seasons:
            if season.related_tv_id not in deleted_tvs:
                season.delete()
        for tv in plan.delete_tvs:
            tv.delete()
    logger.info("Merged %d anime tracked as TV for %s", len(plan.merges), plan.user)
