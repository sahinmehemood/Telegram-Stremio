#!/usr/bin/env python3
"""Termux-friendly worker for announcing new Telegram media.

This is a standalone companion to the TeleStremio Android APK. It listens for
new channel posts through a Telegram user session; it never scans old history.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import html
import ipaddress
import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

try:
    import httpx
except ImportError:
    httpx = None

try:
    from guessit import guessit
except ImportError:
    guessit = None

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.handlers import DeletedMessagesHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


APP_DIR = Path(__file__).resolve().parent
CINEMETA_BASE = "https://v3-cinemeta.strem.io"
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".wmv",
}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".001", ".part"}
QUALITY_RE = re.compile(r"(?i)(2160\s*p?|4k|1440p|1080p|720p|576p|480p|360p)")
IMDB_RE = re.compile(r"(?i)\btt\d{7,10}\b")
SEASON_EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[\s._-]*E(\d{1,3})\b")
X_EPISODE_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")
EPISODE_ONLY_RE = re.compile(r"(?i)\bE(?:P)?[\s._-]?(\d{1,4})\b")
RELEASE_TAG_RE = re.compile(
    r"(?i)\b(?:2160p?|4k|1440p|1080p|720p|576p|480p|360p|"
    r"web[- .]?dl|web[- .]?rip|webrip|bluray|blu-ray|brrip|bdrip|"
    r"hdtv|hdr10?\+?|dolby.?vision|hevc|x265|h265|x264|h264|av1|"
    r"aac|ddp?\d(?:\.\d)?|dts(?:-hd)?|proper|repack|remux|"
    r"10bit|8bit|multi|dual.audio|eng.sub|subs?)\b.*$"
)


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs without overriding variables already in the process."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def _parse_chat(value: str) -> int | str:
    value = value.strip()
    if not value:
        return value
    try:
        return int(value)
    except ValueError:
        return "@" + value.lstrip("@")


def _parse_source_channels(value: str) -> tuple[set[int], set[str]]:
    ids: set[int] = set()
    usernames: set[str] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            usernames.add(item.lstrip("@").casefold())
    return ids, usernames


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().casefold()
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    source_ids: set[int]
    source_usernames: set[str]
    announcement_channel: int | str
    session_name: str
    state_db: Path
    announce_each_episode: bool
    public_base_url: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        values = {
            "API_ID": os.getenv("API_ID", "").strip(),
            "API_HASH": os.getenv("API_HASH", "").strip(),
            "SOURCE_CHANNELS": os.getenv("SOURCE_CHANNELS", "").strip(),
            "ANNOUNCEMENT_CHANNEL": os.getenv("ANNOUNCEMENT_CHANNEL", "").strip(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError("Missing required setting(s): " + ", ".join(missing))
        try:
            api_id = int(values["API_ID"])
        except ValueError as exc:
            raise ValueError("API_ID must be an integer from https://my.telegram.org/apps.") from exc

        source_ids, source_usernames = _parse_source_channels(values["SOURCE_CHANNELS"])
        if not source_ids and not source_usernames:
            raise ValueError("SOURCE_CHANNELS must contain at least one ID or @username.")

        state_path = Path(os.getenv("STATE_DB", "announcements.sqlite3").strip()).expanduser()
        if not state_path.is_absolute():
            state_path = APP_DIR / state_path

        return cls(
            api_id=api_id,
            api_hash=values["API_HASH"],
            source_ids=source_ids,
            source_usernames=source_usernames,
            announcement_channel=_parse_chat(values["ANNOUNCEMENT_CHANNEL"]),
            session_name=os.getenv("SESSION_NAME", "telestremio_announcer").strip() or "telestremio_announcer",
            state_db=state_path,
            announce_each_episode=_env_bool("ANNOUNCE_EACH_EPISODE", False),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        )


@dataclass
class MediaInfo:
    title: str
    media_type: str  # "movie" or "series"
    year: str = ""
    season: int | None = None
    episode: int | None = None
    quality: str = ""
    description: str = ""
    rating: str = ""
    genres: list[str] | None = None
    imdb_id: str = ""
    poster: str = ""
    backdrop: str = ""
    tmdb_id: str = ""
    tmdb_url: str = ""

    def __post_init__(self) -> None:
        if self.genres is None:
            self.genres = []

    @property
    def photo_url(self) -> str:
        return self.poster or self.backdrop


class AnnouncementStore:
    """SQLite state for deduplication and source-to-announcement deletion mapping."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS announced (
                    key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    source_chat_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    sent_chat_id TEXT NOT NULL,
                    sent_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Migrate the earlier companion schema without discarding dedupe state.
            columns = {row[1] for row in db.execute("PRAGMA table_info(announced)")}
            if "sent_chat_id" not in columns:
                db.execute(
                    "ALTER TABLE announced ADD COLUMN sent_chat_id TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=20000")
        return db

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def has(self, key: str) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM announced WHERE key = ?", (key,)
            ).fetchone() is not None

    def add(
        self,
        key: str,
        info: MediaInfo,
        source_chat_id: str | int,
        source_message_id: int,
        sent_chat_id: str | int,
        sent_message_id: int,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO announced
                (key, title, media_type, source_chat_id, source_message_id,
                 sent_chat_id, sent_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, info.title, info.media_type, str(source_chat_id),
                    int(source_message_id), str(sent_chat_id), int(sent_message_id),
                    created_at,
                ),
            )

    def find_source_message(self, chat_id: str | int, message_id: int) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """
                SELECT key, sent_chat_id, sent_message_id
                FROM announced
                WHERE source_chat_id = ? AND source_message_id = ?
                """,
                (str(chat_id), int(message_id)),
            ).fetchall()
        return [
            {"key": row[0], "sent_chat_id": row[1], "sent_message_id": row[2]}
            for row in rows
        ]

    def remove(self, key: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM announced WHERE key = ?", (key,))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _message_file_name(message: Any) -> str:
    for attr in ("video", "document", "animation"):
        obj = getattr(message, attr, None)
        if obj:
            name = getattr(obj, "file_name", None)
            if name:
                return str(name)
    return ""


def _is_media_message(message: Any, file_name: str) -> bool:
    if getattr(message, "video", None):
        return True
    document = getattr(message, "document", None)
    if not document:
        return False
    mime = str(getattr(document, "mime_type", "") or "").casefold()
    if mime.startswith("video/"):
        return True
    name = (file_name or "").casefold()
    if any(name.endswith(ext) for ext in VIDEO_EXTENSIONS):
        return True
    if any(name.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        return True
    if re.search(r"\.(?:mkv|mp4|m4v|avi|mov|webm|mpg|mpeg|ts|m2ts|wmv)\.\d{3}$", name):
        return True
    return False


def _quality_from_name(text: str) -> str:
    match = QUALITY_RE.search(text or "")
    if not match:
        return ""
    value = match.group(0).lower().replace(" ", "")
    if value == "4k" or value.startswith("2160"):
        return "2160p / 4K"
    return value


def _fallback_title(source: str) -> str:
    """Strip common release tokens while preserving non-Latin title characters."""
    name = (source or "").strip()
    if not name:
        return ""
    # Remove known media/archive extensions and common multipart suffixes.
    name = re.sub(
        r"(?i)\.(?:mkv|mp4|m4v|avi|mov|webm|mpg|mpeg|ts|m2ts|wmv|zip|rar|7z)(?:\.\d{3})?$",
        "",
        name,
    )
    name = re.sub(r"(?i)\.part\d*(?:\.rar)?$", "", name)
    name = IMDB_RE.sub(" ", name)
    name = SEASON_EPISODE_RE.sub(" ", name)
    name = X_EPISODE_RE.sub(" ", name)
    name = EPISODE_ONLY_RE.sub(" ", name)
    # In common release naming, the year and quality/release tags end the title.
    name = re.sub(r"\b(?:19|20)\d{2}\b.*$", " ", name)
    name = RELEASE_TAG_RE.sub(" ", name)
    # Remove bracketed technical tags, then convert filename separators to spaces.
    name = re.sub(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*(?:WEB|BluRay|HDR|HEVC|x26)[^)]*\)", " ", name, flags=re.I)
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"(?i)\b(?:proper|repack|extended|unrated|remastered|directors cut)\b", " ", name)
    name = re.sub(r"\s+-\s+.*$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.[](){}")
    return name


