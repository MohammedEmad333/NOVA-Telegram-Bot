"""Private, single-worker Telegram video bot. No third-party Telegram SDK required."""
import http.client
import json
import logging
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


def api_call(method, params=None, timeout=45):
    payload = json.dumps(params or {}).encode()
    req = Request("https://" + API_HOST + "/bot" + TOKEN + "/" + method,
                  data=payload, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram returned an error for " + method)
    return result.get("result")


def message(chat_id, text):
    return api_call("sendMessage", {"chat_id": chat_id, "text": text})


def safe_send(chat_id, text):
    try:
        message(chat_id, text)
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.warning("Could not send message: %s", type(exc).__name__)


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
        raise ValueError("انتهت مهلة التحميل. جرّب فيديو أقصر.")
    if result.returncode:
        LOG.warning("yt-dlp failed (exit code %d)", result.returncode)
        raise ValueError("تعذّر تحميل الفيديو. الرابط قد يكون غير مدعوم أو الملف كبيرًا.")
    videos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in
              (".mp4", ".mkv", ".webm", ".mov")]
    if len(videos) != 1:
        raise ValueError("لم أتمكن من تجهيز ملف فيديو صالح.")
    selected = videos[0]
    if not 0 < selected.stat().st_size <= MAX_BYTES:
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
        'Content-Disposition: form-data; name="video"; filename="nova.mp4"\r\n'
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
    chat_id = (entry.get("chat") or {}).get("id")
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
    while True:
        try:
            updates = api_call("getUpdates", {
                "offset": offset, "timeout": 25, "limit": 10,
                "allowed_updates": ["message"],
            }, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                handle(update)
        except (OSError, RuntimeError, ValueError, HTTPError, URLError) as exc:
            LOG.warning("Polling error: %s", type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
