"""Find TV shows that a user also tracks as anime and record them as links.

A TMDB season maps to MAL entries through AniBridge. When the user tracks
one of those MAL entries as anime, the show gets an ``AnimeTvLink``. Links
only hide and connect entries; nothing is deleted.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from app.models import TV, Anime, MediaTypes, Sources

from .mappings import mal_ids_for_tmdb_season
from .models import AnimeTvLink


@dataclass
class LinkPlan:
    """Links a detection run would create, update or remove for one user."""

    create: list = field(default_factory=list)  # (tv, anime_item, seasons)
    update: list = field(default_factory=list)  # (link, seasons)
    remove: list = field(default_factory=list)  # links
    keep: list = field(default_factory=list)  # links

    @property
    def changed(self):
        """Whether applying the plan changes anything."""
        return bool(self.create or self.update or self.remove)


def _anime_items(user):
    """MAL id → Item of the anime the user tracks."""
    return {
        anime.item.media_id: anime.item
        for anime in Anime.objects.filter(
            user=user, item__source=Sources.MAL.value
        ).select_related("item")
    }


def plan_links(user, mapping, tvs=None):
    """Compare the links AniBridge implies with the stored ones."""
    anime_items = _anime_items(user)
    wanted = defaultdict(set)  # (tv, anime item) -> seasons
    if tvs is None:
        tvs = TV.objects.filter(user=user, item__source=Sources.TMDB.value)
    for tv in tvs.select_related("item").prefetch_related("seasons__item"):
        seasons = sorted({season.item.season_number for season in tv.seasons.all()})
        for number in seasons or [1]:
            for mal_id in mal_ids_for_tmdb_season(mapping, tv.item.media_id, number):
                if mal_id in anime_items:
                    wanted[(tv, anime_items[mal_id])].add(number)

    existing = {
        (link.tv_id, link.anime_item_id): link
        for link in AnimeTvLink.objects.filter(user=user, tv__in=tvs)
    }
    plan = LinkPlan()
    for (tv, item), numbers in wanted.items():
        link = existing.pop((tv.pk, item.pk), None)
        seasons = sorted(numbers)
        if link is None:
            plan.create.append((tv, item, seasons))
        elif link.seasons != seasons:
            plan.update.append((link, seasons))
        else:
            plan.keep.append(link)
    plan.remove.extend(existing.values())
    return plan


def apply_links(user, plan):
    """Store a link plan."""
    with transaction.atomic():
        AnimeTvLink.objects.bulk_create(
            [
                AnimeTvLink(user=user, tv=tv, anime_item=item, seasons=seasons)
                for tv, item, seasons in plan.create
            ],
            ignore_conflicts=True,
        )
        for link, seasons in plan.update:
            link.seasons = seasons
            link.save(update_fields=["seasons"])
        AnimeTvLink.objects.filter(pk__in=[link.pk for link in plan.remove]).delete()


def refresh_links(user, mapping, tvs=None):
    """Detect and store the links of a user (or only of the given shows)."""
    plan = plan_links(user, mapping, tvs)
    if plan.changed:
        apply_links(user, plan)
    return plan


def links_for_media(user, media_type, source, media_id):
    """Links touching one detail page: by TMDB show or by MAL anime."""
    links = AnimeTvLink.objects.filter(user=user).select_related(
        "tv__item", "anime_item"
    )
    if source == Sources.TMDB.value and media_type in (
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
    ):
        return list(links.filter(tv__item__media_id=str(media_id)))
    if source == Sources.MAL.value and media_type == MediaTypes.ANIME.value:
        return list(links.filter(anime_item__media_id=str(media_id)))
    return []
