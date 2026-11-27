# NOVA Telegram Bot 🎬

A private, self-hosted Telegram media downloader inspired by [NOVADownloader](https://github.com/MohammedEmad333/NOVADownloader). **Early MVP, not production-verified.**

Official Telegram bot: **[@NovaOrbitDLBot](https://t.me/NovaOrbitDLBot)**. This identity is separate from the backend service; the Telegram API token stays only in the private server environment.

## Initial features

- Telegram long polling. No incoming ports, public domain or webhook needed.
- Private access based on Telegram numeric user IDs. Unapproved users cannot download.
- YouTube extraction through the same NewPipeExtractor v0.26.5 library used by NOVADownloader Android; SoundCloud uses `yt-dlp`.
- Ordinary public X posts use the same FxTwitter/vxTwitter public metadata fallback as the NOVA Android app, validating `video.twimg.com` MP4/HLS URLs before download. X Broadcast replays use a separate bounded HLS path.
- Ended X Broadcast replays use a dedicated HLS path, prefer up to 480p, enforce the 45 MiB temporary/final output ceiling, and upload as MP4 documents. Ordinary X posts continue through the standard extractor.
- YouTube selects a compatible MP4 stream at or below 720p, preferring separate video + M4A audio and merging with FFmpeg without re-encoding. A progressive MP4 fallback is used when needed.
- Up to 720p where available, one active download/upload at a time. Local Bot API mode starts with a 1 GiB (1024 MiB) configurable file cap; the Local Bot API itself supports files up to 2000 MB.
- Temporary download files are deleted after each task. No user-provided cookies, account logins, DRM bypass or arbitrary-domain fetching.
- Non-root NOVA worker and private Local Bot API service on an internal Docker network. Downloads use a persistent disk volume shared with the API service; the API service is not published on a public host port.
- Unit tests, CI, and no tokens stored in Git.

**Scope:** Direct MP4 from arbitrary sites, MEGA, Witanime, playlists and interactive quality selection are **not yet implemented**. The Local Bot API is included for large uploads; it does not expand the list of supported media extractors. The initial bot does **not** expose the whole Android NOVA feature set. Downloads should be limited to content you have permission to save.

## Setup on Ubuntu (ARM64 supported)

1. Create a bot with Telegram **@BotFather** and keep its token private. Do not paste it into GitHub, chats or logs.
2. To find your numeric Telegram ID, you can first send `/id` to the bot (it responds before the allowlist is configured only if the process is already running with an existing valid allowlist), or obtain your ID through another trusted Telegram method.
3. Clone the repository and create a local private environment file:

```bash
git clone https://github.com/MohammedEmad333/NOVA-Telegram-Bot.git
cd NOVA-Telegram-Bot
cp .env.example .env
chmod 600 .env
nano .env
```

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` in `.env`. Use your **user ID**, not your chat username. Multiple user IDs may be comma-separated.

4. After a reviewed pull request is merged into `main`, start:

```bash
docker compose up -d --build
docker compose logs --tail=60 nova-bot
```

While the PR is awaiting review, use `git switch feat/initial-private-bot` after cloning to test the development branch instead. Keep test deployments private.

Stop the bot with `docker compose down`. Updates: `git pull --ff-only && docker compose up -d --build`.

## Resource policy

Local Bot API mode is enabled for this deployment. Telegram's local server supports uploads up to 2000 MB; NOVA deliberately starts at 1 GiB (1024 MiB). `MAX_VIDEO_MB` can be raised up to 1800 MiB in the bot configuration, subject to available disk space, Telegram's limit and end-to-end testing. `/downloads` is a persistent Docker volume on the host disk, shared with the Local Bot API process for `file://` uploads; it is no longer a RAM-backed tmpfs. The API server's own state is stored in a separate persistent volume. The bot and API containers have bounded CPU/RAM and only one task runs at a time. Check free disk space before increasing the cap. The initial bot only supports private chats and ignores group messages. A single task runs synchronously; new messages wait for the current task, with no durable queue. Requests interrupted by process restart are not resumed.

Do not expect hard guarantees from CPU quota alone. Avoid running heavy transcoding concurrently with production database workloads.

## Durable download queue

NOVA stores accepted requests in a SQLite database at `/downloads/.nova-queue.sqlite3`, on the persistent Docker volume shared with the Local Bot API. Requests are inserted before their Telegram update offset is advanced. If the database is temporarily unavailable, that update is not acknowledged and Telegram can redeliver it.

- One worker processes one download/upload at a time, in FIFO order.
- The default queue admits up to 20 active jobs globally and up to 5 active jobs per user. The currently processing job counts toward both limits.
- Users can send `/queue` to see their own active requests and current positions.
- The queue is private to each user's Telegram account; other users' URLs and jobs are not shown.
- Pending and processing jobs survive container restarts. Jobs marked processing at startup return to pending and are retried.
- Completed/failed job records are retained for seven days for update de-duplication, then cleaned during startup.
- The database is created with restrictive file permissions and stores the submitted public source URL and Telegram IDs. Protect the Docker volume and host backups accordingly.

**Delivery semantics:** queue insertion is de-duplicated by Telegram update ID. A hard crash after Telegram accepts an upload but before NOVA records job completion can cause that job to be retried and the file to be sent twice. This is at-least-once recovery, not exactly-once delivery. A permanently failing source is marked failed and does not block later jobs.

## Local Telegram Bot API setup and migration

The Compose stack uses the Telegram Bot API server through a multi-architecture image (`linux/arm64` and `linux/amd64`). Its HTTP ports are only exposed inside the Compose network; they are not published to the host or internet.

1. Obtain the free `api_id` and `api_hash` from https://my.telegram.org. Add them to the server's private `.env` alongside the existing bot token and allowed user ID. Never commit or paste these credentials into GitHub or chat.
2. Set these values in `.env`:

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081
TELEGRAM_LOCAL_API=true
MAX_VIDEO_MB=1024
DOWNLOAD_TIMEOUT_SECONDS=1800
TELEGRAM_UPLOAD_TIMEOUT_SECONDS=1800
```

3. Pull the reviewed release and build NOVA:

```bash
cd ~/NOVA-Telegram-Bot
git pull --ff-only
docker compose config --quiet
docker compose build nova-bot
```

4. Stop the existing cloud-polling bot, start the private API service and initialize the shared disk volume:

```bash
docker compose stop nova-bot
docker compose up -d telegram-bot-api downloads-init
docker compose ps
docker compose logs --tail=40 telegram-bot-api
```

5. Only after the local API container is healthy, deregister this bot from Telegram's cloud Bot API exactly once. This is required before moving the same bot token to the local server:

```bash
docker compose run --rm --no-deps nova-bot python bot.py --logout-cloud
sleep 10
docker compose up -d nova-bot
docker compose logs --tail=50 nova-bot
```

6. Send `/start` and test with a small authorized media file first. Then increase test sizes gradually. The initial cap is 1 GiB (1024 MiB), not the Local Bot API maximum.

**Do not run the cloud and local pollers at the same time.** Do not run `--logout-cloud` repeatedly. If that command fails, leave NOVA stopped and inspect its numeric status before proceeding.

### Rollback to the cloud Bot API

Stop NOVA, call the local `logOut` once, then switch the `.env` values back to cloud mode before restarting:

```bash
docker compose stop nova-bot
docker compose run --rm --no-deps nova-bot python bot.py --logout-local
```

Then set `TELEGRAM_LOCAL_API=false`, `TELEGRAM_API_BASE_URL=https://api.telegram.org`, and `MAX_VIDEO_MB=45` in `.env`, wait 10 seconds, and run `docker compose up -d nova-bot`. Keep the local API service and volumes until the cloud-mode bot has been verified.

## YouTube anonymous-access restrictions

NewPipeExtractor's `SignInConfirmNotBotException` specifically means YouTube likely blocked anonymous watch access from the server IP. It does not establish that a video is private or that a Google account is required. The extractor project's exception documentation notes that this can usually only be resolved by changing IP. Do not respond by adding cookies, borrowing account sessions, or attempting to bypass YouTube verification. Use a permitted source/network for content you are authorized to download, or leave the source unsupported until access is available.
## Telegram HTTP 429 troubleshooting

If the bot starts but does not answer and logs show HTTP 429 (Too Many Requests),
stop its polling process: `docker compose stop nova-bot`. Check that another
instance or another application is not polling with the same token. Do **not**
repeatedly call `getMe` or `getWebhookInfo` while throttled; wait for Telegram's
`retry_after` interval (when available). HTTP 429 alone is not evidence that the token
is wrong. Other likely causes for a silent bot include a webhook configured on
the same token or a competing polling process (HTTP 409).

After updating the code, the bot respects Telegram's retry delay and applies
bounded exponential backoff to repeated failures. It logs HTTP status codes,
not secret bot tokens. To make **one** authenticated diagnostic call after the
cooldown, with the primary bot stopped, run:

```bash
docker compose run --rm --no-deps nova-bot python bot.py --check-telegram
```

This prints the bot username or the numeric HTTP status, and any
`retry_after` returned by Telegram, without printing the token. It is still
an API call, so do not loop this command during a rate limit.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q bot.py tests
docker compose config --quiet  # requires .env
```

These tests cover URL restrictions, authorization and safe stream-host validation, not live NewPipe extraction, third-party downloads or Telegram upload. End-to-end smoke tests and container resource profiling must pass before treating this as production-ready. End-to-end smoke tests and container resource profiling must pass before treating this as production-ready.

## Security notes

This is an early implementation. `yt-dlp` fetches more than the initial input URL internally (e.g. metadata and video CDNs); a validated initial URL is **not** a complete network egress sandbox. For hostile/public users, add an outbound-proxy policy or network isolation, rate limits, abuse controls and integration tests before opening access. Keep access restricted to trusted users. Telegram's Bot API receives uploaded file data.

No keys are committed. Protect the `.env` file and rotate your token if exposed.