def parse_media(file_name: str, caption: str = "") -> MediaInfo | None:
    """Parse a video-ish filename/caption, with guessit and a Unicode-safe fallback."""
    source = (file_name or caption or "").strip()
    if not source:
        return None

    parsed: dict[str, Any] = {}
    if guessit is not None:
        try:
            parsed = dict(guessit(source))
        except Exception:
            parsed = {}

    se_match = SEASON_EPISODE_RE.search(source)
    x_match = X_EPISODE_RE.search(source)
    ep_match = EPISODE_ONLY_RE.search(source)
    season = _safe_int(parsed.get("season"))
    episode = _safe_int(parsed.get("episode"))
    if se_match:
        season = season if season is not None else int(se_match.group(1))
        episode = episode if episode is not None else int(se_match.group(2))
    elif x_match:
        season = season if season is not None else int(x_match.group(1))
        episode = episode if episode is not None else int(x_match.group(2))
    elif episode is None and ep_match:
        episode = int(ep_match.group(1))

    guessed_type = str(parsed.get("type") or "").casefold()
    media_type = "series" if guessed_type in {"episode", "tv", "series"} or season is not None or episode is not None else "movie"

    title = str(parsed.get("title") or "").strip()
    if not title or not _normalize_title(_fallback_title(title)):
        title = _fallback_title(source)
    # Do not let a parser return a quality tag as if it were a title.
    if not _normalize_title(title) or not _normalize_title(_fallback_title(title)):
        title = _fallback_title(source)
    if not title or not _normalize_title(title):
        logging.warning("Skipped media because its filename/caption contains no usable title: %r", source)
        return None

    year = str(parsed.get("year") or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", year):
        year_match = re.search(r"\b((?:19|20)\d{2})\b", source)
        year = year_match.group(1) if year_match else ""

    quality = _quality_from_name((file_name or "") + " " + (caption or ""))
    imdb_match = IMDB_RE.search(file_name or "") or IMDB_RE.search(caption or "")
    return MediaInfo(
        title=title,
        media_type=media_type,
        year=year,
        season=season,
        episode=episode,
        quality=quality,
        imdb_id=imdb_match.group(0).lower() if imdb_match else "",
    )


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(char for char in normalized if char.isalnum())


def _metadata_match_score(wanted_title: str, wanted_year: str, candidate: dict[str, Any]) -> float:
    candidate_title = str(candidate.get("name") or candidate.get("title") or "")
    score = SequenceMatcher(
        None, _normalize_title(wanted_title), _normalize_title(candidate_title)
    ).ratio()
    candidate_year = str(candidate.get("year") or "")[:4]
    if wanted_year and candidate_year:
        score += 0.12 if wanted_year == candidate_year else -0.12
    return max(0.0, min(score, 1.0))


class Cinemeta:
    """Public Stremio/Cinemeta lookup. Results are cached for this process lifetime."""

    def __init__(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx is missing; run python -m pip install -r requirements.txt")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": "TeleStremio-Mobile-Announcements/1.0"},
        )
        self._cache: dict[str, dict[str, Any] | None] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_meta(self, media_type: str, imdb_id: str) -> dict[str, Any] | None:
        url = f"{CINEMETA_BASE}/meta/{media_type}/{quote(imdb_id, safe='')}.json"
        try:
            response = await self.client.get(url)
            if response.status_code != 200:
                return None
            data = response.json()
            meta = data.get("meta") if isinstance(data, dict) else None
            return meta if isinstance(meta, dict) else None
        except Exception as exc:
            logging.warning("Cinemeta metadata lookup failed: %s", exc)
            return None

    async def _search(self, media_type: str, title: str) -> list[dict[str, Any]]:
        if not title:
            return []
        url = f"{CINEMETA_BASE}/catalog/{media_type}/top/search={quote(title, safe='')}.json"
        try:
            response = await self.client.get(url)
            if response.status_code != 200:
                return []
            data = response.json()
            metas = data.get("metas", []) if isinstance(data, dict) else []
            return [item for item in metas if isinstance(item, dict)]
        except Exception as exc:
            logging.warning("Cinemeta search failed: %s", exc)
            return []

    async def enrich(self, info: MediaInfo) -> MediaInfo:
        cache_key = f"{info.media_type}:{info.imdb_id or _normalize_title(info.title)}:{info.year or 'na'}"
        if cache_key in self._cache:
            meta = self._cache[cache_key]
        else:
            meta = None
            if info.imdb_id:
                # A supplied IMDb ID is authoritative: never run fuzzy title search for it.
                meta = await self._get_meta(info.media_type, info.imdb_id)
            else:
                candidates = await self._search(info.media_type, info.title)
                if candidates:
                    ranked = sorted(
                        candidates,
                        key=lambda item: _metadata_match_score(info.title, info.year, item),
                        reverse=True,
                    )
                    best = ranked[0]
                    if _metadata_match_score(info.title, info.year, best) >= 0.56:
                        meta = best
            self._cache[cache_key] = meta

        if not meta:
            return info

        meta_title = str(meta.get("name") or meta.get("title") or "").strip()
        if meta_title:
            info.title = meta_title
        meta_id = str(meta.get("imdb_id") or meta.get("imdbId") or meta.get("id") or "").strip()
        if re.fullmatch(r"(?i)tt\d{7,10}", meta_id):
            info.imdb_id = meta_id.lower()
        meta_year = meta.get("year")
        if meta_year:
            year_match = re.search(r"(?:19|20)\d{2}", str(meta_year))
            if year_match:
                info.year = year_match.group(0)
        elif meta.get("releaseInfo"):
            year_match = re.search(r"(?:19|20)\d{2}", str(meta["releaseInfo"]))
            if year_match:
                info.year = year_match.group(0)
        if meta.get("description"):
            info.description = str(meta["description"]).strip()
        if meta.get("poster"):
            info.poster = str(meta["poster"]).strip()
        if meta.get("background"):
            info.backdrop = str(meta["background"]).strip()
        if meta.get("imdbRating") not in (None, ""):
            info.rating = str(meta["imdbRating"]).strip()
        genres = meta.get("genres")
        if isinstance(genres, list):
            info.genres = [str(item).strip() for item in genres if str(item).strip()][:4]

        tmdb_id = meta.get("tmdb_id") or meta.get("tmdbId") or meta.get("tmdb")
        if tmdb_id is not None and str(tmdb_id).isdigit():
            info.tmdb_id = str(tmdb_id)
        tmdb_url = str(meta.get("tmdb_url") or meta.get("tmdbUrl") or "").strip()
        if tmdb_url.startswith("https://www.themoviedb.org/"):
            info.tmdb_url = tmdb_url
        return info


