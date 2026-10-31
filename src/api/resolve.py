"""Resolve what a streaming or reading site shows to a Yamtrack item.

Ids are tried before titles. TV numbering is mapped to MAL entries through
the AniBridge data the webhooks already use, so "S2E5" on a site can land on
the right MAL season entry and episode.
"""

import difflib
import logging
import unicodedata

from app.models import MediaTypes, Sources
from app.providers import services, tmdb, tvdb
from integrations.webhooks import anime_mappings

from .auth import ApiError

logger = logging.getLogger(__name__)

KINDS = ("episode", "movie", "chapter")
MAX_CANDIDATES = 5
FUZZY_MIN_RATIO = 0.5


def _int(value):
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def normalize_title(title):
    """Lowercase, accent-free, punctuation-free title for comparisons."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _target(media_type, source, media_id, metadata, season=None, unit=None):
    target = {
        "media_type": media_type,
        "source": source,
        "media_id": str(media_id),
        "title": metadata.get("title", ""),
        "image": metadata.get("image"),
    }
    if season is not None:
        target["season_number"] = season
    if unit is not None:
        target["unit_number"] = unit
    return target


def _mal_target(media_type, mal_id, unit):
    metadata = services.get_media_metadata(media_type, str(mal_id), Sources.MAL.value)
    return _target(media_type, Sources.MAL.value, mal_id, metadata, unit=unit)


def _tv_target(tmdb_id, season, episode, metadata=None):
    metadata = metadata or tmdb.tv(str(tmdb_id))
    return _target(
        MediaTypes.TV.value,
        Sources.TMDB.value,
        tmdb_id,
        metadata,
        season=season if season is not None else 1,
        unit=episode,
    )


def _movie_target(tmdb_id):
    metadata = tmdb.movie(str(tmdb_id))
    return _target(MediaTypes.MOVIE.value, Sources.TMDB.value, tmdb_id, metadata)


class Resolver:
    """Resolves one detection for one user."""

    def __init__(self, user, data):
        """Validate and keep the detection fields."""
        self.user = user
        self.kind = data.get("kind")
        if self.kind not in KINDS:
            raise ApiError(400, "invalid_kind", f"kind must be one of {KINDS}")
        self.hint = data.get("media_hint")
        self.title = (data.get("title") or "").strip()
        self.alt_titles = [
            title.strip()
            for title in data.get("alt_titles") or []
            if isinstance(title, str) and title.strip()
        ]
        self.year = _int(data.get("year"))
        self.season = _int(data.get("season"))
        self.episode = _int(data.get("episode"))
        self.chapter = _int(data.get("chapter"))
        ids = data.get("ids") or {}
        self.ids = {key: str(value) for key, value in ids.items() if value}
        self._mapping = None

    @property
    def unit(self):
        """Episode or chapter number; None for movies."""
        return self.chapter if self.kind == "chapter" else self.episode

    @property
    def wants_manga(self):
        """Whether the detection is a manga chapter."""
        return self.kind == "chapter" or self.hint == "manga"

    def mapping(self):
        """AniBridge mapping data, fetched once per request."""
        if self._mapping is None:
            self._mapping = anime_mappings.fetch_mapping_data()
        return self._mapping

    def resolve(self):
        """Return the resolve response for the detection."""
        for resolver in (
            self._by_mal,
            self._by_anidb,
            self._by_tvdb_episode,
            self._by_imdb,
            self._by_tmdb_show,
            self._by_tmdb_movie,
        ):
            target = resolver()
            if target:
                return response(target, "id")
        return self._by_title()

    # Id resolvers

    def _by_mal(self):
        mal_id = self.ids.get("mal")
        if not mal_id:
            return None
        if self.wants_manga:
            return _mal_target(MediaTypes.MANGA.value, mal_id, self.unit)
        return _mal_target(MediaTypes.ANIME.value, mal_id, self.unit or 1)

    def _by_anidb(self):
        anidb_id = self.ids.get("anidb")
        if not anidb_id or not self.user.anime_enabled:
            return None
        mal_id, mal_episode = anime_mappings.get_mal_id_from_anidb(
            self.mapping(), anidb_id, self.unit or 1
        )
        if not mal_id:
            return None
        return _mal_target(MediaTypes.ANIME.value, mal_id, mal_episode)

    def _by_tvdb_episode(self):
        tvdb_episode_id = self.ids.get("tvdb_episode")
        if self.kind != "episode" or not tvdb_episode_id:
            return None

        episode = tvdb.episode(int(tvdb_episode_id))
        if episode and self.user.anime_enabled:
            mal_id, mal_episode = anime_mappings.get_mal_id_from_tvdb(
                self.mapping(),
                episode["series_id"],
                episode["season_number"],
                episode["episode_number"],
            )
            if mal_id:
                return _mal_target(MediaTypes.ANIME.value, mal_id, mal_episode)

        found = self._find_tv_episode(tvdb_episode_id, "tvdb_id")
        if found:
            return found
        if episode:
            show_id = tvdb.series_tmdb_id(episode["series_id"])
            if show_id:
                return self._from_tmdb_show(
                    show_id, episode["season_number"], episode["episode_number"]
                )
        return None

    def _by_imdb(self):
        imdb_id = self.ids.get("imdb")
        if not imdb_id:
            return None
        if self.kind == "episode":
            return self._find_tv_episode(imdb_id, "imdb_id") or self._find_tv_show(
                imdb_id
            )
        if self.kind == "movie":
            results = tmdb.find(imdb_id, "imdb_id").get("movie_results") or []
            if results:
                return self._from_tmdb_movie(results[0]["id"], imdb_id)
        return None

    def _by_tmdb_show(self):
        show_id = self.ids.get("tmdb_show")
        if self.kind != "episode" or not show_id or self.episode is None:
            return None
        return self._from_tmdb_show(show_id, self.season, self.episode)

    def _by_tmdb_movie(self):
        movie_id = self.ids.get("tmdb_movie")
        if self.kind != "movie" or not movie_id:
            return None
        return self._from_tmdb_movie(movie_id, self.ids.get("imdb"))

    def _find_tv_episode(self, external_id, external_source):
        results = tmdb.find(external_id, external_source).get("tv_episode_results")
        if not results:
            return None
        result = results[0]
        return self._from_tmdb_show(
            result["show_id"], result["season_number"], result["episode_number"]
        )

    def _find_tv_show(self, imdb_id):
        results = tmdb.find(imdb_id, "imdb_id").get("tv_results")
        if not results or self.episode is None:
            return None
        return self._from_tmdb_show(results[0]["id"], self.season, self.episode)

    def _from_tmdb_show(self, show_id, season, episode):
        """TV target, or the MAL entry AniBridge maps this season/episode to."""
        metadata = tmdb.tv(str(show_id))
        season = season if season is not None else 1
        tvdb_id = metadata.get("tvdb_id")
        if self.user.anime_enabled and tvdb_id and episode is not None:
            mal_id, mal_episode = anime_mappings.get_mal_id_from_tvdb(
                self.mapping(), tvdb_id, season, episode
            )
            if mal_id:
                return _mal_target(MediaTypes.ANIME.value, mal_id, mal_episode)
        return _tv_target(show_id, season, episode, metadata)

    def _from_tmdb_movie(self, movie_id, imdb_id=None):
        """Movie target, or the MAL entry for an anime film."""
        if self.user.anime_enabled:
            mal_id = anime_mappings.get_mal_id_from_tmdb_movie(self.mapping(), movie_id)
            if not mal_id and imdb_id:
                mal_id = anime_mappings.get_mal_id_from_imdb(self.mapping(), imdb_id)
            if mal_id:
                return _mal_target(MediaTypes.ANIME.value, mal_id, 1)
        return _movie_target(movie_id)

    # Title resolution

    def _search(self, media_type, query):
        return services.search(
            media_type,
            query,
            1,
            show_adult_titles=self.user.show_adult_titles,
        )["results"]

    def _by_title(self):
        if not self.title:
            return response(None, "none")

        if self.wants_manga:
            return self._title_on_mal(MediaTypes.MANGA.value)

        tmdb_type = (
            MediaTypes.MOVIE.value if self.kind == "movie" else MediaTypes.TV.value
        )
        tmdb_results = self._search(tmdb_type, self.title)
        exact = pick_exact(tmdb_results, self.titles, self.year)
        tmdb_target = None
        if exact:
            if self.kind == "movie":
                tmdb_target = self._from_tmdb_movie(exact["media_id"])
            else:
                tmdb_target = self._from_tmdb_show(
                    exact["media_id"], self.season, self.episode
                )
            if self.hint != "anime" or tmdb_target["source"] == Sources.MAL.value:
                return response(tmdb_target, "title_exact")

        if self.hint == "anime" and self.user.anime_enabled:
            on_mal = self._title_on_mal(MediaTypes.ANIME.value)
            if on_mal["target"] or not tmdb_target:
                return on_mal
            # The site says anime but neither AniBridge nor MAL agree: let the
            # user confirm the TMDB show.
            return response(tmdb_target, "title_fuzzy", on_mal["candidates"])

        return response(None, "title_fuzzy", self._candidates(tmdb_results))

    def _title_on_mal(self, media_type):
        results = self._search(media_type, self.title)
        exact = pick_exact(results, self.titles, self.year)
        if exact:
            unit = self.unit if media_type == MediaTypes.MANGA.value else self.unit or 1
            target = _target(
                media_type,
                Sources.MAL.value,
                exact["media_id"],
                exact,
                unit=unit,
            )
            return response(target, "title_exact")
        return response(None, "title_fuzzy", self._candidates(results))

    @property
    def titles(self):
        """All titles of the detection, normalized and deduplicated."""
        return list(
            dict.fromkeys(
                normalize_title(title)
                for title in [self.title, *self.alt_titles]
                if title
            )
        )

    def _candidates(self, results):
        ranked = sorted(
            results,
            key=lambda result: similarity(result.get("title"), self.titles),
            reverse=True,
        )
        candidates = []
        for result in ranked[:MAX_CANDIDATES]:
            if similarity(result.get("title"), self.titles) < FUZZY_MIN_RATIO:
                break
            season = (
                self.season if result["media_type"] == MediaTypes.TV.value else None
            )
            if result["media_type"] == MediaTypes.TV.value and season is None:
                season = 1
            candidates.append(
                _target(
                    result["media_type"],
                    result["source"],
                    result["media_id"],
                    result,
                    season=season,
                    unit=self.unit,
                )
            )
        return candidates


def similarity(title, normalized_titles):
    """Best similarity ratio between a result title and the detection's titles."""
    candidate = normalize_title(title)
    return max(
        (
            difflib.SequenceMatcher(None, candidate, wanted).ratio()
            for wanted in normalized_titles
        ),
        default=0,
    )


def pick_exact(results, normalized_titles, year):
    """Return the one exact title match, using the year to break ties."""
    exact = [
        result
        for result in results
        if normalize_title(result.get("title")) in normalized_titles
    ]
    if len(exact) > 1 and year:
        exact = [
            result
            for result in exact
            if result.get("release_year") and abs(result["release_year"] - year) <= 1
        ]
    return exact[0] if len(exact) == 1 else None


def response(target, confidence, candidates=None):
    """Build the resolve response, adding the MAL entry for anime and manga."""
    mal = None
    if target and target["source"] == Sources.MAL.value:
        mal = {
            "type": target["media_type"],
            "id": target["media_id"],
            "unit_number": target.get("unit_number"),
        }
    return {
        "target": target,
        "mal": mal,
        "confidence": confidence if target else (confidence if candidates else "none"),
        "candidates": candidates or [],
    }
