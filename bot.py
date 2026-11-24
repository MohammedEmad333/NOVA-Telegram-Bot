"""Private, single-worker Telegram video bot. No third-party Telegram SDK required."""
import http.client
import json
import logging
import sys
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("nova")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
MAX_MB = int(os.environ.get("MAX_VIDEO_MB", "45"))
MAX_BYTES = MAX_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "900"))
WORKDIR = Path(os.environ.get("WORKDIR", "/downloads"))
API_HOST = "api.telegram.org"
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
SUPPORTED = ("youtube.com", "youtu.be", "youtube-nocookie.com", "soundcloud.com", "x.com", "twitter.com")


def allowed_ids():
    return {int(item.strip()) for item in ALLOWED_RAW.split(",") if item.strip().isdigit()}


def permitted_url(value):
    """Restrict sources to a known allowlist. Do not download arbitrary URLs."""
    try:
        p = urlsplit(value)
        host = (p.hostname or "").lower().rstrip(".")
        return (p.scheme == "https" and p.username is None and p.password is None
                and p.port in (None, 443) and bool(host)
                and any(host == domain or host.endswith("." + domain) for domain in SUPPORTED))
    except ValueError:
        return False


def extract_url(text):
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,!?)\"'") if match else None


class TelegramApiError(Exception):
    """Telegram API failure without exposing the token-bearing request URL."""

    def __init__(self, status, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__("Telegram API HTTP " + str(status))


def retry_after_seconds(headers=None, body=None):
    """Honor Telegram ResponseParameters.retry_after and Retry-After headers."""
    values = []
    header = headers.get("Retry-After") if headers else None
    try:
        data = json.loads(body) if body else {}
        parameter = (data.get("parameters") or {}).get("retry_after")
    except (ValueError, TypeError, AttributeError):
        parameter = None

    for raw in (header, parameter):
        if isinstance(raw, bool):
            continue
        try:
            seconds = int(raw)
        except (ValueError, TypeError, OverflowError):
            continue
        if 0 < seconds <= 604800:
            values.append(seconds)
    return max(values, default=None)


def polling_delay(status, errors, retry_after=None):
    """Back off on every failure, with a longer wait for Telegram rate limits."""
    fallback = min(900, 30 * 2 ** min(max(errors - 1, 0), 5))
    if status in (401, 403):
        return max(900, fallback)
    if status == 409:
        return max(300, fallback)
    if status == 429:
        return max(fallback, (retry_after or 0) + 2)
    return fallback


def api_call(method, params=None, timeout=45):
    payload = json.dumps(params or {}).encode()
    req = Request("https://" + API_HOST + "/bot" + TOKEN + "/" + method,
                  data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        # Never log HTTPError or its URL; both can contain the secret bot token.
        retry_after = retry_after_seconds(exc.headers, exc.read(16384))
        raise TelegramApiError(exc.code, retry_after) from None
    if not result.get("ok"):
        raise TelegramApiError(
            result.get("error_code", 0),
            retry_after_seconds(body=json.dumps(result)),
        )
    return result.get("result")


def message(chat_id, text):
    return api_call("sendMessage", {"chat_id": chat_id, "text": text})


def safe_send(chat_id, text):
    try:
        message(chat_id, text)
    except TelegramApiError as exc:
        LOG.warning("Message failed: Telegram HTTP %d", exc.status)
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.warning("Could not send message: %s", type(exc).__name__)


def classify_ytdlp_error(stderr):
    """Map yt-dlp diagnostics to safe categories without logging raw URLs or stderr."""
    detail = (stderr or "").lower()
    cases = [
        (("sign in to confirm you're not a bot", "confirm you're not a bot"),
         ("youtube_verification",
          "YouTube طلب تحققًا من المصدر. لا يستخدم NOVA حسابات أو يتجاوز أنظمة الحماية.")),
        (("no supported javascript runtime",),
         ("javascript_runtime_missing",
          "بيئة JavaScript المطلوبة لاستخراج YouTube غير متاحة داخل الحاوية.")),
        (("remote component challenge solver script", "n challenge solving failed",
          "challenge solving failed"),
         ("youtube_challenge",
          "تعذّر حل تحدّي YouTube. نحتاج مراجعة إعدادات yt-dlp ومكوّن EJS.")),
        (("http error 429", "429: too many requests"),
         ("source_rate_limited",
          "المصدر فرض حدًا مؤقتًا على الطلبات. انتظر قليلًا قبل إعادة المحاولة.")),
        (("http error 403", "403: forbidden"),
         ("source_forbidden",
          "المصدر رفض طلب التحميل (HTTP 403). قد لا يسمح بالتحميل من عنوان IP الخاص بالسيرفر.")),
        (("private video", "this video is private"),
         ("private_video", "هذا الفيديو خاص ولا يمكن الوصول إليه كرابط عام.")),
        (("sign in", "login required", "authentication required"),
         ("login_required", "المصدر يتطلب تسجيل دخول، والبوت لا يستخدم حسابات أو ملفات تعريف ارتباط.")),
        (("video unavailable", "this video is unavailable", "has been removed"),
         ("video_unavailable", "الفيديو غير متاح من المصدر حاليًا.")),
        (("requested format is not available", "no video formats found"),
         ("no_compatible_format", "لم يعثر NOVA على صيغة فيديو متوافقة لهذا الرابط.")),
        (("file is larger than max-filesize", "file is larger than the maximum"),
         ("source_file_too_large", "حجم الفيديو يتجاوز الحد المسموح به للبوت.")),
        (("ffmpeg not found", "ffmpeg is not installed"),
         ("ffmpeg_missing", "أداة FFmpeg غير متاحة داخل الحاوية.")),
        (("unsupported url",),
         ("unsupported_url", "هذا الرابط غير مدعوم من محرك التحميل.")),
        (("timed out", "timeout"),
         ("source_timeout", "انتهت مهلة الاتصال بمصدر الفيديو.")),
        (("unable to download webpage", "connection reset", "network is unreachable"),
         ("source_network_error", "تعذّر الاتصال بمصدر الفيديو من السيرفر.")),
    ]
    for needles, result in cases:
        if any(needle in detail for needle in needles):
            return result
    return ("unknown", "تعذّر استخراج الفيديو. السبب غير معروف بعد؛ أعد المحاولة لاحقًا.")


def download(url, folder):
    """A bounded, one-video download. No playlists, cookies, login or custom config."""
    output = str(folder / "nova.%(ext)s")
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--no-cache-dir", "--no-part",
        "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--max-filesize", str(MAX_BYTES), "--restrict-filenames",
        "--format", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "--output", output, "--", url,
    ]
    try:
        result = subprocess.run(args, cwd=str(folder), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=DOWNLOAD_TIMEOUT,
                                check=False, text=True)
    except subprocess.TimeoutExpired:
        LOG.warning("yt-dlp timed out after %d seconds", DOWNLOAD_TIMEOUT)
        raise ValueError("انتهت مهلة التحميل. جرّب فيديو أقصر.")
    if result.returncode:
        reason, user_message = classify_ytdlp_error(result.stderr)
        LOG.warning("yt-dlp failed (exit=%d, reason=%s)", result.returncode, reason)
        raise ValueError(user_message)
    videos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in
              (".mp4", ".mkv", ".webm", ".mov")]
    if len(videos) != 1:
        LOG.warning("yt-dlp completed without a recognized video file")
        raise ValueError("لم يتم إنشاء ملف فيديو نهائي بصيغة مدعومة.")
    selected = videos[0]
    if not 0 < selected.stat().st_size <= MAX_BYTES:
        LOG.warning("yt-dlp output exceeded configured size limit")
        raise ValueError("الفيديو أكبر من الحد المسموح للإرسال.")
    return selected


