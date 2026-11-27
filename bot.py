"""Private, single-worker Telegram video bot. No third-party Telegram SDK required."""
import http.client
import json
import logging
import sys
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("nova")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
CLOUD_API_BASE_URL = "https://api.telegram.org"
API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", CLOUD_API_BASE_URL).rstrip("/")
LOCAL_API_MODE = os.environ.get("TELEGRAM_LOCAL_API", "false").strip().lower() in (
    "1", "true", "yes", "on"
)
MAX_SAFE_MB = 1800 if LOCAL_API_MODE else 45
MAX_MB = int(os.environ.get(
    "MAX_VIDEO_MB", "1024" if LOCAL_API_MODE else "45"
))
MAX_BYTES = MAX_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "1800"))
UPLOAD_TIMEOUT = int(os.environ.get("TELEGRAM_UPLOAD_TIMEOUT_SECONDS", "1800"))
WORKDIR = Path(os.environ.get("WORKDIR", "/downloads"))
QUEUE_DB_PATH = Path(os.environ.get(
    "QUEUE_DB_PATH", str(WORKDIR / ".nova-queue.sqlite3")
))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "20"))
MAX_QUEUE_PER_USER = int(os.environ.get("MAX_QUEUE_PER_USER", "5"))
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
SUPPORTED = ("youtube.com", "youtu.be", "youtube-nocookie.com", "soundcloud.com", "x.com", "twitter.com")
MEGA_HOSTS = {"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"}


def allowed_ids():
    return {int(item.strip()) for item in ALLOWED_RAW.split(",") if item.strip().isdigit()}


def parse_mega_file_url(value):
    """Parse a public MEGA file link while preserving its fragment decryption key."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or host not in MEGA_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            return None

        if parsed.path.startswith("/file/"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 2:
                return None
            file_id, file_key = parts[1], parsed.fragment
        else:
            fragment = parsed.fragment
            if fragment.startswith("!"):
                fragment = fragment[1:]
            parts = fragment.split("!")
            if len(parts) != 2:
                return None
            file_id, file_key = parts

        if not re.fullmatch(r"[A-Za-z0-9_-]{8}", file_id):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", file_key):
            return None
        return file_id, file_key
    except (ValueError, TypeError):
        return None


def permitted_url(value):
    """Restrict sources to a known allowlist. Do not download arbitrary URLs."""
    try:
        p = urlsplit(value)
        host = (p.hostname or "").lower().rstrip(".")
        valid_origin = (
            p.scheme == "https" and p.username is None and p.password is None
            and p.port in (None, 443) and bool(host)
        )
        if not valid_origin:
            return False
        if host in MEGA_HOSTS:
            return parse_mega_file_url(value) is not None
        return any(host == domain or host.endswith("." + domain) for domain in SUPPORTED)
    except ValueError:
        return False


def extract_url(text):
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,!?)\"'") if match else None


def queue_connection():
    connection = sqlite3.connect(str(QUEUE_DB_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def init_queue_db():
    """Create a private durable queue and recover jobs interrupted by a restart."""
    QUEUE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous_umask = os.umask(0o077)
    connection = None
    try:
        connection = queue_connection()
        connection.execute("""
            CREATE TABLE IF NOT EXISTS queue_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'processing', 'completed', 'failed')
                ),
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS queue_jobs_status_order "
            "ON queue_jobs(status, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS queue_jobs_user_status "
            "ON queue_jobs(user_id, status, id)"
        )
        connection.execute(
            "UPDATE queue_jobs SET status='pending', started_at=NULL "
            "WHERE status='processing'"
        )
        connection.execute(
            "DELETE FROM queue_jobs WHERE status IN ('completed', 'failed') "
            "AND finished_at < ?",
            (time.time() - 7 * 24 * 60 * 60,),
        )
        connection.commit()
    finally:
        if connection is not None:
            connection.close()
        os.umask(previous_umask)
    try:
        os.chmod(QUEUE_DB_PATH, 0o600)
    except OSError:
        LOG.warning("Could not restrict queue database permissions")


