# TeleStremio Mobile Announcements (Termux companion)

This folder is a **phone-runnable announcement worker** for TeleStremio users. It watches Telegram channels for new video/media posts and publishes one formatted announcement to a destination channel. It is designed for Termux on Android; it does not require a VPS or MongoDB.

> **Important scope note:** the public TeleStremio repository does not contain the native Android app's Kotlin/Gradle source. This is therefore a companion process, not a rebuilt or modified APK. It delivers the Telegram-channel announcement behavior while native app source is unavailable.

## Features

- Watches one or more Telegram source channels as your own Telegram account (so it can read channels your account can already access, without inviting a bot to every source).
- Posts a poster-backed message when a new title is first detected.
- Enriches titles from Cinemeta (no API key required); unmatched titles still get a useful filename-based announcement.
- Includes title, year, movie/series type, IMDb rating, genres, release quality and a short description when metadata is available.
- Supports optional public redirect URL buttons compatible with the full server's /open/stremio/... and /open/nuvio/... routes.
- Persists announced title keys in SQLite to avoid reposting a title on process restarts.
- Understands common release filenames and episode patterns via guessit, with a conservative fallback parser.
- Keeps your Telegram session and settings local; no tokens or session files are committed.

## What you need

1. Android with Termux.
2. A Telegram API ID and API hash from https://my.telegram.org/apps.
3. Admin permission to post in the destination announcement channel.
4. Membership/access to the media source channels you want to watch.

This uses your **user account**, not a bot token. The first run asks you to log in with your Telegram phone number, code, and 2FA password if enabled. A local session file is created; **never share or commit that file**.

## Install in Termux

~~~sh
pkg update
pkg install -y python git
git clone --branch feature/mobile-media-announcements https://github.com/sahinmehemood/Telegram-Stremio.git
cd Telegram-Stremio/mobile-announcements
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
nano .env
python announcer.py
~~~

The first launch prompts for the Telegram login. Once the account is signed in, the worker listens for new posts in the configured source channels.

### Configure .env

- API_ID: numeric API ID from my.telegram.org/apps.
- API_HASH: API hash from my.telegram.org/apps.
- SOURCE_CHANNELS: comma-separated channel IDs or public usernames, e.g. @my_movies,-1001234567890. Use IDs for private channels.
- ANNOUNCEMENT_CHANNEL: your destination channel ID or public username, e.g. @my_media_updates.
- PUBLIC_BASE_URL (optional): public HTTPS base URL for a server that implements /open/stremio/{movie|series}/{imdb_id} and /open/nuvio/{movie|series}/{imdb_id}. Leave blank to use IMDb/TMDb info buttons instead. A phone-local URL such as 127.0.0.1 is not reachable by people clicking your channel posts.
- ANNOUNCE_EACH_EPISODE: false by default, which mirrors the VPS server's title-level deduplication. Set to true to announce each identifiable episode separately.
- STATE_DB: optional local SQLite path (default announcements.sqlite3).

The account must have permission to post in the destination channel. It must already be a member of each source channel. If Telegram channel signatures are enabled, posts may show the admin signature according to the channel's settings.

## Keep it running on Android

Termux must remain installed and allowed to run in the background. For a long-running session, use a terminal multiplexer or a foreground session, allow Termux unrestricted battery use, and acquire a wake lock with termux-wake-lock if the device sleeps aggressively. Android may still stop background processes; this is not a 24/7 VPS.

## Behavior and limitations

- It only handles **new incoming posts** after the worker is started; it does not bulk-scan historic channel contents.
- It announces a title once by its canonical IMDb ID when metadata is found, otherwise by a normalized title/year key. Additional qualities or episodes of a series do not create repeat announcements unless ANNOUNCE_EACH_EPISODE=true.
- Source filenames without enough title information may be posted using their best-effort parsed title; metadata is not guaranteed for obscure, personal, or incorrectly named files.
- Without a public HTTPS redirect service, Telegram inline buttons cannot safely route through the VPS-style redirect endpoint. The worker uses HTTPS information links instead. A configured PUBLIC_BASE_URL restores the Stremio/Nuvio buttons if those routes exist.
- This is separate from the TeleStremio Android process. Stopping the worker stops new announcements; playback in TeleStremio is unaffected.

## Safety

Keep .env, *.session, SQLite database files, and logs private. If the Telegram session is exposed, revoke it from Telegram's Devices settings.
