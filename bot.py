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
        (("no video could be found in this tweet", "no video found in this tweet"),
         ("no_video_in_post",
          "لم يعثر X على فيديو مرفق بهذا المنشور. قد يكون المنشور نصًا أو صورة أو رابطًا خارجيًا، أو أن بيانات الفيديو غير متاحة للعامة. جرّب منشورًا يحتوي فيديو مرفقًا مباشرةً.")),
        (("this broadcast is no longer available", "broadcast is no longer available"),
         ("broadcast_unavailable", "تسجيل بث X لم يعد متاحًا للإعادة من المصدر.")),
        (("this broadcast has not yet started", "broadcast is scheduled to start"),
         ("broadcast_not_started", "بث X لم يبدأ بعد، ولا يوجد تسجيل متاح للتنزيل.")),
        (("geo-restricted", "not available in your country"),
         ("source_geo_restricted", "تسجيل بث X غير متاح في منطقة السيرفر.")),
        (("unable to download m3u8", "failed to download m3u8", "m3u8 download error"),
         ("hls_unavailable", "تعذّر تنزيل مسار HLS الخاص ببث X.")),
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


def parse_newpipe_result(stdout, returncode):
    """Read the bridge's final JSON line, including expected diagnostic exits."""
    try:
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        metadata = json.loads(lines[-1])
    except (ValueError, IndexError, json.JSONDecodeError):
        raise ValueError("invalid_extractor_output") from None
    if not isinstance(metadata, dict) or not isinstance(metadata.get("ok"), bool):
        raise ValueError("invalid_extractor_output")
    if returncode and metadata.get("ok"):
        raise ValueError("inconsistent_extractor_exit")
    return metadata

def download_youtube(url, folder):
    """Resolve YouTube streams with the same NewPipeExtractor version used by NOVA Android."""
    command = [
        "java", "-cp", "/opt/nova-extractor/lib/*", "NovaExtractor", url,
    ]
    try:
        result = subprocess.run(
            command, cwd=str(folder), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=120, check=False, text=True,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("انتهت مهلة تحليل YouTube. جرّب مرة أخرى لاحقًا.")
    try:
        metadata = parse_newpipe_result(result.stdout, result.returncode)
    except ValueError as exc:
        LOG.warning("NewPipe extractor returned invalid metadata (exit=%d, reason=%s)",
                    result.returncode, str(exc))
        raise ValueError("تعذّر قراءة نتيجة محرك YouTube. راجع سجل التشخيص.") from None
    if not metadata.get("ok"):
        reason = metadata.get("reason", "extractor_error")
        LOG.warning("NewPipe extraction failed (reason=%s, error_type=%s)",
                    reason, metadata.get("error_type", "none"))
        messages = {
            "youtube_anti_bot": "YouTube قيّد الوصول المجهول من عنوان IP الخاص بالسيرفر (Sign in to confirm you are not a bot). هذا ليس دليلًا على أن الفيديو خاص أو يحتاج حسابًا. لن نستخدم Cookies أو نتجاوز التحقق.",
            "source_challenge": "YouTube طلب تحققًا من المصدر. لم يتم تجاوز أنظمة الحماية.",
            "login_required": "المصدر يتطلب تسجيل دخول فعلًا، والبوت لا يستخدم حسابات أو Cookies.",
            "source_rate_limited": "YouTube فرض حدًا مؤقتًا على الطلبات. انتظر قليلًا قبل المحاولة.",
            "source_forbidden": "YouTube رفض طلب الوصول إلى مسار الفيديو.",
            "no_compatible_stream_under_limit": "لم نجد جودة MP4 متوافقة ضمن حد 45 MB.",
            "extractor_error": "NewPipe لم يتمكن من استخراج بيانات الفيديو. نحتاج رمز الخطأ التفصيلي من السجل.",
            "youtube_unavailable": "الفيديو غير متاح عبر NewPipe من هذا السيرفر.",
        }
        raise ValueError(messages.get(reason, "تعذّر استخراج الفيديو من YouTube."))
    video_url = metadata.get("video_url")
    audio_url = metadata.get("audio_url")
    if not video_url or not permitted_media_url(video_url):
        raise ValueError("لم يتم العثور على رابط فيديو صالح.")
    if audio_url and not permitted_media_url(audio_url):
        raise ValueError("لم يتم العثور على رابط صوت صالح.")
    estimated = int(metadata.get("video_bytes") or 0) + int(metadata.get("audio_bytes") or 0)
    if estimated > 0 and estimated > MAX_BYTES:
        raise ValueError("حجم الفيديو يتجاوز حد الإرسال البالغ 45 MB.")

    video_path = folder / "nova-video.mp4"
    audio_path = folder / "nova-audio.m4a"
    output_path = folder / "nova.mp4"

    def fetch_stream(stream_url, destination, remaining):
        request = Request(stream_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
            "Referer": "https://www.youtube.com/",
            "Accept-Encoding": "identity",
        })
        written = 0
        with urlopen(request, timeout=45) as response, destination.open("wb") as target:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining:
                    raise ValueError("حجم مسار الفيديو تجاوز الحد المسموح.")
                target.write(chunk)
        if written == 0:
            raise ValueError("أعاد مصدر الفيديو ملفًا فارغًا.")
        return written

    video_size = fetch_stream(video_url, video_path, MAX_BYTES)
    if audio_url:
        audio_size = fetch_stream(audio_url, audio_path, MAX_BYTES - video_size)
        try:
            merged = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", str(video_path), "-i", str(audio_path),
                 "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                 "-movflags", "+faststart", "-y", str(output_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("انتهت مهلة دمج الصوت والفيديو.")
        if merged.returncode != 0 or not output_path.is_file():
            LOG.warning("FFmpeg stream mux failed (exit=%d)", merged.returncode)
            raise ValueError("تعذّر دمج الصوت والفيديو.")
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    else:
        video_path.rename(output_path)

    final_size = output_path.stat().st_size
    if not 0 < final_size <= MAX_BYTES:
        raise ValueError("الملف النهائي أكبر من حد الإرسال البالغ 45 MB.")
    LOG.info("NewPipe download ready (resolution=%s, bytes=%d)",
             metadata.get("resolution", "unknown"), final_size)
    return output_path


