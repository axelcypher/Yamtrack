"""Progress per item in a date range, for ``GET /api/tracker/v1/activity``.

Games count played minutes, anime watched episodes and manga read chapters,
taken from the progress history the same way as the usage statistics: imports
and catch-up jumps do not count, and neither do writes mirrored by the
anime/TV sync. Episodes of a TV season linked to an anime count for that anime
in MAL numbering, so an episode tracked on both sides counts once.
"""

from collections import Counter, defaultdict

from app.models import Anime, Game, Item, Manga, MediaTypes
from integrations.webhooks import anime_mappings

from . import usage_stats
from .mappings import mal_from_tmdb_show
from .models import AnimeTvLink

UNITS = {
    MediaTypes.GAME.value: "minutes",
    MediaTypes.ANIME.value: "episodes",
    MediaTypes.MANGA.value: "chapters",
}


def _linked_tv_episodes(user, start, end):
    """(anime item id, MAL episode) for watched episodes of linked TV seasons."""
    targets = defaultdict(dict)  # (tv id, season) -> {MAL id: anime item id}
    for link in AnimeTvLink.objects.filter(user=user).select_related("anime_item"):
        for number in link.seasons:
            targets[(link.tv_id, number)][link.anime_item.media_id] = link.anime_item_id
    if not targets:
        return set()

    rows = usage_stats.watched_episodes(
        user,
        start,
        end,
        "related_season__related_tv_id",
        "related_season__related_tv__item__media_id",
        "related_season__item__season_number",
        "item__episode_number",
    )
    watched, mapping = set(), None
    for tv_id, tmdb_id, season_number, episode_number in rows:
        season_targets = targets.get((tv_id, season_number))
        if not season_targets:
            continue
        if mapping is None:
            mapping = anime_mappings.fetch_mapping_data()
        mal_id, mal_episode = mal_from_tmdb_show(
            mapping, tmdb_id, season_number, episode_number
        )
        item_id = season_targets.get(str(mal_id))
        if item_id and mal_episode:
            watched.add((item_id, mal_episode))
    return watched


def _anime_episodes(user, start, end):
    watched = _linked_tv_episodes(user, start, end)
    for _moment, item_id, first, last in usage_stats.history_increases(
        Anime, user, start, end
    ):
        watched.update((item_id, episode) for episode in range(first, last + 1))
    return Counter(item_id for item_id, _episode in watched)


def _summed(deltas):
    amounts = Counter()
    for _moment, item_id, delta in deltas:
        amounts[item_id] += delta
    return amounts


def _game_minutes(user, start, end):
    return _summed(
        usage_stats.history_deltas(
            Game,
            user,
            start,
            end,
            max_step=usage_stats.MAX_GAME_STEP,
            units=False,
        )
    )


def _manga_chapters(user, start, end):
    return _summed(usage_stats.history_deltas(Manga, user, start, end))


AMOUNTS = {
    MediaTypes.GAME.value: _game_minutes,
    MediaTypes.ANIME.value: _anime_episodes,
    MediaTypes.MANGA.value: _manga_chapters,
}


def activity(user, media_type, start, end):
    """Return the items with progress in the range, largest amount first."""
    amounts = AMOUNTS[media_type](user, start, end)
    items = Item.objects.in_bulk([key for key, amount in amounts.items() if amount])
    rows = [
        {
            "source": item.source,
            "media_id": item.media_id,
            "title": item.title,
            "image": item.image,
            "amount": amounts[item_id],
            "unit": UNITS[media_type],
        }
        for item_id, item in items.items()
    ]
    rows.sort(key=lambda row: (-row["amount"], row["title"].casefold()))
    return rows
