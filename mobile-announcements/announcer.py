#!/usr/bin/env python3
"""Termux-friendly Telegram media announcement worker.

Watches source channels via a logged-in Telegram user session and publishes
one formatted announcement per movie/series (or per episode when configured).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import httpx
except ImportError:  # Gives a helpful message when launched before installing requirements.
    httpx = None

try:
    from guessit import guessit
except ImportError:
    guessit = None

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


APP_DIR = Path(__file__).resolve().parent
CINEMETA_BASE = "https://v3-cinemeta.strem.io"
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".wmv", ".flv", ".ogv", ".3gp", ".vob", ".asf",
    ".divx", ".mpv", ".mpe", ".mpg", ".iso",
}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".001", ".002", ".003", ".part"}
QUALITY_RE = re.compile(r"(?i)(2160\s*p?|4k|1440p|1080p|720p|576p|480p|360p)")
IMDB_RE = re.compile(r"(?i)\btt\d{7,10}\b")
SEASON_EPISODE_RE = re.compile(r"(?i)\bS(\d{1,2})[\s._-]*E(\d{1,3})\b")
X_EPISODE_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")
EPISODE_ONLY_RE = re.compile(r"(?i)\bE(?:P)?[\s._-]?(\d{1,4})\b")


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs without overriding values already in the environment."""
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
        return value if value.startswith("@") else f"@{value}"


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
        api_id_text = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()
        source_raw = os.getenv("SOURCE_CHANNELS", "").strip()
        target_raw = os.getenv("ANNOUNCEMENT_CHANNEL", "").strip()

        missing = [
            key for key, value in (
                ("API_ID", api_id_text),
                ("API_HASH", api_hash),
                ("SOURCE_CHANNELS", source_raw),
                ("ANNOUNCEMENT_CHANNEL", target_raw),
            ) if not value
        ]
        if missing:
            raise ValueError(
                "Missing required setting(s) in mobile-announcements/.env: "
                + ", ".join(missing)
            )
        try:
            api_id = int(api_id_text)
        except ValueError as exc:
            raise ValueError("API_ID must be a numeric value from my.telegram.org/apps.") from exc

        source_ids, source_usernames = _parse_source_channels(source_raw)
        if not source_ids and not source_usernames:
            raise ValueError("SOURCE_CHANNELS must contain at least one channel ID or username.")

        state_raw = os.getenv("STATE_DB", "announcements.sqlite3").strip()
        state_path = Path(state_raw).expanduser()
        if not state_path.is_absolute():
            state_path = APP_DIR / state_path

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            source_ids=source_ids,
            source_usernames=source_usernames,
            announcement_channel=_parse_chat(target_raw),
            session_name=os.getenv("SESSION_NAME", "telestremio_announcer").strip(),
            state_db=state_path,
            announce_each_episode=_env_bool("ANNOUNCE_EACH_EPISODE", False),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip(),
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
    tmdb_url: str = ""
    tmdb_id: str = ""

    def __post_init__(self) -> None:
        if self.genres is None:
            self.genres = []


class AnnouncementStore:
    """Small local SQLite store for successful announcement deduplication."""

    def __init__(self, path: Path):
        self.path = path
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
                    sent_message_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
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
        with self._connect() as db:
            return db.execute("SELECT 1 FROM announced WHERE key = ?", (key,)).fetchone() is not None

    def add(
        self,
        key: str,
        info: MediaInfo,
        source_chat_id: str,
        source_message_id: int,
        sent_message_id: int | None,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO announced
                (key, title, media_type, source_chat_id, source_message_id, sent_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, info.title, info.media_type, source_chat_id,
                    source_message_id, sent_message_id, created_at,
                ),
            )


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
            mime = str(getattr(obj, "mime_type", "") or "").lower()
            if mime.startswith("video/"):
                return ""
    return ""


