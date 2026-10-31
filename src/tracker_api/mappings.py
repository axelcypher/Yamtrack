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
