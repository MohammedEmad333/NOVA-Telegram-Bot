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

    def test_parse_message(self):
        self.assertEqual(bot.extract_url("Hi https://youtu.be/123."), "https://youtu.be/123")
        self.assertIsNone(bot.extract_url("nothing"))

    def test_user_allowlist(self):
        with patch.object(bot, "ALLOWED_RAW", "123, 456, invalid"):
            self.assertEqual(bot.allowed_ids(), {123, 456})

    def test_unauthorized_user_cannot_download(self):
        with patch.object(bot, "ALLOWED_RAW", "123"), \
             patch.object(bot, "safe_send") as notify, \
             patch.object(bot, "download") as download:
            bot.handle({"message": {"from": {"id": 999}, "chat": {"id": 999},
                                    "text": "https://youtu.be/test"}})
            download.assert_not_called()
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