def _is_media_message(message: Any, file_name: str) -> bool:
    if getattr(message, "video", None):
        return True
    if getattr(message, "document", None):
        doc = message.document
        mime = str(getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            return True
        # Include common archive/split-volume files used for large Telegram media.
        lower_name = file_name.casefold()
        if any(lower_name.endswith(ext) for ext in VIDEO_EXTENSIONS):
            return True
        if any(lower_name.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
            return True
        if re.search(r"\.(?:mkv|mp4|avi|mov|webm)\.\d{3}$", lower_name):
            return True
    return False


def _quality_from_name(text: str) -> str:
    match = QUALITY_RE.search(text or "")
    if not match:
        return ""
    value = match.group(0).lower().replace(" ", "")
    if value == "4k":
        return "2160p / 4K"
    if value.startswith("2160"):
        return "2160p / 4K"
    return value


def _fallback_title(file_name: str, caption: str) -> str:
    name = file_name or caption or "Unknown media"
    name = re.sub(r"(?i)\.(mkv|mp4|m4v|avi|mov|webm|mpg|mpeg|ts|m2ts|wmv|flv|zip|rar|7z)(?:\.\d{3})?$", "", name)
    name = SEASON_EPISODE_RE.sub(" ", name)
    name = X_EPISODE_RE.sub(" ", name)
    name = EPISODE_ONLY_RE.sub(" ", name)
    name = re.sub(r"(?i)\b(?:2160p?|4k|1440p|1080p|720p|576p|480p|360p|web[- .]?dl|webrip|bluray|blu-ray|brrip|bdrip|hdtv|hdr10?\+?|dolby.?vision|dv|hevc|x265|h265|x264|h264|av1|aac|ddp?\d(?:\.\d)?|dts(?:-hd)?|proper|repack|remux)\b.*$", " ", name)
    name = re.sub(r"\b(19|20)\d{2}\b.*$", " ", name)
    name = re.sub(r"[\[\](){}]", " ", name)
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+-\s+.*$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    return name or "Unknown media"


def parse_media(file_name: str, caption: str = "") -> MediaInfo | None:
    """Parse a video-ish filename/caption into a conservative media identity."""
    source = file_name or caption
    if not source.strip():
        return None

    parsed: dict[str, Any] = {}
    if guessit is not None:
        try:
            parsed = dict(guessit(source))
        except Exception:
            parsed = {}

    title = str(parsed.get("title") or "").strip()
    guessed_type = str(parsed.get("type") or "").lower()
    season = _safe_int(parsed.get("season"))
    episode = _safe_int(parsed.get("episode"))

    # Keep TV detection working even when guessit is unavailable, which is useful
    # for lightweight diagnostics and conservative fallback parsing.
    se_match = SEASON_EPISODE_RE.search(source)
    x_match = X_EPISODE_RE.search(source)
    ep_match = EPISODE_ONLY_RE.search(source)
    if se_match:
        season = season if season is not None else int(se_match.group(1))
        episode = episode if episode is not None else int(se_match.group(2))
    elif x_match:
        season = season if season is not None else int(x_match.group(1))
        episode = episode if episode is not None else int(x_match.group(2))
    elif episode is None and ep_match:
        episode = int(ep_match.group(1))
    is_series = guessed_type in {"episode", "tv"} or season is not None or episode is not None

    if not title:
        title = _fallback_title(source, caption)

    # Prefer the filename-derived title, but if the caption begins with a readable
    # title and the file name is only a generic upload name, fall back to the caption.
    if title.casefold() in {"unknown", "video", "movie", "download"} and caption.strip():
        title = _fallback_title(caption, "")

    year = str(parsed.get("year") or "").strip()
    if year and not re.fullmatch(r"\d{4}", year):
        year = ""

    quality = _quality_from_name(file_name + " " + caption)
    if not year:
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", source)
        if year_match:
            year = year_match.group(1)

    info = MediaInfo(
        title=title,
        media_type="series" if is_series else "movie",
        year=year,
        season=season,
        episode=episode,
        quality=quality,
    )

    # Explicit IMDb IDs are useful and avoid fuzzy title matching later.
    imdb_match = IMDB_RE.search(source) or IMDB_RE.search(caption or "")
    if imdb_match:
        info.imdb_id = imdb_match.group(0).lower()
    return info


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _metadata_match_score(wanted_title: str, wanted_year: str, candidate: dict[str, Any]) -> float:
    candidate_title = str(candidate.get("name") or candidate.get("title") or "")
    score = SequenceMatcher(None, _normalize_title(wanted_title), _normalize_title(candidate_title)).ratio()
    candidate_year = str(candidate.get("year") or "")[:4]
    if wanted_year and candidate_year:
        if wanted_year == candidate_year:
            score += 0.12
        elif wanted_year != candidate_year:
            score -= 0.12
    return min(score, 1.0)


class Cinemeta:
    """Public Stremio/Cinemeta metadata lookup. No API key is required."""

    def __init__(self) -> None:
        if httpx is None:
            raise RuntimeError("httpx is missing; run: python -m pip install -r requirements.txt")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": "TeleStremio-Mobile-Announcements/0.1"},
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
        except (httpx.HTTPError, ValueError, TypeError):
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
        except (httpx.HTTPError, ValueError, TypeError):
            return []

    async def enrich(self, info: MediaInfo) -> MediaInfo:
        key = f"{info.media_type}:{info.imdb_id or _normalize_title(info.title)}:{info.year}"
        if key in self._cache:
            meta = self._cache[key]
        else:
            meta = None
            if info.imdb_id:
                meta = await self._get_meta(info.media_type, info.imdb_id)
                if not meta and info.media_type == "series":
                    meta = await self._get_meta("series", info.imdb_id)
            if meta is None:
                candidates = await self._search(info.media_type, info.title)
                if candidates:
                    ranked = sorted(
                        candidates,
                        key=lambda item: _metadata_match_score(info.title, info.year, item),
                        reverse=True,
                    )
                    best = ranked[0]
                    score = _metadata_match_score(info.title, info.year, best)
                    if score >= 0.56:
                        meta = best
            self._cache[key] = meta

        if not meta:
            return info

        meta_title = str(meta.get("name") or meta.get("title") or "").strip()
        meta_id = str(meta.get("id") or "").strip()
        if meta_title:
            info.title = meta_title
        if meta_id.lower().startswith("tt"):
            info.imdb_id = meta_id.lower()
        if meta.get("year"):
            info.year = str(meta["year"])[:4]
        elif meta.get("releaseInfo"):
            match = re.search(r"\b(?:19|20)\d{2}\b", str(meta["releaseInfo"]))
            if match:
                info.year = match.group(0)
        if meta.get("description"):
            info.description = str(meta["description"]).strip()
        if meta.get("poster"):
            info.poster = str(meta["poster"]).strip()
        if meta.get("background"):
            info.poster = info.poster or str(meta["background"]).strip()
        if meta.get("imdbRating") not in (None, ""):
            info.rating = str(meta["imdbRating"]).strip()
        genres = meta.get("genres")
        if isinstance(genres, list):
            info.genres = [str(g).strip() for g in genres if str(g).strip()][:4]
        info.tmdb_url = str(meta.get("url") or "").strip()
        return info


def dedupe_key(info: MediaInfo, announce_each_episode: bool = False) -> str:
    """A stable title key; optionally split series keys by season/episode."""
    if info.imdb_id:
        identity = info.imdb_id
    else:
        identity = _normalize_title(info.title)
        if info.year:
            identity += ":" + info.year
    key = f"{info.media_type}:{identity}"
    if announce_each_episode and info.media_type == "series" and info.season is not None and info.episode is not None:
        key += f":s{info.season:02d}e{info.episode:02d}"
    return key


def _clean_rating(value: str) -> str:
    try:
        number = float(value)
        if not (0 <= number <= 10):
            return ""
        return f"{number:.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def build_caption(info: MediaInfo) -> str:
    is_series = info.media_type == "series"
    header = f"{'📺' if is_series else '🎬'} <b>{html.escape(info.title or 'Unknown media')}</b>"
    if info.year:
        header += f" ({html.escape(info.year)})"

    lines = [header, "", f"🗂 <b>Type:</b> {'Series' if is_series else 'Movie'}"]
    rating = _clean_rating(info.rating)
    if rating:
        lines.append(f"⭐ <b>Rating:</b> {rating}/10")
    if info.genres:
        genre_text = ", ".join(info.genres[:4])
        lines.append(f"🎭 <b>Genres:</b> {html.escape(genre_text)}")
    if info.quality:
        lines.append(f"📶 <b>Quality:</b> {html.escape(info.quality)}")
    if is_series and info.season is not None and info.episode is not None:
        lines.append(f"📺 <b>Episode:</b> S{info.season:02d}E{info.episode:02d}")

    description = (info.description or "").strip()
    if description:
        # Photo captions in Telegram have a strict 1024-character limit.
        description = re.sub(r"\s+", " ", description)
        description = description[:260].rstrip()
        lines.extend(["", f"<i>{html.escape(description)}</i>"])

    caption = "\n".join(lines)
    if len(caption) <= 900:
        return caption
    return caption[:895].rstrip() + "..."


def build_keyboard(info: MediaInfo, public_base_url: str = "") -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    media_path_type = "series" if info.media_type == "series" else "movie"
    public_base_url = public_base_url if public_base_url.startswith("https://") else ""

    if public_base_url and info.imdb_id:
        rows.append([
            InlineKeyboardButton(
                "▶️ Stremio",
                url=f"{public_base_url}/open/stremio/{media_path_type}/{quote(info.imdb_id, safe='')}",
            ),
            InlineKeyboardButton(
                "📱 Nuvio",
                url=f"{public_base_url}/open/nuvio/{media_path_type}/{quote(info.imdb_id, safe='')}",
            ),
        ])
    else:
        info_buttons: list[InlineKeyboardButton] = []
        if info.imdb_id:
            info_buttons.append(
                InlineKeyboardButton("IMDb", url=f"https://www.imdb.com/title/{quote(info.imdb_id, safe='')}/")
            )
        if info.tmdb_url.startswith("https://"):
            info_buttons.append(InlineKeyboardButton("Metadata", url=info.tmdb_url))
        if info_buttons:
            rows.append(info_buttons)

    return InlineKeyboardMarkup(rows) if rows else None


class AnnouncementWorker:
    def __init__(self, client: Client, config: Config):
        self.client = client
        self.config = config
        self.store = AnnouncementStore(config.state_db)
        self.metadata = Cinemeta()
        self._lock = asyncio.Lock()

    def _is_source(self, message: Any) -> bool:
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        chat_id = _safe_int(getattr(chat, "id", None))
        if chat_id is not None and chat_id in self.config.source_ids:
            return True
        username = str(getattr(chat, "username", "") or "").casefold()
        return bool(username and username in self.config.source_usernames)

    async def handle(self, _client: Client, message: Any) -> None:
        if not self._is_source(message):
            return

        file_name = _message_file_name(message)
        caption_text = str(getattr(message, "caption", "") or getattr(message, "text", "") or "")
        if not _is_media_message(message, file_name):
            return

        info = parse_media(file_name, caption_text)
        if info is None or not info.title or info.title.casefold() == "unknown media":
            logging.warning("Skipped a media post because its title could not be parsed.")
            return

        # Locking around metadata lookup, dedupe check and send prevents simultaneous
        # posts for multiple resolutions of the same title from creating duplicates.
        async with self._lock:
            try:
                info = await self.metadata.enrich(info)
                key = dedupe_key(info, self.config.announce_each_episode)
                if self.store.has(key):
                    logging.info("Already announced: %s", info.title)
                    return

                sent = await self._send_announcement_with_retry(info)
                if sent is None:
                    logging.error("Could not publish announcement for %s", info.title)
                    return

                chat = getattr(message, "chat", None)
                self.store.add(
                    key=key,
                    info=info,
                    source_chat_id=str(getattr(chat, "id", "")),
                    source_message_id=int(getattr(message, "id", 0) or 0),
                    sent_message_id=int(getattr(sent, "id", 0) or 0) or None,
                )
                logging.info("Announced %s (%s)", info.title, key)
            except FloodWait as exc:
                wait_seconds = max(1, int(getattr(exc, "value", 1)))
                logging.warning("Telegram flood wait: %ss. Skipping this attempt; the next matching media may retry.", wait_seconds)
            except Exception:
                logging.exception("Announcement failed for media file %r", file_name)

    async def _send_announcement_with_retry(self, info: MediaInfo) -> Any | None:
        """Retry brief Telegram/network failures without marking the title announced."""
        for attempt in range(3):
            try:
                return await self._send_announcement(info)
            except FloodWait as exc:
                wait_seconds = max(1, int(getattr(exc, "value", 1)))
                if wait_seconds > 120 or attempt == 2:
                    raise
                logging.warning("Telegram requested a %ss wait; retrying afterwards.", wait_seconds)
                await asyncio.sleep(wait_seconds + 1)
            except Exception:
                if attempt == 2:
                    raise
                delay = 2 ** attempt
                logging.warning("Send attempt %s failed; retrying in %ss.", attempt + 1, delay, exc_info=True)
                await asyncio.sleep(delay)
        return None

    async def _send_announcement(self, info: MediaInfo) -> Any | None:
        caption = build_caption(info)
        keyboard = build_keyboard(info, self.config.public_base_url)
        if info.poster and info.poster.startswith("https://"):
            try:
                return await self.client.send_photo(
                    chat_id=self.config.announcement_channel,
                    photo=info.poster,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except FloodWait:
                raise
            except Exception as exc:
                logging.warning("Poster post failed (%s); retrying as text.", type(exc).__name__)

        return await self.client.send_message(
            chat_id=self.config.announcement_channel,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def close(self) -> None:
        await self.metadata.close()


async def main() -> None:
    load_dotenv(APP_DIR / ".env")
    config = Config.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    config.state_db.parent.mkdir(parents=True, exist_ok=True)
    config.state_db.touch(exist_ok=True)

    app = Client(
        name=config.session_name,
        api_id=config.api_id,
        api_hash=config.api_hash,
        workdir=str(APP_DIR),
    )
    worker = AnnouncementWorker(app, config)
    app.add_handler(MessageHandler(worker.handle, filters.channel), group=-1)

    try:
        await app.start()
        me = await app.get_me()
        display_name = " ".join(filter(None, [me.first_name, me.last_name])) or "Telegram user"
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