def enqueue_job(update_id, user_id, chat_id, url):
    """Persist a request before acknowledging its Telegram update."""
    connection = queue_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id, status FROM queue_jobs WHERE update_id=?",
            (update_id,),
        ).fetchone()
        if existing:
            connection.commit()
            return {"accepted": False, "duplicate": True,
                    "job_id": existing["id"], "status": existing["status"]}

        active_total = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status IN ('pending', 'processing')"
        ).fetchone()[0]
        if active_total >= MAX_QUEUE_SIZE:
            connection.commit()
            return {"accepted": False, "reason": "queue_full"}

        active_user = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE user_id=? "
            "AND status IN ('pending', 'processing')",
            (user_id,),
        ).fetchone()[0]
        if active_user >= MAX_QUEUE_PER_USER:
            connection.commit()
            return {"accepted": False, "reason": "user_limit"}

        cursor = connection.execute(
            "INSERT INTO queue_jobs "
            "(update_id, user_id, chat_id, url, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (update_id, user_id, chat_id, url, time.time()),
        )
        job_id = cursor.lastrowid
        processing_count = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status='processing'"
        ).fetchone()[0]
        pending_ahead = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status='pending' AND id<=?",
            (job_id,),
        ).fetchone()[0]
        position = processing_count + pending_ahead
        connection.commit()
        return {"accepted": True, "job_id": job_id, "position": position}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next_job():
    """Atomically claim the oldest pending job; only the single worker calls this."""
    connection = queue_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, update_id, user_id, chat_id, url FROM queue_jobs "
            "WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            "UPDATE queue_jobs SET status='processing', started_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_job(job_id, succeeded):
    connection = queue_connection()
    try:
        connection.execute(
            "UPDATE queue_jobs SET status=?, finished_at=? "
            "WHERE id=? AND status='processing'",
            ("completed" if succeeded else "failed", time.time(), job_id),
        )
        connection.execute(
            "DELETE FROM queue_jobs WHERE status IN ('completed', 'failed') "
            "AND finished_at < ?",
            (time.time() - 7 * 24 * 60 * 60,),
        )
        connection.commit()
    finally:
        connection.close()


def user_queue_snapshot(user_id):
    """Return a user's active jobs with positions in the global FIFO queue."""
    connection = queue_connection()
    try:
        processing_count = connection.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status='processing'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id, status FROM queue_jobs "
            "WHERE user_id=? AND status IN ('pending', 'processing') ORDER BY id",
            (user_id,),
        ).fetchall()
        result = []
        for row in rows:
            if row["status"] == "processing":
                position = 1
            else:
                pending_ahead = connection.execute(
                    "SELECT COUNT(*) FROM queue_jobs "
                    "WHERE status='pending' AND id<=?",
                    (row["id"],),
                ).fetchone()[0]
                position = processing_count + pending_ahead
            result.append({"job_id": row["id"], "status": row["status"],
                           "position": position})
        return result
    finally:
        connection.close()




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


