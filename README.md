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
- Up to 720p where available, one active download/upload at a time, 45 MiB final file limit. Larger videos are rejected.
- Temporary download files are deleted after each task. No user-provided cookies, account logins, DRM bypass or arbitrary-domain fetching.
- Non-root Docker service, read-only filesystem, 768 MiB RAM / 1 vCPU, 256 MiB ephemeral download storage, bounded logs.
- Unit tests, CI, and no tokens stored in Git.

**Scope:** Direct MP4 from arbitrary sites, MEGA, Witanime, playlists, interactive quality selection and Telegram Local Bot API are **not yet implemented**. The initial bot does **not** expose the whole Android NOVA feature set. Downloads should be limited to content you have permission to save.

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

The default standard Telegram Bot API supports uploads up to 50 MB; the MVP caps output at 45 MiB to leave margin. `/downloads` is a 256 MiB **tmpfs** (RAM-backed) mounted into the container, so it competes with available host memory even though container `mem_limit` may not fully account for all charged pages. Review Docker memory and disk behavior under real traffic before raising limits. The initial bot only supports private chats and ignores group messages. A single task runs synchronously; new messages wait for the current task, with no durable queue. Requests interrupted by process restart are not resumed.

Do not expect hard guarantees from CPU quota alone. Avoid running heavy transcoding concurrently with production database workloads.

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
