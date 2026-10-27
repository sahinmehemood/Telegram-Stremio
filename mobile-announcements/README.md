# Mobile Announcements for TeleStremio (Termux)

## 1. What it is

Mobile Announcements is a small Python worker that runs in Termux and posts an announcement when new video media arrives in Telegram source channels. It logs into Telegram as your user account through PyroFork, not with a bot token. It uses Cinemeta when possible and stores deduplication state in a local SQLite database. It is **not inside the TeleStremio Android app** and it does **not** share that app's login or session.

## 2. What you need

- An Android phone with [Termux](https://termux.dev/) installed.
- A Telegram API ID and API hash from [my.telegram.org/apps](https://my.telegram.org/apps).
- Your Telegram account already joined to every source media channel.
- Admin rights or equivalent permission to post in the destination announcement channel.
- Internet access while the worker is running.

The worker uses your **Telegram user account** to read source channels because those channels may not have a bot. It sends announcements as the same account, so Telegram must allow that account to post in the destination.

## 3. Install in Termux

Paste these commands in Termux:

~~~sh
pkg update
pkg install python git
git clone --branch feature/mobile-media-announcements https://github.com/sahinmehemood/Telegram-Stremio.git
cd Telegram-Stremio/mobile-announcements
python -m pip install -r requirements.txt
cp .env.example .env
nano .env
python announcer.py
~~~

Fill in **API_ID**, **API_HASH**, **SOURCE_CHANNELS**, and **ANNOUNCEMENT_CHANNEL** before the final command. Use a comma-separated list for **SOURCE_CHANNELS**, such as @public_movies,-1001234567890. Usernames are matched without @ and without case sensitivity. Use the full -100… ID for private channels; a plain numeric ID and a -100… ID are compared as different integers.

For a source that only has a private numeric ID, use that exact ID. Do not add a bot to your source channels: the worker reads them using your logged-in Telegram user session.

## 4. First login and session safety

On the first run of **python announcer.py**, PyroFork prompts for your Telegram phone number, the login code Telegram sends you, and your two-step-verification password if you have one. These are normal Telegram sign-in prompts; do not put your phone number, code, or 2FA password in **.env**. The session file is saved in **mobile-announcements/** using the configured **SESSION_NAME** (by default **telestremio_announcer.session**).

Never commit, upload, or share the session file, its journal file, **.env**, or the SQLite database. They are ignored by Git. If a session file leaks, revoke that session from Telegram **Settings → Devices** and sign in again.

Optional performance note: PyroFork can run without TgCrypto, but may be slower. Only try installing TgCrypto separately if pip has a compatible wheel for your Termux/Python version; if pip attempts a source build or fails, skip it rather than setting up a compiler.

## 5. Keep it running on Android

Run this in a second Termux session to request a wake lock:

~~~sh
termux-wake-lock
~~~

Allow Termux **Unrestricted** battery usage in Android's app battery settings, and do not force-stop Termux. Keep the worker's terminal session open; a terminal multiplexer may help if you use one. To release the wake lock later, run **termux-wake-unlock**.

Android can still kill Termux or its network activity, especially under memory pressure or vendor battery management. This is a phone process, **not 24/7 hosting**. Announcements are not backfilled after an interruption: if a post arrives while the worker is stopped, it is not announced when you restart it.

## 6. What it will and will not post

- It handles **new channel posts received after startup only**. It does not scan or announce old channel history.
- It accepts Telegram videos, video MIME-type documents, documents named with common video extensions, and common archive/split parts (.zip, .rar, .7z, .001, .part, and filenames such as movie.mkv.001). Plain text, photos, audio, stickers, and subtitles are ignored.
- Each movie is announced once; a series is announced the first time it is seen. Additional qualities and later episodes of that series do not create new posts by default.
- Set **ANNOUNCE_EACH_EPISODE=true** to use season/episode keys for series posts. A post without a useful parsed title is skipped and logged.
- The worker parses names with guessit where possible, then falls back to a filename parser. It looks up Cinemeta without an API key, but if metadata is unavailable it still posts the parsed title, year, type and quality.
- The announcement is formatted as HTML with the title/year, Movie or Series, one-decimal rating when available, up to four genres, quality, and a description truncated to 320 characters. It sends the poster when available, otherwise the backdrop. If Telegram rejects the photo send, the worker falls back to the same caption as a text post.
- A local SQLite database remembers successfully sent announcements across restarts. If a source post that triggered an announcement is deleted, the worker attempts to delete its corresponding announcement and drops the local mapping even if Telegram forbids the deletion.
- On a Telegram FloodWait, the worker waits for Telegram's requested duration and retries that send once. If the retry fails, it leaves the dedupe key unstored.

## 7. Buttons: public HTTPS server or IMDb/TMDb

Leave **PUBLIC_BASE_URL** blank unless you have a **public HTTPS server** that implements the same VPS redirect routes. When a safe public HTTPS base and IMDb ID are both available, the post gets **▶️ Stremio** and **📱 Nuvio** buttons pointing to /open/stremio/{movie|series}/{imdbId} and /open/nuvio/{movie|series}/{imdbId}. The worker does not create these routes and does not make the Android app a public server.

Without that base URL, an available IMDb ID gets one IMDb button; otherwise a known TMDb ID/link can get a TMDb button. Localhost and private LAN IP addresses are never used for Stremio/Nuvio buttons. No fake 127.0.0.1 button or bot-only “Get Addon” button is generated.
