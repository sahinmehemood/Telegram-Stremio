"""AniList metadata provider (GraphQL API). No API key required; falls back silently."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from Backend.helper.metadata.common import cached_call, KITSU_CACHE
from Backend.logger import LOGGER

GRAPHQL_URL = "https://graphql.anilist.co"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_ANILIST_CACHE = KITSU_CACHE


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return _client


def _enabled() -> bool:
    # AniList is open and requires no credentials. Always enabled unless explicitly
    # disabled via settings (checked by caller).
    return True


async def _graphql(query: str, variables: dict) -> Optional[Dict[str, Any]]:
    try:
        client = await _get_client()
        resp = await client.post(GRAPHQL_URL, json={"query": query, "variables": variables})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("errors"):
            LOGGER.warning(f"[AniList] GraphQL errors: {data['errors']}")
            return None
        return data.get("data", {})
    except Exception as exc:
        LOGGER.warning(f"[AniList] Request failed: {exc}")
        return None


_SEARCH_QUERY = """
query ($search: String, $type: MediaType) {
  Page(perPage: 5) {
    media(search: $search, type: $type) {
      id
      idMal
      title { romaji english native userPreferred }
      type format status episodes duration
      startDate { year month day }
      endDate { year month day }
      description
      genres
      synonyms
      averageScore
      popularity
      coverImage { large medium }
      bannerImage
      studios { nodes { name } }
      externalLinks { site url }
    }
  }
}
"""


_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    idMal
    title { romaji english native userPreferred }
    type format status episodes duration
    startDate { year month day }
    endDate { year month day }
    description
    genres
    synonyms
    averageScore
    popularity
    coverImage { large medium }
    bannerImage
    studios { nodes { name } }
    externalLinks { site url }
    season seasonYear
  }
}
"""


def _extract_imdb(external_links: list) -> str:
    for link in (external_links or []):
        if link.get("site") == "IMDb":
            url = link.get("url", "")
            # Extract ttxxxxxxx from https://www.imdb.com/title/tt1234567/
            import re
            m = re.search(r"(tt\d+)", url)
            if m:
                return m.group(1)
    return ""


def _best_title(titles: dict) -> str:
    return titles.get("english") or titles.get("romaji") or titles.get("native") or ""


def _media_type_str(item: dict) -> str:
    # AniList returns ANIME or MANGA. We only care about anime.
    return "tv" if item.get("type") == "ANIME" else "movie"


async def search_title(query: str, media_type: str) -> Optional[Dict[str, Any]]:
    """Search AniList for an anime title. media_type: 'movie' or 'tv'."""
    cache_key = f"anilist:search:{media_type}:{query.lower()}"
    cached = _ANILIST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # AniList only covers anime. For movie searches, still try — anime movies exist.
    data = await _graphql(_SEARCH_QUERY, {"search": query, "type": "ANIME"})
    if not data:
        _ANILIST_CACHE[cache_key] = None
        return None

    media_list = (data or {}).get("Page", {}).get("media", [])
    if not media_list:
        _ANILIST_CACHE[cache_key] = None
        return None

    item = media_list[0]
    start_date = item.get("startDate") or {}
    year = start_date.get("year")
    cover = item.get("coverImage") or {}
    imdb_id = _extract_imdb(item.get("externalLinks") or [])

    result: Dict[str, Any] = {
        "anilist_id": item.get("id"),
        "mal_id": item.get("idMal"),
        "imdb_id": imdb_id,
        "title": _best_title(item.get("title") or {}),
        "year": year,
        "media_type": "tv",  # AniList anime is series-oriented
        "poster": cover.get("large") or cover.get("medium") or "",
        "backdrop": item.get("bannerImage") or "",
        "overview": item.get("description") or "",
        "rating": item.get("averageScore") or 0,
        "genres": item.get("genres") or [],
        "runtime": str(item.get("duration") or "") + " min" if item.get("duration") else "",
        "episodes": item.get("episodes"),
        "status": item.get("status") or "",
    }
    _ANILIST_CACHE[cache_key] = result
    return result


async def details(anilist_id: int) -> Optional[Dict[str, Any]]:
    """Fetch full details for an AniList item."""
    cache_key = f"anilist:detail:{anilist_id}"
    cached = _ANILIST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    data = await _graphql(_MEDIA_QUERY, {"id": anilist_id})
    if not data:
        _ANILIST_CACHE[cache_key] = None
        return None

    item = data.get("Media")
    if not item:
        _ANILIST_CACHE[cache_key] = None
        return None

    start_date = item.get("startDate") or {}
    year = start_date.get("year")
    cover = item.get("coverImage") or {}
    imdb_id = _extract_imdb(item.get("externalLinks") or [])

    result: Dict[str, Any] = {
        "anilist_id": item.get("id"),
        "mal_id": item.get("idMal"),
        "imdb_id": imdb_id,
        "title": _best_title(item.get("title") or {}),
        "year": year,
        "media_type": "tv",
        "poster": cover.get("large") or cover.get("medium") or "",
        "backdrop": item.get("bannerImage") or "",
        "overview": item.get("description") or "",
        "rating": item.get("averageScore") or 0,
        "genres": item.get("genres") or [],
        "runtime": str(item.get("duration") or "") + " min" if item.get("duration") else "",
        "episodes": item.get("episodes"),
        "status": item.get("status") or "",
        "season": item.get("season") or "",
        "season_year": (start_date or {}).get("year"),
    }
    _ANILIST_CACHE[cache_key] = result
    return result
