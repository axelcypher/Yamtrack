from integrations.webhooks import anime_mappings


def mal_from_tmdb_show(mapping_data, tmdb_id, season_number, episode_number):
    """Map a TMDB show season/episode to a MAL id and episode via AniBridge.

    Yamtrack stores TV progress in TMDB numbering, which differs from TVDB's
    for some shows (One Piece, for one), so TMDB seasons must use AniBridge's
    ``tmdb_show`` entries rather than the ``tvdb_show`` ones the webhooks use.
    Returns ``(None, None)`` when there is no mapping.
    """
    return anime_mappings._get_mal_mapping(
        mapping_data,
        f"tmdb_show:{tmdb_id}:s{season_number}",
        episode_number,
    )


def mal_ids_for_tmdb_season(mapping_data, tmdb_id, season_number):
    """Return every MAL id a TMDB season maps to (split cours give several)."""
    targets = mapping_data.get(f"tmdb_show:{tmdb_id}:s{season_number}", {})
    mal_ids = (anime_mappings._parse_mal_descriptor(target) for target in targets)
    return {str(mal_id) for mal_id in mal_ids if mal_id}


def tmdb_from_mal(mapping_data, mal_id, mal_episode):
    """Map a MAL episode to ``(tmdb_id, season, episode)`` via AniBridge.

    AniBridge lists ``mal:{id}`` → ``tmdb_show:{id}:s{n}`` ranges, the reverse
    of ``mal_from_tmdb_show``. Returns None when no TMDB season covers it.
    """
    for target, ranges in mapping_data.get(f"mal:{mal_id}", {}).items():
        provider, tmdb_id, season, *_ = [*target.split(":"), None, None]
        if provider != "tmdb_show" or not tmdb_id or not season:
            continue
        episode = anime_mappings._map_episode_number(ranges, mal_episode)
        if episode is not None:
            return tmdb_id, int(season.removeprefix("s")), episode
    return None