def dedupe_key(info: MediaInfo, announce_each_episode: bool = False) -> str:
    """VPS-style title dedupe, with Unicode-safe fallback and optional episode keys."""
    if info.imdb_id:
        key = "imdb:" + info.imdb_id.casefold()
    else:
        title = _normalize_title(info.title)
        key = f"title:{title}:{info.year or 'na'}"
    if (
        announce_each_episode and info.media_type == "series"
        and info.episode is not None
    ):
        key += f":s{info.season or 0}e{info.episode}"
    return key


def _clean_rating(value: str) -> str:
    try:
        number = float(value)
        if 0 <= number <= 10:
            return f"{number:.1f}"
    except (TypeError, ValueError):
        pass
    return ""


def build_caption(info: MediaInfo) -> str:
    """Build an HTML caption matching the VPS announcer's layout."""
    is_series = info.media_type == "series"
    title = html.escape(re.sub(r"\s+", " ", info.title or "Unknown").strip())
    year = html.escape(str(info.year or "").strip())
    header = f"{'📺' if is_series else '🎬'} <b>{title}</b>"
    if year:
        header += f" ({year})"

    lines = [
        header,
        "",
        f"🗂 <b>Type:</b> {'Series' if is_series else 'Movie'}",
    ]
    rating = _clean_rating(info.rating)
    if rating:
        lines.append(f"⭐ <b>Rating:</b> {rating}")
    genres = [str(genre).strip() for genre in (info.genres or []) if str(genre).strip()][:4]
    if genres:
        lines.append("🎭 <b>Genres:</b> " + html.escape(", ".join(genres)))
    if info.quality:
        lines.append(f"📶 <b>Quality:</b> {html.escape(info.quality.strip())}")

    description = re.sub(r"\s+", " ", (info.description or "").strip())
    if description:
        if len(description) > 320:
            description = description[:317] + "..."
        lines.extend(["", f"<i>{html.escape(description)}</i>"])
    return "\n".join(lines)


