"""Priority chains for metadata resolution.

Anime  : Kitsu > TVDB > TMDB > Cinemeta
Movies : TMDB > Cinemeta
Series : TVDB > Cinemeta > TMDB
"""
from __future__ import annotations

from typing import Optional

from Backend.helper.metadata.common import split_default_id, title_similarity, CINEMETA_THRESHOLD
from Backend.helper.metadata.providers import anilist, cinemeta, kitsu, simkl, tmdb, tvdb
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


# ── Movies: TMDB > Cinemeta ──────────────────────────────────────────────────

async def resolve_movie(
    title: str,
    encoded_string,
    year=None,
    quality=None,
    default_id=None,
) -> Optional[dict]:
    imdb_id, tmdb_id, explicit_imdb, force_tmdb = split_default_id(default_id)

    # Explicit TMDB id
    if tmdb_id and force_tmdb:
        movie = await tmdb.details("movie", tmdb_id)
        if movie:
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    # Explicit IMDb id → Cinemeta
    if imdb_id and explicit_imdb:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                return cinemeta.build_movie_payload(detail, imdb_id, title, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"Cinemeta explicit movie fetch failed [{imdb_id}]: {e}")

    # 1) TMDB first
    if not tmdb_id:
        hit = await tmdb.safe_search(title, "movie", year)
        if hit:
            tmdb_id = hit.id
    if tmdb_id:
        movie = await tmdb.details("movie", tmdb_id)
        if movie:
            LOGGER.info(f"[MOVIE] TMDB hit for '{title}' (year={year})")
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    # 2) Cinemeta fallback
    LOGGER.info(f"[MOVIE] TMDB miss for '{title}' -> Cinemeta")
    if not imdb_id:
        imdb_id = await cinemeta.safe_search(title, "movie", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                sim = title_similarity(title, detail.get("title", ""))
                if sim >= CINEMETA_THRESHOLD or explicit_imdb:
                    return cinemeta.build_movie_payload(detail, imdb_id, title, quality, encoded_string)
                LOGGER.info(
                    f"[MOVIE] Cinemeta title mismatch for '{title}': "
                    f"got '{detail.get('title')}' (sim={sim:.2f})"
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta movie fetch failed [{title}]: {e}")

    # 3) Simkl fallback (optional, requires Simkl Client-ID in Settings)
    settings = SettingsManager.current()
    if getattr(settings, "simkl_enabled", False) and getattr(settings, "simkl_client_id", ""):
        try:
            simkl_hit = await simkl.search_title(title, "movie", settings.simkl_client_id)
            if simkl_hit and simkl_hit.get("tmdb_id"):
                tmdb_id_simkl = int(simkl_hit["tmdb_id"])
                movie = await tmdb.details("movie", tmdb_id_simkl)
                if movie:
                    LOGGER.info(f"[MOVIE] Simkl hit for '{title}' via TMDB {tmdb_id_simkl}")
                    return tmdb.build_movie_payload(movie, quality, encoded_string)
            elif simkl_hit and simkl_hit.get("imdb_id"):
                detail = await cinemeta.cached_detail(simkl_hit["imdb_id"], "movie")
                if detail:
                    LOGGER.info(f"[MOVIE] Simkl hit for '{title}' via IMDb {simkl_hit['imdb_id']}")
                    return cinemeta.build_movie_payload(detail, simkl_hit["imdb_id"], title, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"[MOVIE] Simkl fallback failed for '{title}': {e}")

    LOGGER.info(f"[MOVIE] No metadata for '{title}' (year={year})")
    return None


# ── Series: TVDB > Cinemeta > TMDB ────────────────────────────────────────────

async def resolve_series(
    title: str,
    season: int,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    default_id=None,
) -> Optional[dict]:
    imdb_id, tmdb_id, explicit_imdb, force_tmdb = split_default_id(default_id)

    # Explicit overrides skip the chain
    if tmdb_id and force_tmdb:
        tv = await tmdb.details("tv", tmdb_id)
        if tv:
            ep = await tmdb.episode_details(tmdb_id, season, episode)
            return tmdb.build_tv_payload(tv, ep, season, episode, quality, encoded_string)

    if imdb_id and explicit_imdb:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = await cinemeta.cached_season(imdb_id, season, episode)
            if detail:
                return cinemeta.build_tv_payload(
                    detail, ep or {}, imdb_id, title, season, episode, quality, encoded_string
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta explicit TV fetch failed [{imdb_id}]: {e}")

    # 1) TVDB
    try:
        result = await tvdb.fetch_series_metadata(
            title, season, episode, encoded_string, year=year, quality=quality
        )
        if result:
            LOGGER.info(f"[SERIES] TVDB hit for '{title}' S{season:02d}E{episode:02d}")
            return result
    except Exception as e:
        LOGGER.warning(f"[SERIES] TVDB error for '{title}': {e}")

    # 2) Cinemeta
    LOGGER.info(f"[SERIES] TVDB miss for '{title}' -> Cinemeta")
    if not imdb_id:
        imdb_id = await cinemeta.safe_search(title, "tvSeries", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = await cinemeta.cached_season(imdb_id, season, episode)
            if detail:
                sim = title_similarity(title, detail.get("title", ""))
                if sim >= CINEMETA_THRESHOLD or explicit_imdb:
                    return cinemeta.build_tv_payload(
                        detail, ep or {}, imdb_id, title, season, episode, quality, encoded_string
                    )
                LOGGER.info(
                    f"[SERIES] Cinemeta title mismatch for '{title}': "
                    f"got '{detail.get('title')}' (sim={sim:.2f})"
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta TV fetch failed [{title}]: {e}")

    # 3) TMDB
    LOGGER.info(f"[SERIES] Cinemeta miss for '{title}' -> TMDB")
    if not tmdb_id:
        hit = await tmdb.safe_search(title, "tv", year)
        if hit:
            tmdb_id = hit.id
    if tmdb_id:
        tv = await tmdb.details("tv", tmdb_id)
        if tv:
            ep = await tmdb.episode_details(tmdb_id, season, episode)
            return tmdb.build_tv_payload(tv, ep, season, episode, quality, encoded_string)

    # 4) Simkl fallback (optional, requires Simkl Client-ID in Settings)
    settings = SettingsManager.current()
    if getattr(settings, "simkl_enabled", False) and getattr(settings, "simkl_client_id", ""):
        try:
            simkl_hit = await simkl.search_title(title, "tv", settings.simkl_client_id)
            if simkl_hit and simkl_hit.get("tmdb_id"):
                tmdb_id_simkl = int(simkl_hit["tmdb_id"])
                tv = await tmdb.details("tv", tmdb_id_simkl)
                if tv:
                    ep = await tmdb.episode_details(tmdb_id_simkl, season, episode)
                    LOGGER.info(f"[SERIES] Simkl hit for '{title}' via TMDB {tmdb_id_simkl}")
                    return tmdb.build_tv_payload(tv, ep, season, episode, quality, encoded_string)
            elif simkl_hit and simkl_hit.get("imdb_id"):
                detail = await cinemeta.cached_detail(simkl_hit["imdb_id"], "tvSeries")
                ep = await cinemeta.cached_season(simkl_hit["imdb_id"], season, episode)
                if detail:
                    LOGGER.info(f"[SERIES] Simkl hit for '{title}' via IMDb {simkl_hit['imdb_id']}")
                    return cinemeta.build_tv_payload(detail, ep or {}, simkl_hit["imdb_id"], title, season, episode, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"[SERIES] Simkl fallback failed for '{title}': {e}")

    LOGGER.info(f"[SERIES] No metadata for '{title}' S{season:02d}E{episode:02d}")
    return None


# ── Anime: Kitsu > TVDB > TMDB > Cinemeta ─────────────────────────────────────

async def resolve_anime_tv(
    title: str,
    season,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
) -> Optional[dict]:
    """Resolve anime episode metadata.

    absolute=True (or season is None): orphan/absolute numbering
    e.g. "One Piece 1223 720.mkv" → Kitsu absolute episode 1223.
    """
    absolute = bool(absolute or season is None)
    label = f"E{episode}" if absolute else f"S{int(season):02d}E{int(episode):02d}"
    # Resolve the downstream episode pair before optional providers use it.
    use_season = 1 if absolute else int(season)
    use_episode = int(episode)

    # 1) Kitsu (native absolute-episode support via ani.zip)
    try:
        result = await kitsu.fetch_anime_tv(
            title, season, episode, encoded_string,
            year=year, quality=quality, absolute=absolute,
        )
        if result:
            LOGGER.info(f"[ANIME] Kitsu hit for '{title}' {label}")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] Kitsu error for '{title}': {e}")

    # 1.5) AniList (anime-focused, no API key or login required)
    if getattr(SettingsManager.current(), "anilist_enabled", True):
        try:
            anilist_hit = await anilist.search_title(title, "tv")
            if anilist_hit and anilist_hit.get("anilist_id"):
                detail = await anilist.details(int(anilist_hit["anilist_id"]))
                if detail:
                    LOGGER.info(f"[ANIME] AniList hit for '{title}' id={anilist_hit['anilist_id']}")
                    # Build payload from AniList data
                    return {
                        "title": detail.get("title") or title,
                        "year": detail.get("year") or year,
                        "media_type": "tv",
                        "poster": detail.get("poster") or "",
                        "backdrop": detail.get("backdrop") or "",
                        "overview": detail.get("overview") or "",
                        "rating": detail.get("rating") or 0,
                        "genres": detail.get("genres") or [],
                        "runtime": detail.get("runtime") or "",
                        "imdb_id": detail.get("imdb_id") or "",
                        "tmdb_id": "",
                        "anilist_id": detail.get("anilist_id"),
                        "season_number": use_season,
                        "episode_number": use_episode,
                        "absolute_episode": use_episode if absolute else None,
                        "episode_title": f"Episode {use_episode}",
                        "quality": quality or "HD",
                        "encoded_string": encoded_string,
                    }
        except Exception as e:
            LOGGER.warning(f"[ANIME] AniList error for '{title}': {e}")

    # For absolute episodes without a mapped season, use season 1 + absolute number
    # so downstream providers and Stremio still get a valid S/E pair.

    # 2) TVDB
    try:
        result = await tvdb.fetch_series_metadata(
            title, use_season, use_episode, encoded_string, year=year, quality=quality
        )
        if result:
            if absolute:
                result["season_number"] = result.get("season_number") or use_season
                result["episode_number"] = use_episode
                result["absolute_episode"] = use_episode
            LOGGER.info(f"[ANIME] TVDB hit for '{title}' {label}")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] TVDB error for '{title}': {e}")

    # 3) TMDB
    hit = await tmdb.safe_search(title, "tv", year)
    if hit:
        tv = await tmdb.details("tv", hit.id)
        if tv:
            ep = None if absolute else await tmdb.episode_details(hit.id, use_season, use_episode)
            LOGGER.info(f"[ANIME] TMDB hit for '{title}' {label}")
            payload = tmdb.build_tv_payload(tv, ep, use_season, use_episode, quality, encoded_string)
            if absolute:
                payload["absolute_episode"] = use_episode
                if not payload.get("episode_title") or payload["episode_title"].startswith("S"):
                    payload["episode_title"] = f"Episode {use_episode}"
            return payload

    # 4) Cinemeta (least priority)
    imdb_id = await cinemeta.safe_search(title, "tvSeries", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = {} if absolute else await cinemeta.cached_season(imdb_id, use_season, use_episode)
            if detail:
                LOGGER.info(f"[ANIME] Cinemeta hit for '{title}' {label}")
                payload = cinemeta.build_tv_payload(
                    detail, ep or {}, imdb_id, title, use_season, use_episode, quality, encoded_string
                )
                if absolute:
                    payload["absolute_episode"] = use_episode
                    if not (ep or {}).get("title"):
                        payload["episode_title"] = f"Episode {use_episode}"
                return payload
        except Exception as e:
            LOGGER.warning(f"[ANIME] Cinemeta error for '{title}': {e}")

    # 5) Simkl fallback (optional, requires Simkl Client-ID in Settings)
    settings = SettingsManager.current()
    if getattr(settings, "simkl_enabled", False) and getattr(settings, "simkl_client_id", ""):
        try:
            simkl_hit = await simkl.search_title(title, "tv", settings.simkl_client_id)
            if simkl_hit and simkl_hit.get("tmdb_id"):
                tv = await tmdb.details("tv", int(simkl_hit["tmdb_id"]))
                if tv:
                    ep = await tmdb.episode_details(int(simkl_hit["tmdb_id"]), use_season, use_episode)
                    LOGGER.info(f"[ANIME] Simkl hit for '{title}' via TMDB {simkl_hit['tmdb_id']}")
                    payload = tmdb.build_tv_payload(tv, ep, use_season, use_episode, quality, encoded_string)
                    if absolute:
                        payload["absolute_episode"] = use_episode
                    return payload
        except Exception as e:
            LOGGER.warning(f"[ANIME] Simkl fallback failed for '{title}': {e}")

    LOGGER.info(f"[ANIME] No metadata for '{title}' {label}")
    return None


async def resolve_anime_movie(
    title: str,
    encoded_string,
    year=None,
    quality=None,
) -> Optional[dict]:
    # 1) Kitsu
    try:
        result = await kitsu.fetch_anime_movie(title, encoded_string, year=year, quality=quality)
        if result:
            LOGGER.info(f"[ANIME] Kitsu movie hit for '{title}'")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] Kitsu movie error for '{title}': {e}")

    # 1.5) AniList (anime-focused, no API key or login required)
    if getattr(SettingsManager.current(), "anilist_enabled", True):
        try:
            anilist_hit = await anilist.search_title(title, "movie")
            if anilist_hit and anilist_hit.get("anilist_id"):
                detail = await anilist.details(int(anilist_hit["anilist_id"]))
                if detail:
                    LOGGER.info(f"[ANIME] AniList movie hit for '{title}' id={anilist_hit['anilist_id']}")
                    return {
                        "title": detail.get("title") or title,
                        "year": detail.get("year") or year,
                        "media_type": "movie",
                        "poster": detail.get("poster") or "",
                        "backdrop": detail.get("backdrop") or "",
                        "overview": detail.get("overview") or "",
                        "rating": detail.get("rating") or 0,
                        "genres": detail.get("genres") or [],
                        "runtime": detail.get("runtime") or "",
                        "imdb_id": detail.get("imdb_id") or "",
                        "tmdb_id": "",
                        "anilist_id": detail.get("anilist_id"),
                        "quality": quality or "HD",
                        "encoded_string": encoded_string,
                    }
        except Exception as e:
            LOGGER.warning(f"[ANIME] AniList movie error for '{title}': {e}")

    # 2) TVDB
    try:
        result = await tvdb.fetch_movie_metadata(title, encoded_string, year=year, quality=quality)
        if result:
            LOGGER.info(f"[ANIME] TVDB movie hit for '{title}'")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] TVDB movie error for '{title}': {e}")

    # 3) TMDB
    hit = await tmdb.safe_search(title, "movie", year)
    if hit:
        movie = await tmdb.details("movie", hit.id)
        if movie:
            LOGGER.info(f"[ANIME] TMDB movie hit for '{title}'")
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    # 4) Cinemeta
    imdb_id = await cinemeta.safe_search(title, "movie", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                LOGGER.info(f"[ANIME] Cinemeta movie hit for '{title}'")
                return cinemeta.build_movie_payload(detail, imdb_id, title, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"[ANIME] Cinemeta movie error for '{title}': {e}")

    # 5) Simkl fallback (optional, requires Simkl Client-ID in Settings)
    settings = SettingsManager.current()
    if getattr(settings, "simkl_enabled", False) and getattr(settings, "simkl_client_id", ""):
        try:
            simkl_hit = await simkl.search_title(title, "movie", settings.simkl_client_id)
            if simkl_hit and simkl_hit.get("tmdb_id"):
                movie = await tmdb.details("movie", int(simkl_hit["tmdb_id"]))
                if movie:
                    LOGGER.info(f"[ANIME] Simkl movie hit for '{title}' via TMDB {simkl_hit['tmdb_id']}")
                    return tmdb.build_movie_payload(movie, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"[ANIME] Simkl movie fallback failed for '{title}': {e}")

    LOGGER.info(f"[ANIME] No movie metadata for '{title}'")
    return None