def permitted_media_url(value):
    """Only accept extractor-issued Google video CDN URLs."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme == "https" and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and (host == "googlevideo.com" or host.endswith(".googlevideo.com")))
    except ValueError:
        return False


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


def is_x_broadcast_url(url):
    """Recognize X/Twitter replay broadcast URLs, not ordinary video posts."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        trusted = (host == "x.com" or host.endswith(".x.com")
                   or host == "twitter.com" or host.endswith(".twitter.com"))
        return (parsed.scheme == "https" and trusted
                and parsed.path.startswith("/i/broadcasts/"))
    except ValueError:
        return False


def x_broadcast_download_args(url, folder):
    """Prefer 480p replay HLS, fall back to <=720p, and cap the output."""
    output = str(folder / "nova-x-broadcast.%(ext)s")
    return [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--no-cache-dir", "--no-part",
        "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
        "--max-filesize", str(MAX_BYTES), "--restrict-filenames",
        "--format", "best[height<=480]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "--output", output, "--", url,
    ]


def _folder_bytes(folder):
    total = 0
    for item in folder.iterdir():
        if item.is_file():
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                pass
    return total


def _stop_download_process(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def download_x_broadcast(url, folder):
    """Download an ended X broadcast replay with a hard on-disk size guard."""
    args = x_broadcast_download_args(url, folder)
    try:
        with tempfile.TemporaryFile(mode="w+b") as error_output:
            try:
                process = subprocess.Popen(
                    args, cwd=str(folder), stdout=subprocess.DEVNULL,
                    stderr=error_output,
                )
            except OSError as exc:
                LOG.warning("Could not start X broadcast downloader (%s)",
                            type(exc).__name__)
                raise ValueError("تعذّر تشغيل محرك تنزيل بث X.") from None

            started = time.monotonic()
            while process.poll() is None:
                if _folder_bytes(folder) > MAX_BYTES:
                    _stop_download_process(process)
                    raise ValueError(
                        "حجم تسجيل البث تجاوز حد 45MB. لم نرسل ملفًا جزئيًا."
                    )
                if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
                    _stop_download_process(process)
                    raise ValueError(
                        "انتهت مهلة تنزيل تسجيل X. جرّب تسجيلًا أقصر."
                    )
                time.sleep(0.25)

            if _folder_bytes(folder) > MAX_BYTES:
                raise ValueError(
                    "حجم تسجيل البث تجاوز حد 45MB. لم نرسل ملفًا جزئيًا."
                )

            if process.returncode:
                error_output.seek(0)
                stderr = error_output.read(16384).decode("utf-8", "replace")
                reason, user_message = classify_ytdlp_error(stderr)
                LOG.warning("X broadcast download failed (exit=%d, reason=%s)",
                            process.returncode, reason)
                raise ValueError(user_message)
    except subprocess.TimeoutExpired:
        raise ValueError("انتهت مهلة تنزيل تسجيل X. جرّب تسجيلًا أقصر.") from None

    videos = [p for p in folder.iterdir() if p.is_file()
              and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if len(videos) != 1:
        LOG.warning("X broadcast completed without a single video output")
        raise ValueError("لم يتم إنشاء ملف فيديو صالح من تسجيل X.")
    selected = videos[0]
    if not 0 < selected.stat().st_size <= MAX_BYTES:
        raise ValueError("حجم تسجيل X يتجاوز حد الإرسال البالغ 45MB.")
    LOG.info("X broadcast replay ready (bytes=%d)", selected.stat().st_size)
    return selected


def download_audio(url, folder):
    """Download one permitted SoundCloud track as bounded MP3 audio."""
    output = str(folder / "nova-audio.%(ext)s")
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--no-cache-dir", "--no-part",
        "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--max-filesize", str(MAX_BYTES), "--restrict-filenames",
        "--format", "bestaudio/best",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "5",
        "--output", output, "--", url,
    ]
    try:
        result = subprocess.run(args, cwd=str(folder), stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=DOWNLOAD_TIMEOUT,
                                check=False, text=True)
    except subprocess.TimeoutExpired:
        LOG.warning("SoundCloud audio extraction timed out after %d seconds", DOWNLOAD_TIMEOUT)
        raise ValueError("انتهت مهلة تحميل الصوت. جرّب مقطعًا أقصر.")
    if result.returncode:
        reason, user_message = classify_ytdlp_error(result.stderr)
        LOG.warning("SoundCloud yt-dlp failed (exit=%d, reason=%s)", result.returncode, reason)
        raise ValueError(user_message)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"]
    if len(files) != 1:
        LOG.warning("SoundCloud extraction completed without a single MP3 output")
        raise ValueError("لم يتم إنشاء ملف صوت MP3 صالح.")
    selected = files[0]
    if not 0 < selected.stat().st_size <= MAX_BYTES:
        LOG.warning("SoundCloud audio exceeded configured size limit")
        raise ValueError("حجم الملف الصوتي يتجاوز حد الإرسال.")
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


def send_audio(chat_id, path):
    """Stream an MP3 upload through Telegram's audio endpoint."""
    boundary = "nova_audio_boundary_6a1f9c42"
    head = (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        + str(chat_id) + "\r\n"
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="audio"; filename="nova.mp3"\r\n'
        "Content-Type: audio/mpeg\r\n\r\n"
    ).encode()
    tail = ("\r\n--" + boundary + "--\r\n").encode()
    conn = http.client.HTTPSConnection(API_HOST, timeout=180)
    try:
        conn.putrequest("POST", "/bot" + TOKEN + "/sendAudio")
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
            raise RuntimeError("Telegram audio upload rejected")
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
        host = (urlsplit(url).hostname or "").lower()
        is_soundcloud = host == "soundcloud.com" or host.endswith(".soundcloud.com")
        if is_soundcloud:
            file_path = download_audio(url, folder)
            send_audio(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال الصوت ✅")
        elif is_x_broadcast_url(url):
            file_path = download_x_broadcast(url, folder)
            send_video(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال تسجيل البث ✅")
        else:
            if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
                file_path = download_youtube(url, folder)
            else:
                file_path = download(url, folder)
            send_video(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال الفيديو ✅")
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