def _is_safe_public_base(value: str) -> bool:
    """Only accept a public-looking HTTPS host for VPS deep-link buttons."""
    if not value:
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.casefold().rstrip(".")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
            return False
        try:
            address = ipaddress.ip_address(host)
            if (
                address.is_private or address.is_loopback or address.is_link_local
                or address.is_unspecified or address.is_multicast
            ):
                return False
            # CGNAT addresses are not public service addresses either.
            if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def build_keyboard(info: MediaInfo, public_base_url: str = "") -> InlineKeyboardMarkup | None:
    media_path_type = "series" if info.media_type == "series" else "movie"
    rows: list[list[InlineKeyboardButton]] = []
    imdb_id = (info.imdb_id or "").strip()
    if _is_safe_public_base(public_base_url) and imdb_id:
        base = public_base_url.rstrip("/")
        encoded_id = quote(imdb_id, safe="")
        rows.append([
            InlineKeyboardButton(
                "▶️ Stremio",
                url=f"{base}/open/stremio/{media_path_type}/{encoded_id}",
            ),
            InlineKeyboardButton(
                "📱 Nuvio",
                url=f"{base}/open/nuvio/{media_path_type}/{encoded_id}",
            ),
        ])
    elif imdb_id:
        rows.append([
            InlineKeyboardButton(
                "IMDb", url=f"https://www.imdb.com/title/{quote(imdb_id, safe='')}/"
            )
        ])
    elif info.tmdb_url.startswith("https://www.themoviedb.org/"):
        rows.append([InlineKeyboardButton("TMDb", url=info.tmdb_url)])
    elif info.tmdb_id and info.tmdb_id.isdigit():
        media_kind = "tv" if info.media_type == "series" else "movie"
        rows.append([
            InlineKeyboardButton(
                "TMDb",
                url=f"https://www.themoviedb.org/{media_kind}/{quote(info.tmdb_id, safe='')}",
            )
        ])
    return InlineKeyboardMarkup(rows) if rows else None