def api_call(method, params=None, timeout=45, base_url=None):
    payload = json.dumps(params or {}).encode()
    endpoint = (base_url or API_BASE_URL).rstrip("/")
    req = Request(endpoint + "/bot" + TOKEN + "/" + method,
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

def download_mega(url, folder):
    """Stream and decrypt one public MEGA file link without buffering it in RAM."""
    parsed = parse_mega_file_url(url)
    if parsed is None:
        raise ValueError("رابط MEGA غير صالح أو لا يحتوي مفتاح فك التشفير.")

    file_id, encoded_key = parsed
    output_path = None
    client = None
    try:
        from mega import Mega
        from mega.crypto import (
            a32_to_str, base64_to_a32, base64_url_decode,
            decrypt_attr, get_chunks, str_to_a32,
        )
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
        import httpx

        client = Mega()
        file_data = client._api_request({"a": "g", "g": 1, "p": file_id})
        if not isinstance(file_data, dict) or not all(
            key in file_data for key in ("g", "s", "at")
        ):
            raise ValueError("ملف MEGA غير متاح، أو أن الرابط منتهي أو محذوف.")

        raw_size = file_data.get("s")
        if isinstance(raw_size, bool):
            raise ValueError("تعذّر قراءة حجم ملف MEGA.")
        try:
            file_size = int(raw_size)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("تعذّر قراءة حجم ملف MEGA.") from None
        if file_size <= 0:
            raise ValueError("ملف MEGA فارغ أو غير صالح.")
        if file_size > MAX_BYTES:
            raise ValueError(f"حجم ملف MEGA يتجاوز حد NOVA البالغ {MAX_MB} MB.")

        key_words = base64_to_a32(encoded_key)
        if len(key_words) != 8:
            raise ValueError("مفتاح فك تشفير ملف MEGA غير صالح.")
        file_key = tuple(key_words)
        key = (
            file_key[0] ^ file_key[4],
            file_key[1] ^ file_key[5],
            file_key[2] ^ file_key[6],
            file_key[3] ^ file_key[7],
        )
        iv = file_key[4:6] + (0, 0)
        expected_meta_mac = file_key[6:8]

        attributes = decrypt_attr(base64_url_decode(file_data["at"]), key)
        if not isinstance(attributes, dict):
            raise ValueError("تعذّر فك تشفير بيانات الملف من MEGA.")
        filename = str(attributes.get("n") or "mega-file.bin")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(
            char for char in filename
            if char not in "/\\:" and ord(char) >= 32 and ord(char) != 127
        ).strip(" .")
        if not filename or filename in (".", ".."):
            filename = "mega-file.bin"
        filename = filename[:180]
        output_path = folder / filename

        download_url = file_data["g"]
        download_host = (urlsplit(download_url).hostname or "").lower().rstrip(".")
        if (urlsplit(download_url).scheme != "https"
                or not (download_host.endswith(".mega.co.nz")
                        or download_host == "mega.co.nz"
                        or download_host.endswith(".mega.nz")
                        or download_host == "mega.nz")):
            raise ValueError("أعاد MEGA عنوان تنزيل غير صالح.")

        key_bytes = a32_to_str(key)
        ctr_value = (iv[0] << 32) + (iv[1] << 64)
        ctr = Counter.new(128, initial_value=ctr_value)
        aes_ctr = AES.new(key_bytes, AES.MODE_CTR, counter=ctr)

        mac_value = b"\x00" * 16
        mac_encryptor = AES.new(key_bytes, AES.MODE_CBC, mac_value)
        mac_iv = a32_to_str([iv[0], iv[1], iv[0], iv[1]])
        downloaded = 0
        started = time.monotonic()

        def read_exact(iterator, pending, expected):
            while len(pending) < expected:
                try:
                    pending.extend(next(iterator))
                except StopIteration:
                    raise ValueError("انقطع تنزيل ملف MEGA قبل اكتماله.") from None
            chunk = bytes(pending[:expected])
            del pending[:expected]
            return chunk

        timeout = httpx.Timeout(connect=20, read=60, write=20, pool=20)
        with client._http.stream("GET", download_url, timeout=timeout) as response:
            response.raise_for_status()
            declared_length = response.headers.get("Content-Length")
            if declared_length and declared_length.isdigit():
                if int(declared_length) != file_size:
                    raise ValueError("حجم استجابة MEGA لا يطابق بيانات الملف.")
            iterator = iter(response.iter_bytes(chunk_size=1024 * 1024))
            pending = bytearray()

            with output_path.open("wb") as target:
                for _, chunk_size in get_chunks(file_size):
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                        raise ValueError("انتهت مهلة تنزيل ملف MEGA.")
                    encrypted_chunk = read_exact(iterator, pending, chunk_size)
                    plain_chunk = aes_ctr.decrypt(encrypted_chunk)
                    target.write(plain_chunk)
                    downloaded += len(plain_chunk)

                    # MEGA authenticates each protocol chunk with a CBC-MAC.
                    cbc = AES.new(key_bytes, AES.MODE_CBC, mac_iv)
                    last_start = ((len(plain_chunk) - 1) // 16) * 16
                    for block_start in range(0, last_start, 16):
                        cbc.encrypt(plain_chunk[block_start:block_start + 16])
                    last_block = plain_chunk[last_start:]
                    if len(last_block) % 16:
                        last_block += b"\x00" * (16 - len(last_block) % 16)
                    mac_value = mac_encryptor.encrypt(cbc.encrypt(last_block))

                if downloaded != file_size or pending:
                    raise ValueError("حجم ملف MEGA الذي تم تنزيله غير مكتمل.")
                if next(iterator, b""):
                    raise ValueError("استجابة MEGA تجاوزت الحجم المعلن للملف.")

        actual_mac = str_to_a32(mac_value)
        actual_meta_mac = (
            actual_mac[0] ^ actual_mac[1],
            actual_mac[2] ^ actual_mac[3],
        )
        if actual_meta_mac != expected_meta_mac:
            raise ValueError("فشل التحقق من سلامة ملف MEGA بعد فك التشفير.")

        if not output_path.is_file() or output_path.stat().st_size != file_size:
            raise ValueError("الملف الناتج من MEGA غير مكتمل.")
        LOG.info("MEGA file download ready (bytes=%d)", file_size)
        return output_path
    except ValueError:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        LOG.warning("MEGA download failed (error_type=%s)", type(exc).__name__)
        raise ValueError(
            "تعذّر تنزيل ملف MEGA. قد يكون الرابط غير متاح أو أن MEGA فرض حدًا مؤقتًا."
        ) from None
    finally:
        if client is not None:
            try:
                client._http.close()
            except Exception:
                pass


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
            "no_compatible_stream_under_limit": f"لم نجد جودة MP4 متوافقة ضمن حد {MAX_MB} MB.",
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
        raise ValueError(f"حجم الفيديو يتجاوز حد الإرسال البالغ {MAX_MB} MB.")

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
        raise ValueError(f"الملف النهائي أكبر من حد الإرسال البالغ {MAX_MB} MB.")
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
        "--concurrent-fragments", "4",
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


def x_status_id(url):
    """Return only a numeric public X status ID from a canonical status URL."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or host not in ("x.com", "www.x.com", "twitter.com",
                                "www.twitter.com", "mobile.twitter.com")):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) not in (3, 5) or parts[1] != "status":
            return None
        if len(parts) == 5 and (parts[3] != "video"
                                or not parts[4].isdigit()
                                or not 1 <= int(parts[4]) <= 16):
            return None
        return parts[2] if parts[2].isdigit() and 10 <= len(parts[2]) <= 22 else None
    except (ValueError, TypeError):
        return None


def _safe_x_media_url(value, suffix):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and parsed.hostname == "video.twimg.com"
                and (parsed.path or "").lower().endswith(suffix))
    except (ValueError, TypeError):
        return False


def _fetch_x_public_json(endpoint):
    request = Request(endpoint, headers={
        "Accept": "application/json",
        "User-Agent": "NOVA-Telegram-Bot/1.0 (public media metadata)",
    })
    with urlopen(request, timeout=20) as response:
        raw = response.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("X metadata response exceeded limit")
    return json.loads(raw.decode("utf-8"))


def _x_video_candidates(root, status_id, backup=False):
    candidates = []
    if backup:
        if str(root.get("tweetID", "")) != status_id:
            return candidates
        items = root.get("media_extended") or []
        for item in items[:16]:
            if not isinstance(item, dict) or item.get("type") not in ("video", "gif"):
                continue
            url = item.get("url", "")
            if _safe_x_media_url(url, ".mp4"):
                size = item.get("size") if isinstance(item.get("size"), dict) else {}
                candidates.append((int(size.get("height") or 0), 0, url, "mp4"))
            elif _safe_x_media_url(url, ".m3u8"):
                size = item.get("size") if isinstance(item.get("size"), dict) else {}
                candidates.append((int(size.get("height") or 0), 0, url, "hls"))
        if not candidates:
            for url in (root.get("mediaURLs") or [])[:16]:
                if _safe_x_media_url(url, ".mp4"):
                    candidates.append((0, 0, url, "mp4"))
                elif _safe_x_media_url(url, ".m3u8"):
                    candidates.append((0, 0, url, "hls"))
        return candidates

    tweet = root.get("tweet")
    if not isinstance(tweet, dict) or root.get("code") != 200:
        return candidates
    returned_id = str(tweet.get("id") or "")
    if returned_id and returned_id != status_id:
        return candidates
    media = tweet.get("media")
    videos = media.get("videos") if isinstance(media, dict) else None
    if not isinstance(videos, list):
        return candidates
    for video in videos[:16]:
        if not isinstance(video, dict):
            continue
        variants = video.get("formats")
        if not isinstance(variants, list):
            variants = video.get("variants")
        if not isinstance(variants, list):
            variants = []
        for variant in variants[:32]:
            if not isinstance(variant, dict):
                continue
            url = variant.get("url") or ""
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (ValueError, TypeError):
                bitrate = 0
            try:
                height = int(video.get("height") or 0)
            except (ValueError, TypeError):
                height = 0
            if _safe_x_media_url(url, ".mp4"):
                candidates.append((height, bitrate, url, "mp4"))
            elif _safe_x_media_url(url, ".m3u8"):
                candidates.append((height, bitrate, url, "hls"))
        direct = video.get("url") or ""
        if _safe_x_media_url(direct, ".mp4"):
            candidates.append((int(video.get("height") or 0), 0, direct, "mp4"))
        elif _safe_x_media_url(direct, ".m3u8"):
            candidates.append((int(video.get("height") or 0), 0, direct, "hls"))
    return candidates


def _x_post_metadata(status_id):
    endpoints = (
        ("https://api.fxtwitter.com/status/" + status_id, False),
        ("https://api.vxtwitter.com/Twitter/status/" + status_id, True),
    )
    last_error = None
    for endpoint, backup in endpoints:
        try:
            root = _fetch_x_public_json(endpoint)
            candidates = _x_video_candidates(root, status_id, backup)
            if candidates:
                candidates.sort(key=lambda item: (
                    item[3] == "mp4",
                    item[0] <= 720 if item[0] else True,
                    item[1],
                    item[0],
                ), reverse=True)
                return candidates
            last_error = "no_public_video"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
    LOG.warning("X public metadata providers returned no usable video (reason=%s)",
                last_error or "unknown")
    raise ValueError(
        "لم يتم العثور على فيديو عام قابل للتنزيل في منشور X عبر مصادر البيانات العامة."
    )


def download_x_status(url, folder):
    """Use the same public FxTwitter/vxTwitter metadata strategy as NOVA Android."""
    status_id = x_status_id(url)
    if not status_id:
        raise ValueError("رابط منشور X غير صالح.")
    candidates = _x_post_metadata(status_id)
    selected = next((item for item in candidates if item[3] == "mp4"), None)
    if selected:
        height, bitrate, media_url, _ = selected
        output = folder / "nova-x.mp4"
        request = Request(media_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://x.com/",
            "Accept-Encoding": "identity",
        })
        written = 0
        started = time.monotonic()
        try:
            with urlopen(request, timeout=45) as response, output.open("wb") as target:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise ValueError(f"حجم فيديو X يتجاوز حد {MAX_MB}MB.")
                while True:
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                        raise ValueError("انتهت مهلة تنزيل فيديو X.")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ValueError(f"حجم فيديو X تجاوز حد {MAX_MB}MB.")
                    target.write(chunk)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        if not written:
            raise ValueError("أعاد مصدر X ملف فيديو فارغًا.")
        LOG.info("X public MP4 ready (height=%d, bytes=%d)", height, written)
        return output

    hls = next((item for item in candidates if item[3] == "hls"), None)
    if hls:
        _, _, manifest_url, _ = hls
        args = [
            "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
            "--no-warnings", "--no-cache-dir", "--no-part",
            "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
            "--concurrent-fragments", "4",
            "--max-filesize", str(MAX_BYTES), "--format", "best",
            "--merge-output-format", "mp4", "--output", str(folder / "nova-x.%(ext)s"),
            "--", manifest_url,
        ]
        try:
            with tempfile.TemporaryFile(mode="w+b") as error_output:
                process = subprocess.Popen(
                    args, cwd=str(folder), stdout=subprocess.DEVNULL,
                    stderr=error_output, start_new_session=True,
                )
                started = time.monotonic()
                while process.poll() is None:
                    if _folder_bytes(folder) > MAX_BYTES:
                        _stop_download_process(process)
                        raise ValueError(f"حجم فيديو HLS من X تجاوز حد {MAX_MB}MB.")
                    if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
                        _stop_download_process(process)
                        raise ValueError("انتهت مهلة تنزيل HLS من X.")
                    time.sleep(0.25)
                if _folder_bytes(folder) > MAX_BYTES:
                    raise ValueError(f"حجم فيديو HLS من X تجاوز حد {MAX_MB}MB.")
                if process.returncode:
                    error_output.seek(0)
                    stderr = error_output.read(16384).decode("utf-8", "replace")
                    reason, message = classify_ytdlp_error(stderr)
                    LOG.warning("X HLS download failed (exit=%d, reason=%s)",
                                process.returncode, reason)
                    raise ValueError(message)
        except subprocess.TimeoutExpired:
            raise ValueError("انتهت مهلة تنزيل HLS من X.") from None
        videos = [p for p in folder.iterdir() if p.is_file()
                  and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
        if len(videos) != 1 or not 0 < videos[0].stat().st_size <= MAX_BYTES:
            raise ValueError(f"ملف فيديو X غير صالح أو يتجاوز حد {MAX_MB}MB.")
        return videos[0]
    raise ValueError("لم يعثر X على مسار فيديو مدعوم.")


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
        "--concurrent-fragments", "4",
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
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def download_x_broadcast(url, folder):
    """Download an ended X broadcast replay with a hard on-disk size guard."""
    args = x_broadcast_download_args(url, folder)
    try:
        with tempfile.TemporaryFile(mode="w+b") as error_output:
            try:
                process = subprocess.Popen(
                    args, cwd=str(folder), stdout=subprocess.DEVNULL,
                    stderr=error_output, start_new_session=True,
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
                        f"حجم تسجيل البث تجاوز حد {MAX_MB}MB. لم نرسل ملفًا جزئيًا."
                    )
                if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
                    _stop_download_process(process)
                    raise ValueError(
                        "انتهت مهلة تنزيل تسجيل X. جرّب تسجيلًا أقصر."
                    )
                time.sleep(0.25)

            if _folder_bytes(folder) > MAX_BYTES:
                raise ValueError(
                    f"حجم تسجيل البث تجاوز حد {MAX_MB}MB. لم نرسل ملفًا جزئيًا."
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
        raise ValueError(f"حجم تسجيل X يتجاوز حد الإرسال البالغ {MAX_MB}MB.")
    LOG.info("X broadcast replay ready (bytes=%d)", selected.stat().st_size)
    return selected


def download_audio(url, folder):
    """Download one permitted SoundCloud track as bounded MP3 audio."""
    output = str(folder / "nova-audio.%(ext)s")
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
        "--no-warnings", "--no-cache-dir", "--no-part",
        "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--concurrent-fragments", "4",
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


def _api_connection(timeout):
    endpoint = urlsplit(API_BASE_URL)
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
        raise RuntimeError("Invalid Telegram API base URL")
    connection_type = (http.client.HTTPSConnection if endpoint.scheme == "https"
                       else http.client.HTTPConnection)
    connection = connection_type(endpoint.hostname, endpoint.port, timeout=timeout)
    base_path = endpoint.path.rstrip("/")
    return connection, base_path


def _send_multipart_cloud(chat_id, path, method, field, filename, content_type):
    """Stream a file to the standard cloud Bot API without loading it into RAM."""
    boundary = "nova_boundary_8d7e6f21"
    head = (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        + str(chat_id) + "\r\n"
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="' + field + '"; filename="' + filename + '"\r\n'
        "Content-Type: " + content_type + "\r\n\r\n"
    ).encode()
    tail = ("\r\n--" + boundary + "--\r\n").encode()
    conn, base_path = _api_connection(timeout=180)
    try:
        conn.putrequest("POST", base_path + "/bot" + TOKEN + "/" + method)
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        conn.putheader("Content-Length", str(len(head) + path.stat().st_size + len(tail)))
        conn.endheaders()
        conn.send(head)
        with path.open("rb") as source:
            while data := source.read(256 * 1024):
                conn.send(data)
        conn.send(tail)
        response = conn.getresponse()
        payload = response.read(8192)
        result = json.loads(payload)
        if response.status != 200 or not result.get("ok"):
            raise RuntimeError("Telegram upload rejected")
        return result.get("result")
    finally:
        conn.close()


def _send_local_file(chat_id, path, method, field):
    """Ask the local Bot API container to read a shared file:// path directly."""
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError("Telegram upload path is not a regular file")
    # The API container runs as uid 101 and receives gid 10001 as a supplemental
    # group. Keep the job directory traversable and the file group-readable.
    os.chmod(resolved.parent, 0o750)
    os.chmod(resolved, 0o640)
    return api_call(
        method,
        {"chat_id": chat_id, field: resolved.as_uri()},
        timeout=UPLOAD_TIMEOUT,
    )


def log_download_complete(source, path, started):
    """Log source-to-disk throughput separately from Telegram upload throughput."""
    size = path.stat().st_size
    elapsed = max(time.monotonic() - started, 0.001)
    LOG.info("Media download complete (source=%s, bytes=%d, seconds=%.2f, Mbps=%.2f)",
             source, size, elapsed, size * 8 / elapsed / 1_000_000)


def send_video(chat_id, path):
    """Send a video/document and log the measured upload duration."""
    started = time.monotonic()
    size = path.stat().st_size
    try:
        if LOCAL_API_MODE:
            result = _send_local_file(chat_id, path, "sendDocument", "document")
        else:
            result = _send_multipart_cloud(
                chat_id, path, "sendDocument", "document",
                "nova" + path.suffix.lower(), "application/octet-stream",
            )
        elapsed = max(time.monotonic() - started, 0.001)
        LOG.info("Telegram video upload complete (bytes=%d, seconds=%.2f, Mbps=%.2f, api=%s)",
                 size, elapsed, size * 8 / elapsed / 1_000_000,
                 "local" if LOCAL_API_MODE else "cloud")
        return result
    except Exception:
        LOG.exception("Telegram video upload failed (bytes=%d, seconds=%.2f, api=%s)",
                      size, time.monotonic() - started,
                      "local" if LOCAL_API_MODE else "cloud")
        raise


def send_audio(chat_id, path):
    """Send an MP3 and log the measured upload duration."""
    started = time.monotonic()
    size = path.stat().st_size
    try:
        if LOCAL_API_MODE:
            result = _send_local_file(chat_id, path, "sendAudio", "audio")
        else:
            result = _send_multipart_cloud(
                chat_id, path, "sendAudio", "audio", "nova.mp3", "audio/mpeg",
            )
        elapsed = max(time.monotonic() - started, 0.001)
        LOG.info("Telegram audio upload complete (bytes=%d, seconds=%.2f, Mbps=%.2f, api=%s)",
                 size, elapsed, size * 8 / elapsed / 1_000_000,
                 "local" if LOCAL_API_MODE else "cloud")
        return result
    except Exception:
        LOG.exception("Telegram audio upload failed (bytes=%d, seconds=%.2f, api=%s)",
                      size, time.monotonic() - started,
                      "local" if LOCAL_API_MODE else "cloud")
        raise


def process_media_job(chat_id, url):
    """Run one persisted media job and clean its temporary files."""
    safe_send(chat_id, "بدأ تنفيذ طلبك الآن. يتم تنزيل الملف ثم إرساله…")
    folder = None
    download_started = time.monotonic()
    source = "unknown"
    try:
        folder = Path(tempfile.mkdtemp(prefix="nova-", dir=str(WORKDIR)))
        host = (urlsplit(url).hostname or "").lower()
        is_soundcloud = host == "soundcloud.com" or host.endswith(".soundcloud.com")
        if is_soundcloud:
            source = "soundcloud"
            LOG.info("Media download started (source=%s)", source)
            file_path = download_audio(url, folder)
            log_download_complete(source, file_path, download_started)
            send_audio(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال الصوت ✅")
        elif is_x_broadcast_url(url):
            source = "x_broadcast"
            LOG.info("Media download started (source=%s)", source)
            file_path = download_x_broadcast(url, folder)
            log_download_complete(source, file_path, download_started)
            send_video(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال تسجيل البث ✅")
        elif x_status_id(url):
            source = "x_post"
            LOG.info("Media download started (source=%s)", source)
            file_path = download_x_status(url, folder)
            log_download_complete(source, file_path, download_started)
            send_video(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال فيديو X ✅")
        else:
            if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
                source = "youtube"
                LOG.info("Media download started (source=%s)", source)
                file_path = download_youtube(url, folder)
            else:
                source = "yt_dlp"
                LOG.info("Media download started (source=%s)", source)
                file_path = download(url, folder)
            log_download_complete(source, file_path, download_started)
            send_video(chat_id, file_path)
            safe_send(chat_id, "اكتمل إرسال الفيديو ✅")
        return True
    except ValueError as exc:
        LOG.warning("Media download/send stopped (source=%s, seconds=%.2f, reason=%s)",
                    source, time.monotonic() - download_started, type(exc).__name__)
        safe_send(chat_id, str(exc))
        return False
    except Exception as exc:
        LOG.exception("Download/send failure (source=%s, seconds=%.2f): %s",
                      source, time.monotonic() - download_started, type(exc).__name__)
        safe_send(chat_id, "حدث خطأ أثناء التحميل أو الإرسال. جرّب مرة أخرى لاحقًا.")
        return False
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


def queue_worker():
    """Process persisted jobs sequentially; interrupted jobs are retried at startup."""
    LOG.info("NOVA queue worker started (max_active=%d, per_user=%d)",
             MAX_QUEUE_SIZE, MAX_QUEUE_PER_USER)
    while True:
        try:
            job = claim_next_job()
        except sqlite3.Error:
            LOG.exception("Queue database read/claim failed")
            time.sleep(2)
            continue
        if job is None:
            time.sleep(1)
            continue

        LOG.info("Queue job started (job_id=%d, user_id=%d)",
                 job["id"], job["user_id"])
        succeeded = process_media_job(job["chat_id"], job["url"])
        while True:
            try:
                finish_job(job["id"], succeeded)
                LOG.info("Queue job finished (job_id=%d, status=%s)",
                         job["id"], "completed" if succeeded else "failed")
                break
            except sqlite3.Error:
                LOG.exception("Could not persist queue completion (job_id=%d)", job["id"])
                time.sleep(2)


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

    command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
    if command == "/id":
        safe_send(chat_id, "Telegram user ID: " + str(user_id))
        return
    if user_id not in allowed_ids():
        safe_send(chat_id, "هذا البوت خاص وغير متاح لهذا الحساب.")
        return
    if command in ("/start", "/help"):
        safe_send(
            chat_id,
            "NOVA Bot 🎬\nأرسل رابط YouTube أو SoundCloud أو فيديو X عام. "
            + "حد الملف: " + str(MAX_MB) + " MB. "
            + "يعمل NOVA على طلب واحد في كل مرة، ويمكنك متابعة دورك بالأمر /queue.",
        )
        return
    if command == "/queue":
        jobs = user_queue_snapshot(user_id)
        if not jobs:
            safe_send(chat_id, "ليس لديك طلبات نشطة في قائمة الانتظار.")
            return
        lines = ["طلباتك النشطة في NOVA:"]
        for job in jobs:
            if job["status"] == "processing":
                lines.append("• طلب قيد التنفيذ الآن.")
            else:
                lines.append("• طلب في الانتظار. ترتيبك الحالي: #" + str(job["position"]))
        safe_send(chat_id, "\n".join(lines))
        return

    url = extract_url(text)
    if not url or len(url) > 2048 or not permitted_url(url):
        safe_send(chat_id, "أرسل رابط HTTPS عامًا وصالحًا من YouTube أو SoundCloud أو X.")
        return
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        safe_send(chat_id, "تعذّر تسجيل الطلب. أعد إرسال الرابط.")
        return
    try:
        result = enqueue_job(update_id, user_id, chat_id, url)
    except sqlite3.Error:
        LOG.exception("Could not persist incoming media request")
        safe_send(chat_id, "تعذّر حفظ طلبك في قائمة الانتظار. حاول مرة أخرى بعد قليل.")
        return False

    if result.get("duplicate"):
        return
    if not result.get("accepted"):
        if result.get("reason") == "user_limit":
            safe_send(chat_id, "لديك بالفعل " + str(MAX_QUEUE_PER_USER)
                       + " طلبات نشطة. انتظر اكتمال أحدها ثم أرسل طلبًا جديدًا.")
        else:
            safe_send(chat_id, "قائمة الانتظار ممتلئة حاليًا. حاول مجددًا بعد اكتمال بعض الطلبات.")
        return

    position = result["position"]
    if position == 1:
        safe_send(chat_id, "تمت إضافة طلبك إلى قائمة NOVA. أنت التالي وسيبدأ التنفيذ قريبًا. لمتابعة دورك أرسل /queue.")
    else:
        safe_send(chat_id, "تمت إضافة طلبك إلى قائمة NOVA. ترتيبك الحالي: #"
                   + str(position) + ". لمتابعة دورك أرسل /queue.")


def main():
    if not TOKEN or ":" not in TOKEN or not allowed_ids():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS before startup")
    endpoint = urlsplit(API_BASE_URL)
    if (endpoint.scheme not in ("http", "https") or not endpoint.hostname
            or endpoint.username is not None or endpoint.password is not None):
        raise SystemExit("Invalid TELEGRAM_API_BASE_URL")
    if (not 1 <= MAX_MB <= MAX_SAFE_MB
            or not 30 <= DOWNLOAD_TIMEOUT <= 7200
            or not 30 <= UPLOAD_TIMEOUT <= 7200
            or not 1 <= MAX_QUEUE_SIZE <= 200
            or not 1 <= MAX_QUEUE_PER_USER <= MAX_QUEUE_SIZE):
        raise SystemExit("Invalid resource limits for the configured Telegram API mode")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if LOCAL_API_MODE:
        os.chmod(WORKDIR, 0o750)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_queue_db()
    worker = threading.Thread(target=queue_worker, name="nova-queue-worker", daemon=True)
    worker.start()
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
                # Persist the request before advancing the long-poll offset.
                # On a transient DB error Telegram will redeliver this update.
                if handle(update) is False:
                    break
                offset = update["update_id"] + 1
        except TelegramApiError as exc:
            errors += 1
            delay = polling_delay(exc.status, errors, exc.retry_after)
            LOG.warning("Telegram polling HTTP %d; next attempt in %d seconds",
                        exc.status, delay)
            time.sleep(delay)
        except (OSError, RuntimeError, ValueError, URLError, sqlite3.Error) as exc:
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
        print("API mode:", "local" if LOCAL_API_MODE else "cloud")
        print("Username:", (result or {}).get("username", "unknown"))
    except TelegramApiError as exc:
        print("Telegram getMe: HTTP", exc.status)
        if exc.retry_after is not None:
            print("Telegram retry_after (seconds):", exc.retry_after)
        raise SystemExit(1)
    except (OSError, ValueError) as exc:
        print("Telegram connection error:", type(exc).__name__)
        raise SystemExit(1)


def logout_cloud():
    """Deregister the bot from api.telegram.org before first local-mode login."""
    try:
        result = api_call("logOut", timeout=15, base_url=CLOUD_API_BASE_URL)
        if result is not True:
            raise ValueError("Telegram cloud logOut did not return true")
        print("Telegram cloud logOut: OK")
    except TelegramApiError as exc:
        print("Telegram cloud logOut: HTTP", exc.status)
        if exc.retry_after is not None:
            print("Telegram retry_after (seconds):", exc.retry_after)
        raise SystemExit(1)


def logout_local():
    """Deregister the bot from the configured local API before a cloud rollback."""
    if not LOCAL_API_MODE:
        raise SystemExit("TELEGRAM_LOCAL_API must be true to log out of the local API")
    try:
        result = api_call("logOut", timeout=15)
        if result is not True:
            raise ValueError("Local Telegram API logOut did not return true")
        print("Telegram local logOut: OK")
    except TelegramApiError as exc:
        print("Telegram local logOut: HTTP", exc.status)
        if exc.retry_after is not None:
            print("Telegram retry_after (seconds):", exc.retry_after)
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--check-telegram":
        diagnose()
    elif len(sys.argv) == 2 and sys.argv[1] == "--logout-cloud":
        logout_cloud()
    elif len(sys.argv) == 2 and sys.argv[1] == "--logout-local":
        logout_local()
    else:
        main()