def send_video(chat_id, path):
    """Stream multipart upload without buffering the entire video in RAM."""
    boundary = "nova_boundary_8d7e6f21"
    head = (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        + str(chat_id) + "\r\n"
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="document"; filename="nova' + path.suffix.lower() + '"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = ("\r\n--" + boundary + "--\r\n").encode()
    conn = http.client.HTTPSConnection(API_HOST, timeout=180)
    try:
        conn.putrequest("POST", "/bot" + TOKEN + "/sendDocument")
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        conn.putheader("Content-Length", str(len(head) + path.stat().st_size + len(tail)))
        conn.endheaders()
        conn.send(head)
        with path.open("rb") as f:
            while data := f.read(256 * 1024):
                conn.send(data)
        conn.send(tail)
        response = conn.getresponse()
        payload = response.read(8192)
        if response.status != 200 or not json.loads(payload).get("ok"):
            raise RuntimeError("Telegram upload rejected")
    finally:
        conn.close()


def handle(update):
    entry = update.get("message") or {}
    user = entry.get("from") or {}
    chat = entry.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    user_id = user.get("id")
    text = entry.get("text") or ""
    if not chat_id or not isinstance(user_id, int):
        return
    if text.strip().startswith("/id"):
        safe_send(chat_id, "Telegram user ID: " + str(user_id))
        return
    if user_id not in allowed_ids():
        safe_send(chat_id, "هذا البوت خاص وغير متاح لهذا الحساب.")
        return
    if text.strip().startswith(("/start", "/help")):
        safe_send(chat_id, "NOVA Bot 🎬\nأرسل رابط YouTube أو SoundCloud أو فيديو X عام. حد الملف: " + str(MAX_MB) + " MB. تحميل واحد في كل مرة.")
        return
    url = extract_url(text)
    if not url or not permitted_url(url):
        safe_send(chat_id, "أرسل رابط HTTPS عامًا من YouTube أو SoundCloud أو X. لا ندعم الروابط الخاصة أو المحمية.")
        return
    safe_send(chat_id, "جاري تجهيز الفيديو…")
    folder = Path(tempfile.mkdtemp(prefix="nova-", dir=str(WORKDIR)))
    try:
        file_path = download(url, folder)
        send_video(chat_id, file_path)
        safe_send(chat_id, "اكتمل الإرسال ✅")
    except ValueError as exc:
        safe_send(chat_id, str(exc))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        LOG.exception("Download/send failure: %s", type(exc).__name__)
        safe_send(chat_id, "حدث خطأ أثناء التحميل أو الإرسال. جرّب مرة أخرى لاحقًا.")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main():
    if not TOKEN or ":" not in TOKEN or not allowed_ids():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS before startup")
    if not 1 <= MAX_MB <= 45 or not 30 <= DOWNLOAD_TIMEOUT <= 3600:
        raise SystemExit("Invalid resource limits")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("NOVA private bot starting, max video size %s MB", MAX_MB)
    offset = None
    errors = 0
    while True:
        try:
            updates = api_call("getUpdates", {
                "offset": offset, "timeout": 25, "limit": 10,
                "allowed_updates": ["message"],
            }, timeout=35)
            errors = 0
            for update in updates:
                offset = update["update_id"] + 1
                handle(update)
        except TelegramApiError as exc:
            errors += 1
            delay = polling_delay(exc.status, errors, exc.retry_after)
            LOG.warning("Telegram polling HTTP %d; next attempt in %d seconds",
                        exc.status, delay)
            time.sleep(delay)
        except (OSError, RuntimeError, ValueError, URLError) as exc:
            errors += 1
            delay = polling_delay(None, errors)
            LOG.warning("Polling network/response failure (%s); next attempt in %d seconds",
                        type(exc).__name__, delay)
            time.sleep(delay)


def diagnose():
    """A single private health check; never print or persist the bot token."""
    try:
        result = api_call("getMe", timeout=15)
        print("Telegram getMe: OK")
        print("Username:", (result or {}).get("username", "unknown"))
    except TelegramApiError as exc:
        print("Telegram getMe: HTTP", exc.status)
        if exc.retry_after is not None:
            print("Telegram retry_after (seconds):", exc.retry_after)
        raise SystemExit(1)
    except (OSError, ValueError) as exc:
        print("Telegram connection error:", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--check-telegram":
        diagnose()
    else:
        main()