class AnnouncementWorker:
    def __init__(self, client: Client, config: Config):
        self.client = client
        self.config = config
        self.store = AnnouncementStore(config.state_db)
        self.metadata = Cinemeta()
        self._lock = asyncio.Lock()
        self.source_chat_ids: set[int] = set(config.source_ids)

    async def resolve_source_chats(self) -> None:
        """Resolve configured usernames once, so channel-delete updates can be mapped."""
        for username in sorted(self.config.source_usernames):
            try:
                chat = await self.client.get_chat("@" + username)
                chat_id = _safe_int(getattr(chat, "id", None))
                if chat_id is not None:
                    self.source_chat_ids.add(chat_id)
            except Exception as exc:
                logging.warning("Could not resolve source @%s: %s", username, exc)

    def _is_source(self, message: Any) -> bool:
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        chat_id = _safe_int(getattr(chat, "id", None))
        if chat_id is not None and chat_id in self.source_chat_ids:
            return True
        username = str(getattr(chat, "username", "") or "").lstrip("@").casefold()
        return bool(username and username in self.config.source_usernames)

    async def handle(self, _client: Client, message: Any) -> None:
        try:
            if not self._is_source(message):
                return
            file_name = _message_file_name(message)
            caption_text = str(
                getattr(message, "caption", "") or getattr(message, "text", "") or ""
            )
            if not _is_media_message(message, file_name):
                return

            info = parse_media(file_name, caption_text)
            if info is None:
                logging.info("Skipped media with no usable title (message %s).", getattr(message, "id", "?"))
                return

            async with self._lock:
                info = await self.metadata.enrich(info)
                key = dedupe_key(info, self.config.announce_each_episode)
                if self.store.has(key):
                    logging.info("Already announced: %s", info.title)
                    return

                sent = await self._send_announcement_with_retry(info)
                if sent is None:
                    logging.error("Announcement send failed for %r; dedupe key was not stored.", info.title)
                    return

                source_chat = getattr(message, "chat", None)
                sent_chat = getattr(sent, "chat", None)
                source_chat_id = _safe_int(getattr(source_chat, "id", None))
                source_message_id = _safe_int(getattr(message, "id", None))
                sent_chat_id = _safe_int(getattr(sent_chat, "id", None))
                sent_message_id = _safe_int(getattr(sent, "id", None))
                if source_chat_id is None or source_message_id is None or sent_message_id is None:
                    logging.error("Telegram accepted an announcement, but message IDs were missing; state was not stored.")
                    return
                if sent_chat_id is None:
                    sent_chat_id = self.config.announcement_channel
                self.store.add(
                    key=key,
                    info=info,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    sent_chat_id=sent_chat_id,
                    sent_message_id=sent_message_id,
                )
                logging.info("Announced %s (%s)", info.title, key)
        except Exception as exc:
            # Isolate failures to this update; the listener must keep processing new posts.
            logging.error("Could not process source media message: %s", exc)

    async def _send_announcement_with_retry(self, info: MediaInfo) -> Any | None:
        """On FloodWait, sleep as requested and retry the send exactly once."""
        try:
            return await self._send_announcement(info)
        except FloodWait as exc:
            wait_seconds = max(1, int(getattr(exc, "value", 1)))
            logging.warning("Telegram FloodWait: sleeping %s seconds before one retry.", wait_seconds)
            await asyncio.sleep(wait_seconds)
            try:
                return await self._send_announcement(info)
            except Exception as retry_exc:
                logging.error("Announcement retry failed: %s", retry_exc)
                return None
        except Exception as exc:
            logging.error("Announcement send failed: %s", exc)
            return None

    async def _send_announcement(self, info: MediaInfo) -> Any | None:
        caption = build_caption(info)
        keyboard = build_keyboard(info, self.config.public_base_url)
        if info.photo_url and info.photo_url.startswith(("https://", "http://")):
            try:
                return await self.client.send_photo(
                    chat_id=self.config.announcement_channel,
                    photo=info.photo_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except FloodWait:
                raise
            except Exception as exc:
                logging.warning("Photo send failed; falling back to text for %r: %s", info.title, exc)

        return await self.client.send_message(
            chat_id=self.config.announcement_channel,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def handle_deleted(self, _client: Client, messages: Any) -> None:
        """Delete mapped announcements when their original source post is deleted."""
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        for message in messages:
            try:
                chat = getattr(message, "chat", None)
                chat_id = _safe_int(
                    getattr(chat, "id", None) if chat is not None
                    else getattr(message, "chat_id", None)
                )
                message_id = _safe_int(
                    getattr(message, "id", None) or getattr(message, "message_id", None)
                )
                if chat_id is None or message_id is None or chat_id not in self.source_chat_ids:
                    continue
                for row in self.store.find_source_message(chat_id, message_id):
                    try:
                        await self.client.delete_messages(
                            chat_id=row["sent_chat_id"],
                            message_ids=[int(row["sent_message_id"])],
                        )
                        logging.info("Deleted announcement for removed source message %s.", message_id)
                    except Exception as exc:
                        # Drop state even on forbidden/invalid deletes: do not retry forever.
                        logging.warning("Could not delete mapped announcement for source %s: %s", message_id, exc)
                    finally:
                        self.store.remove(row["key"])
            except Exception as exc:
                logging.error("Could not process deleted source message: %s", exc)

    async def close(self) -> None:
        await self.metadata.close()


async def main() -> None:
    load_dotenv(APP_DIR / ".env")
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    config.state_db.parent.mkdir(parents=True, exist_ok=True)

    app = Client(
        name=config.session_name,
        api_id=config.api_id,
        api_hash=config.api_hash,
        workdir=str(APP_DIR),
    )
    worker = AnnouncementWorker(app, config)
    app.add_handler(MessageHandler(worker.handle, filters.channel), group=-1)
    app.add_handler(DeletedMessagesHandler(worker.handle_deleted, filters.channel), group=-1)

    try:
        await app.start()
        me = await app.get_me()
        display_name = " ".join(
            filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])
        ) or "Telegram user"
        await worker.resolve_source_chats()
        logging.info("Logged in as %s (id %s).", display_name, me.id)
        logging.info("Watching %d configured source channel(s).", len(config.source_ids) + len(config.source_usernames))
        logging.info("Announcements will be published to %s.", config.announcement_channel)
        await idle()
    finally:
        await worker.close()
        if getattr(app, "is_connected", False):
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAnnouncement worker stopped.")
    except Exception as exc:
        raise SystemExit(f"Startup failed: {exc}") from exc
