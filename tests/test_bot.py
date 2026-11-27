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

    def test_newpipe_antibot_reason_is_not_misreported_as_login(self):
        payload = '{"ok":false,"reason":"youtube_anti_bot","error_type":"SignInConfirmNotBotException"}'
        result = bot.parse_newpipe_result(payload, 1)
        self.assertEqual(result["reason"], "youtube_anti_bot")
        self.assertNotEqual(result["reason"], "login_required")
    def test_newpipe_json_diagnostic_is_parsed_even_when_java_exits_nonzero(self):
        payload = '{"ok":false,"reason":"source_challenge","message":"safe"}'
        result = bot.parse_newpipe_result(payload, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "source_challenge")

    def test_newpipe_success_requires_zero_exit(self):
        payload = '{"ok":true,"video_url":"https://rr1.googlevideo.com/videoplayback"}'
        self.assertTrue(bot.parse_newpipe_result(payload, 0)["ok"])
        with self.assertRaises(ValueError):
            bot.parse_newpipe_result(payload, 1)

    def test_newpipe_invalid_output_is_rejected(self):
        for stdout in ("", "not json", "{}", '{"ok":"yes"}'):
            with self.assertRaises(ValueError):
                bot.parse_newpipe_result(stdout, 1)
    def test_x_tweet_without_video_has_specific_diagnostic(self):
        reason, message = bot.classify_ytdlp_error(
            "[twitter] 123: No video could be found in this tweet"
        )
        self.assertEqual(reason, "no_video_in_post")
        self.assertIn("فيديو مرفق", message)
        self.assertNotIn("https://", message)

    def test_soundcloud_audio_download_creates_bounded_mp3(self):
        import tempfile
        from pathlib import Path

        def fake_run(args, **kwargs):
            output = next(arg for arg in args if "nova-audio.%(ext)s" in arg)
            Path(kwargs["cwd"], "nova-audio.mp3").write_bytes(b"mp3-data")
            class Result:
                returncode = 0
                stderr = ""
            self.assertIn("--extract-audio", args)
            self.assertIn("mp3", args)
            return Result()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot.subprocess, "run", side_effect=fake_run):
            result = bot.download_audio(
                "https://soundcloud.com/example/track", Path(directory)
            )
            self.assertEqual(result.name, "nova-audio.mp3")
            self.assertEqual(result.read_bytes(), b"mp3-data")

    def test_x_status_id_strips_tracking_parameters_and_rejects_bad_hosts(self):
        self.assertEqual(
            bot.x_status_id("https://x.com/xmov/status/2123052267821826135?a=tracking&s=20"),
            "2123052267821826135",
        )
        self.assertEqual(
            bot.x_status_id("https://twitter.com/user/status/1234567890123456789"),
            "1234567890123456789",
        )
        self.assertIsNone(bot.x_status_id("https://x.com.evil.test/user/status/123456789012"))
        self.assertIsNone(bot.x_status_id("http://x.com/user/status/123456789012"))
        self.assertIsNone(bot.x_status_id("https://x.com/i/broadcasts/123456789012"))

    def test_x_media_url_requires_video_twitter_cdn_and_suffix(self):
        self.assertTrue(bot._safe_x_media_url(
            "https://video.twimg.com/ext_tw_video/123/pu/vid/avc1/720x1280/clip.mp4", ".mp4"
        ))
        self.assertTrue(bot._safe_x_media_url(
            "https://video.twimg.com/ext_tw_video/123/pu/pl/clip.m3u8", ".m3u8"
        ))
        self.assertFalse(bot._safe_x_media_url("https://evil.test/clip.mp4", ".mp4"))
        self.assertFalse(bot._safe_x_media_url("http://video.twimg.com/clip.mp4", ".mp4"))
        self.assertFalse(bot._safe_x_media_url("https://video.twimg.com/clip.mp4.evil", ".mp4"))

    def test_fx_twitter_metadata_parser_extracts_public_mp4_variants(self):
        status_id = "2123052267821826135"
        media_url = "https://video.twimg.com/ext_tw_video/123/pu/vid/avc1/720x1280/clip.mp4"
        root = {
            "code": 200,
            "tweet": {
                "id": status_id,
                "media": {"videos": [{
                    "height": 1280,
                    "formats": [{"url": media_url, "bitrate": 832000}],
                }]},
            },
        }
        candidates = bot._x_video_candidates(root, status_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][2], media_url)
        self.assertEqual(candidates[0][3], "mp4")

    def test_vx_twitter_backup_metadata_parser_checks_status_id(self):
        status_id = "2123052267821826135"
        media_url = "https://video.twimg.com/ext_tw_video/123/pu/vid/avc1/720x1280/clip.mp4"
        root = {
            "tweetID": status_id,
            "media_extended": [{
                "type": "video", "url": media_url,
                "size": {"width": 720, "height": 1280},
            }],
        }
        self.assertEqual(bot._x_video_candidates(root, status_id, True)[0][2], media_url)
        root["tweetID"] = "9999999999999999999"
        self.assertEqual(bot._x_video_candidates(root, status_id, True), [])

    def test_x_broadcast_url_is_recognized_only_on_trusted_hosts(self):
        self.assertTrue(bot.is_x_broadcast_url(
            "https://x.com/i/broadcasts/1jMJgkRXbqmJL"
        ))
        self.assertTrue(bot.is_x_broadcast_url(
            "https://twitter.com/i/broadcasts/1jMJgkRXbqmJL"
        ))
        self.assertFalse(bot.is_x_broadcast_url(
            "https://x.com/i/web/status/123"
        ))
        self.assertFalse(bot.is_x_broadcast_url(
            "https://x.com.evil.test/i/broadcasts/123"
        ))
        self.assertFalse(bot.is_x_broadcast_url(
            "http://x.com/i/broadcasts/123"
        ))

    def test_x_broadcast_download_prefers_480p_and_enforces_size_flag(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            args = bot.x_broadcast_download_args(
                "https://x.com/i/broadcasts/123", Path(directory)
            )
        self.assertIn("best[height<=480]/best[height<=720]/best", args)
        self.assertIn("--max-filesize", args)
        self.assertIn(str(bot.MAX_BYTES), args)
        self.assertIn("--merge-output-format", args)
        self.assertIn("mp4", args)

    def test_folder_bytes_counts_partial_and_final_outputs(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video.mp4.part").write_bytes(b"1234")
            (root / "video.mp4").write_bytes(b"567")
            self.assertEqual(bot._folder_bytes(root), 7)

    def test_local_api_connection_uses_private_compose_service(self):
        with patch.object(bot, "API_BASE_URL", "http://telegram-bot-api:8081"):
            connection, base_path = bot._api_connection(timeout=7)
            try:
                self.assertEqual(connection.host, "telegram-bot-api")
                self.assertEqual(connection.port, 8081)
                self.assertEqual(base_path, "")
                self.assertEqual(connection.timeout, 7)
            finally:
                connection.close()

    def test_local_file_upload_passes_shared_file_uri_to_api(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nova.mp4"
            path.write_bytes(b"test-video")
            with patch.object(bot, "api_call", return_value={"message_id": 42}) as api:
                result = bot._send_local_file(12345, path, "sendDocument", "document")
            self.assertEqual(result, {"message_id": 42})
            args, kwargs = api.call_args
            self.assertEqual(args[0], "sendDocument")
            self.assertEqual(args[1]["chat_id"], 12345)
            self.assertTrue(args[1]["document"].startswith("file://"))
            self.assertEqual(kwargs["timeout"], bot.UPLOAD_TIMEOUT)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)

    def test_cloud_api_connection_keeps_standard_endpoint_by_default(self):
        with patch.object(bot, "API_BASE_URL", "https://api.telegram.org"):
            connection, base_path = bot._api_connection(timeout=9)
            try:
                self.assertEqual(connection.host, "api.telegram.org")
                self.assertEqual(base_path, "")
            finally:
                connection.close()

    def test_local_api_connection_uses_private_compose_service(self):
        with patch.object(bot, "API_BASE_URL", "http://telegram-bot-api:8081"):
            connection, base_path = bot._api_connection(timeout=7)
            try:
                self.assertEqual(connection.host, "telegram-bot-api")
                self.assertEqual(connection.port, 8081)
                self.assertEqual(base_path, "")
                self.assertEqual(connection.timeout, 7)
            finally:
                connection.close()

    def test_local_file_upload_passes_shared_file_uri_to_api(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nova.mp4"
            path.write_bytes(b"test-video")
            with patch.object(bot, "api_call", return_value={"message_id": 42}) as api:
                result = bot._send_local_file(12345, path, "sendDocument", "document")
            self.assertEqual(result, {"message_id": 42})
            args, kwargs = api.call_args
            self.assertEqual(args[0], "sendDocument")
            self.assertEqual(args[1]["chat_id"], 12345)
            self.assertTrue(args[1]["document"].startswith("file://"))
            self.assertEqual(kwargs["timeout"], bot.UPLOAD_TIMEOUT)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o750)

    def test_cloud_api_connection_keeps_standard_endpoint_by_default(self):
        with patch.object(bot, "API_BASE_URL", "https://api.telegram.org"):
            connection, base_path = bot._api_connection(timeout=9)
            try:
                self.assertEqual(connection.host, "api.telegram.org")
                self.assertEqual(base_path, "")
            finally:
                connection.close()

    def test_generic_video_download_uses_bounded_hls_fragment_concurrency(self):
        import tempfile
        from pathlib import Path

        def fake_run(args, **kwargs):
            self.assertIn("--concurrent-fragments", args)
            self.assertEqual(args[args.index("--concurrent-fragments") + 1], "4")
            Path(kwargs["cwd"], "nova.mp4").write_bytes(b"video")
            class Result:
                returncode = 0
                stderr = ""
            return Result()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot.subprocess, "run", side_effect=fake_run):
            result = bot.download("https://x.com/example/video", Path(directory))
        self.assertEqual(result.name, "nova.mp4")

    def test_x_broadcast_uses_four_concurrent_fragments(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            args = bot.x_broadcast_download_args(
                "https://x.com/i/broadcasts/1234567890", Path(directory)
            )
        self.assertIn("--concurrent-fragments", args)
        self.assertEqual(args[args.index("--concurrent-fragments") + 1], "4")

    def test_soundcloud_audio_uses_four_concurrent_fragments(self):
        import tempfile
        from pathlib import Path

        def fake_run(args, **kwargs):
            self.assertIn("--concurrent-fragments", args)
            self.assertEqual(args[args.index("--concurrent-fragments") + 1], "4")
            Path(kwargs["cwd"], "nova-audio.mp3").write_bytes(b"audio")
            class Result:
                returncode = 0
                stderr = ""
            return Result()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot.subprocess, "run", side_effect=fake_run):
            result = bot.download_audio(
                "https://soundcloud.com/example/track", Path(directory)
            )
        self.assertEqual(result.name, "nova-audio.mp3")

    def test_download_throughput_log_reports_bytes_duration_and_mbps(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.mp4"
            path.write_bytes(b"x" * 125000)
            with patch.object(bot, "LOG") as logger:
                bot.log_download_complete("x_broadcast", path, bot.time.monotonic() - 2)
            message = logger.info.call_args.args[0]
            self.assertIn("source=%s", message)
            self.assertEqual(logger.info.call_args.args[1], "x_broadcast")
            self.assertEqual(logger.info.call_args.args[2], 125000)
            self.assertGreater(logger.info.call_args.args[3], 0)
            self.assertGreater(logger.info.call_args.args[4], 0)

    def test_queue_persists_fifo_jobs_and_rejects_duplicates(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "QUEUE_DB_PATH", Path(directory) / "queue.sqlite3"), \
             patch.object(bot, "MAX_QUEUE_SIZE", 3), \
             patch.object(bot, "MAX_QUEUE_PER_USER", 2):
            bot.init_queue_db()
            first = bot.enqueue_job(100, 10, 10, "https://x.com/a/status/1234567890")
            second = bot.enqueue_job(101, 20, 20, "https://x.com/b/status/1234567891")
            duplicate = bot.enqueue_job(100, 10, 10, "https://x.com/a/status/1234567890")
            self.assertEqual(first["position"], 1)
            self.assertEqual(second["position"], 2)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(bot.claim_next_job()["update_id"], 100)
            snapshot = bot.user_queue_snapshot(20)
            self.assertEqual(snapshot[0]["position"], 2)
            bot.finish_job(first["job_id"], True)
            self.assertEqual(bot.claim_next_job()["update_id"], 101)

    def test_queue_enforces_global_and_per_user_limits(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "QUEUE_DB_PATH", Path(directory) / "queue.sqlite3"), \
             patch.object(bot, "MAX_QUEUE_SIZE", 2), \
             patch.object(bot, "MAX_QUEUE_PER_USER", 1):
            bot.init_queue_db()
            self.assertTrue(bot.enqueue_job(1, 10, 10, "https://x.com/a/status/1234567890")["accepted"])
            self.assertEqual(bot.enqueue_job(2, 10, 10, "https://x.com/a/status/1234567891")["reason"], "user_limit")
            self.assertTrue(bot.enqueue_job(3, 20, 20, "https://x.com/b/status/1234567892")["accepted"])
            self.assertEqual(bot.enqueue_job(4, 30, 30, "https://x.com/c/status/1234567893")["reason"], "queue_full")

    def test_queue_recovers_processing_jobs_after_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "QUEUE_DB_PATH", Path(directory) / "queue.sqlite3"):
            bot.init_queue_db()
            job = bot.enqueue_job(55, 10, 10, "https://x.com/a/status/1234567890")
            bot.claim_next_job()
            bot.init_queue_db()
            connection = bot.queue_connection()
            try:
                row = connection.execute(
                    "SELECT status, started_at FROM queue_jobs WHERE id=?",
                    (job["job_id"],),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["started_at"])

    def test_queue_database_is_created_with_private_permissions(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "QUEUE_DB_PATH", Path(directory) / "queue.sqlite3"):
            bot.init_queue_db()
            self.assertEqual(bot.QUEUE_DB_PATH.stat().st_mode & 0o777, 0o600)

    def test_queue_status_command_only_reads_authorized_user_jobs(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bot, "QUEUE_DB_PATH", Path(directory) / "queue.sqlite3"), \
             patch.object(bot, "ALLOWED_RAW", "10"), \
             patch.object(bot, "safe_send") as notify:
            bot.init_queue_db()
            bot.enqueue_job(1, 10, 10, "https://x.com/a/status/1234567890")
            bot.enqueue_job(2, 20, 20, "https://x.com/b/status/1234567891")
            bot.handle({"update_id": 3, "message": {
                "from": {"id": 10}, "chat": {"id": 10, "type": "private"},
                "text": "/queue",
            }})
            notify.assert_called_once()
            self.assertIn("طلباتك النشطة", notify.call_args.args[1])
            self.assertNotIn("1234567891", notify.call_args.args[1])

    def test_queue_db_failure_does_not_acknowledge_update(self):
        with patch.object(bot, "ALLOWED_RAW", "10"), \
             patch.object(bot, "safe_send"), \
             patch.object(bot, "enqueue_job", side_effect=__import__("sqlite3").OperationalError("busy")):
            result = bot.handle({"update_id": 9, "message": {
                "from": {"id": 10}, "chat": {"id": 10, "type": "private"},
                "text": "https://x.com/a/status/1234567890",
            }})
        self.assertIs(result, False)

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
