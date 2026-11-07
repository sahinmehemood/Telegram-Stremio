"""Simkl metadata provider (REST API). Falls back silently when not configured."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from Backend.helper.metadata.common import cached_call, KITSU_CACHE
from Backend.logger import LOGGER

BASE_URL = "https://api.simkl.com"
APP_NAME = "telegram-stremio"
APP_VERSION = "5.3.0"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Simkl rate limit: 100 requests per minute. Keep a small local cache so repeated
# lookups for the same title don't hammer the API.
_SIMLK_CACHE = KITSU_CACHE  # reuse the bounded dict instance


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        return _client


def _headers(client_id: str) -> dict:
    return {
        "Accept": "application/json",
        "Client-Id": client_id,
        "User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)",
    }


async def _fetch_json(url: str, client_id: str) -> Optional[Dict[str, Any]]:
    try:
        client = await _get_client()
        resp = await client.get(url, headers=_headers(client_id))
        if resp.status_code in (401, 403):
            LOGGER.warning(f"[Simkl] Unauthorized — check Simkl Client-ID")
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:
        LOGGER.warning(f"[Simkl] Request failed: {exc}")
        return None


def _enabled(settings) -> bool:
    return bool(getattr(settings, "simkl_enabled", False)) and bool(getattr(settings, "simkl_client_id", ""))


async def search_title(query: str, media_type: str, client_id: str) -> Optional[Dict[str, Any]]:
    """Search Simkl for a title. media_type: 'movie' or 'tv'."""
    if not _enabled(type("S", (), {"simkl_enabled": bool(client_id), "simkl_client_id": client_id})()):
        return None
    cache_key = f"simkl:search:{media_type}:{query.lower()}"
    cached = _SIMLK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    simkl_type = "movies" if media_type == "movie" else "tv"
    url = f"{BASE_URL}/search/{simkl_type}/{query}?client-id={client_id}"
    data = await _fetch_json(url, client_id)
    if not data:
        _SIMLK_CACHE[cache_key] = None
        return None

    results = data.get("results", [])
    if not results:
        _SIMLK_CACHE[cache_key] = None
        return None

    hit = results[0]
    simkl_id = hit.get("ids", {}).get("simkl")
    result: Dict[str, Any] = {
        "simkl_id": simkl_id,
        "title": hit.get("title") or hit.get("title_english") or "",
        "year": hit.get("year"),
        "media_type": media_type,
        "poster": hit.get("poster") or hit.get("poster_path") or "",
        "overview": hit.get("overview") or "",
        "rating": hit.get("rating") or 0,
    }
    _SIMLK_CACHE[cache_key] = result
    return result


async def details(simkl_id: str, media_type: str, client_id: str) -> Optional[Dict[str, Any]]:
    """Fetch full details for a Simkl item."""
    if not client_id:
        return None
    simkl_type = "movies" if media_type == "movie" else "tv"
    cache_key = f"simkl:detail:{media_type}:{simkl_id}"
    cached = _SIMLK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/{simkl_id}/{simkl_type}?client-id={client_id}"
    data = await _fetch_json(url, client_id)
    if not data:
        _SIMLK_CACHE[cache_key] = None
        return None

    item = data.get(simkl_type[:-1], data)  # "movie" or "tv"
    if not item:
        _SIMLK_CACHE[cache_key] = None
        return None

    result: Dict[str, Any] = {
        "simkl_id": item.get("ids", {}).get("simkl", simkl_id),
        "imdb_id": item.get("ids", {}).get("imdb", ""),
        "tmdb_id": item.get("ids", {}).get("tmdb", ""),
        "title": item.get("title") or item.get("title_english") or "",
        "year": item.get("year"),
        "media_type": media_type,
        "poster": item.get("poster") or item.get("poster_path") or "",
        "backdrop": item.get("backdrop") or item.get("backdrop_path") or "",
        "overview": item.get("overview") or "",
        "rating": item.get("rating") or 0,
        "genres": item.get("genres") or [],
        "runtime": item.get("runtime") or "",
    }
    _SIMLK_CACHE[cache_key] = result
    return result


async def sync_watchlist(client_id: str, client_secret: str) -> Optional[Dict[str, Any]]:
    """Fetch the authenticated user's Simkl watchlist (requires OAuth token flow handled elsewhere)."""
    if not client_id or not client_secret:
        return None
    # Simkl watchlist requires an access token via OAuth. This endpoint is a
    # placeholder for future OAuth integration; returns None gracefully.
    return None


async def request_pin(client_id: str) -> Optional[Dict[str, Any]]:
    """Start Simkl's documented AUTH V1 PIN flow without logging secrets."""
    if not client_id:
        return None
    try:
        client = await _get_client()
        resp = await client.get(
            f"{BASE_URL}/oauth/pin",
            params={"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION},
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        data = resp.json()
        if resp.status_code != 200:
            return {"error": data.get("error") or data.get("message") or f"HTTP {resp.status_code}"}
        return data
    except Exception as exc:
        LOGGER.warning(f"[Simkl] PIN request failed: {exc}")
        return {"error": "Simkl is unreachable"}


async def poll_pin(client_id: str, user_code: str) -> Optional[Dict[str, Any]]:
    """Poll the PIN flow; the access token is returned only to the caller."""
    if not client_id or not user_code:
        return None
    try:
        client = await _get_client()
        resp = await client.get(
            f"{BASE_URL}/oauth/pin/{user_code}",
            params={"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION},
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        return resp.json()
    except Exception as exc:
        LOGGER.warning(f"[Simkl] PIN poll failed: {exc}")
        return {"result": "KO", "message": "Simkl is unreachable"}


async def user_settings(client_id: str, access_token: str) -> Optional[Dict[str, Any]]:
    """Return the authenticated Simkl profile, if the stored token is valid."""
    if not client_id or not access_token:
        return None
    try:
        client = await _get_client()
        resp = await client.get(
            f"{BASE_URL}/users/settings",
            params={"client_id": client_id, "app-name": APP_NAME, "app-version": APP_VERSION},
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:
        LOGGER.warning(f"[Simkl] Profile check failed: {exc}")
        return None
