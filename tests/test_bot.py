import unittest
from unittest.mock import patch
import bot


class SecurityTests(unittest.TestCase):
    def test_only_known_https_domains(self):
        good = [
            "https://www.youtube.com/watch?v=1",
            "https://youtu.be/abc",
            "https://m.soundcloud.com/user/track",
            "https://x.com/test/status/123",
        ]
        bad = [
            "http://youtube.com/watch?v=1",
            "https://youtube.com.evil.test/watch",
            "https://127.0.0.1/video.mp4",
            "https://10.0.0.1/",
            "https://localhost/",
            "https://example.com/",
            "file:///etc/passwd",
            "https://user:pass@youtube.com/a",
            "https://youtube.com:444/a",
        ]
        for url in good:
            self.assertTrue(bot.permitted_url(url), url)
        for url in bad:
            self.assertFalse(bot.permitted_url(url), url)

    def test_ytdlp_error_classification_is_safe_and_actionable(self):
        cases = [
            ("Sign in to confirm you're not a bot", "youtube_verification"),
            ("No supported JavaScript runtime", "javascript_runtime_missing"),
            ("HTTP Error 429: Too Many Requests", "source_rate_limited"),
            ("HTTP Error 403: Forbidden", "source_forbidden"),
            ("This video is private", "private_video"),
            ("Requested format is not available", "no_compatible_format"),
            ("ffmpeg not found", "ffmpeg_missing"),
            ("Unsupported URL", "unsupported_url"),
            ("random upstream error", "unknown"),
        ]
        for detail, expected in cases:
            reason, message = bot.classify_ytdlp_error(detail)
            self.assertEqual(reason, expected)
            self.assertTrue(message)
            self.assertNotIn("https://", message)

    def test_extractor_media_urls_are_limited_to_google_video_cdn(self):
        self.assertTrue(bot.permitted_media_url("https://rr1---sn.googlevideo.com/videoplayback"))
        self.assertFalse(bot.permitted_media_url("https://youtube.com/watch?v=1"))
        self.assertFalse(bot.permitted_media_url("http://rr1---sn.googlevideo.com/video"))
        self.assertFalse(bot.permitted_media_url("https://googlevideo.com.evil.test/video"))
        self.assertFalse(bot.permitted_media_url("https://user@googlevideo.com/video"))

    def test_parse_message(self):
        self.assertEqual(bot.extract_url("Hi https://youtu.be/123."), "https://youtu.be/123")
        self.assertIsNone(bot.extract_url("nothing"))

    def test_user_allowlist(self):
        with patch.object(bot, "ALLOWED_RAW", "123, 456, invalid"):
            self.assertEqual(bot.allowed_ids(), {123, 456})

    def test_group_chat_is_ignored_even_for_authorized_user(self):
        with patch.object(bot, "ALLOWED_RAW", "123"), \
             patch.object(bot, "safe_send") as notify, \
             patch.object(bot, "download") as download:
            bot.handle({"message": {"from": {"id": 123},
                                    "chat": {"id": -100987, "type": "supergroup"},
                                    "text": "https://youtu.be/test"}})
            download.assert_not_called()
            notify.assert_not_called()

    def test_retry_after_is_parsed_from_json_or_header(self):
        self.assertEqual(bot.retry_after_seconds(body='{"parameters":{"retry_after":150}}'), 150)
        self.assertEqual(bot.retry_after_seconds(headers={"Retry-After": "80"}), 80)
        self.assertEqual(bot.retry_after_seconds(headers={"Retry-After": "80"},
                         body='{"parameters":{"retry_after":150}}'), 150)
        self.assertIsNone(bot.retry_after_seconds(body='not json'))
        self.assertIsNone(bot.retry_after_seconds(body='{"parameters":{"retry_after":-1}}'))

    def test_rate_limit_backoff_respects_wait(self):
        self.assertEqual(bot.polling_delay(429, 1, 200), 202)
        self.assertEqual(bot.polling_delay(429, 1), 30)
        self.assertEqual(bot.polling_delay(429, 2), 60)
        self.assertEqual(bot.polling_delay(429, 30), 900)
        self.assertEqual(bot.polling_delay(409, 1), 300)
        self.assertEqual(bot.polling_delay(401, 1), 900)

    def test_api_call_sanitizes_http_error(self):
        from io import BytesIO
        from urllib.error import HTTPError
        exc = HTTPError("https://api.telegram.org/botsecret/getUpdates", 429,
                        "Too Many Requests", {"Retry-After": "120"},
                        BytesIO(b'{"ok":false,"parameters":{"retry_after":130}}'))
        with patch.object(bot, "TOKEN", "testsecret"), \
             patch.object(bot, "urlopen", side_effect=exc):
            with self.assertRaises(bot.TelegramApiError) as error:
                bot.api_call("getUpdates")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.retry_after, 130)
        self.assertNotIn("testsecret", str(error.exception))

    def test_unauthorized_user_cannot_download(self):
        with patch.object(bot, "ALLOWED_RAW", "123"), \
             patch.object(bot, "safe_send") as notify, \
             patch.object(bot, "download") as download:
            bot.handle({"message": {"from": {"id": 999},
                                    "chat": {"id": 999, "type": "private"},
                                    "text": "https://youtu.be/test"}})
            download.assert_not_called()
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
