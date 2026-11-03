import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app import tiktok_upload


class _Response:
    def __init__(self, payload, ok=True, text=""):
        self.payload = payload
        self.ok = ok
        self.text = text

    def json(self):
        return self.payload


class TikTokUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.video = Path(self.temp_dir.name) / "video.mp4"
        self.video.write_bytes(b"video bytes")
        for name in ("post", "put"):
            guard = patch.object(tiktok_upload.requests, name, side_effect=AssertionError("Unexpected provider request"))
            guard.start()
            self.addCleanup(guard.stop)
        probe = patch.object(tiktok_upload.subprocess, "run", return_value=SimpleNamespace(stdout="12.0"))
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def _creator(self, **overrides):
        return {
            "username": "lecrown", "privacyLevelOptions": ["SELF_ONLY"],
            "commentDisabled": False, "duetDisabled": False, "stitchDisabled": False,
            "maxVideoPostDurationSec": 180, **overrides,
        }

    def _publish(self, **overrides):
        return tiktok_upload.publish_video_to_tiktok(**{
            "video_path": self.video, "caption": "Tour #Houston", "privacy_level": "SELF_ONLY",
            "is_aigc": True, **overrides,
        })

    def test_authorization_url_uses_direct_post_scopes_and_state(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {
            "TIKTOK_CLIENT_KEY": "client-key",
            "TIKTOK_CLIENT_SECRET": "client-secret",
            "TIKTOK_REDIRECT_URI": "https://example.test/v1/tiktok/auth/callback",
            "TIKTOK_STATE_FILE": str(Path(temp_dir) / "state.json"),
        }, clear=False):
            result = tiktok_upload.tiktok_authorization_url()

        query = parse_qs(urlparse(result["authUrl"]).query)
        self.assertEqual(query["client_key"], ["client-key"])
        self.assertIn("video.publish", query["scope"][0])
        self.assertTrue(query["state"][0])

    def test_publish_uploads_private_video_in_chunks_and_marks_organic_content(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {
            "TIKTOK_TOKEN_FILE": str(Path(temp_dir) / "token.json"),
        }, clear=False):
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.write_bytes(b"video bytes")
            (root / "token.json").write_text(json.dumps({"access_token": "token", "expires_in": 3600, "obtained_at": 9999999999}))
            posts = [
                _Response({"data": {"creator_username": "lecrown", "privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 180}, "error": {"code": "ok"}}),
                _Response({"data": {"upload_url": "https://upload.test/video", "publish_id": "publish-123"}, "error": {"code": "ok"}}),
            ]
            with patch.object(tiktok_upload.requests, "post", side_effect=posts) as post, patch.object(
                tiktok_upload.requests, "put", return_value=_Response({})
            ) as put:
                result = tiktok_upload.publish_video_to_tiktok(
                    video_path=video,
                    caption="Tour #Houston",
                    privacy_level="SELF_ONLY",
                    is_aigc=True,
                    brand_organic=True,
                )

        self.assertEqual(result["publishId"], "publish-123")
        init_payload = post.call_args_list[1].kwargs["json"]
        self.assertTrue(init_payload["post_info"]["brand_organic_toggle"])
        self.assertTrue(init_payload["post_info"]["is_aigc"])
        self.assertEqual(init_payload["source_info"]["source"], "FILE_UPLOAD")
        self.assertEqual(put.call_args.kwargs["headers"]["Content-Range"], "bytes 0-10/11")

    def test_upload_merges_trailing_bytes_into_the_final_chunk(self):
        chunk_size = tiktok_upload.TIKTOK_UPLOAD_CHUNK_SIZE
        self.video.write_bytes(b"x" * (2 * chunk_size + 123))
        initialized = {"upload_url": "https://upload.test/video", "publish_id": "publish-123"}
        with patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator()), patch.object(
            tiktok_upload, "_api_post", return_value=initialized
        ) as init, patch.object(tiktok_upload.requests, "put", return_value=_Response({})) as put:
            result = self._publish()

        self.assertEqual(result["status"], "PROCESSING")
        self.assertEqual(init.call_args.args[1]["source_info"]["total_chunk_count"], 2)
        self.assertEqual([len(call.kwargs["data"]) for call in put.call_args_list], [chunk_size, chunk_size + 123])
        self.assertEqual(put.call_args_list[-1].kwargs["headers"]["Content-Range"], f"bytes {chunk_size}-{2 * chunk_size + 122}/{2 * chunk_size + 123}")

    def test_empty_video_is_rejected_without_a_provider_request(self):
        self.video.write_bytes(b"")
        with self.assertRaisesRegex(tiktok_upload.TikTokUploadConfigurationError, "empty"):
            self._publish()
        self.probe.assert_not_called()

    def test_oversize_video_is_rejected_without_probing_or_uploading(self):
        with self.video.open("wb") as video:
            video.truncate(tiktok_upload.TIKTOK_MAX_VIDEO_SIZE + 1)
        with self.assertRaisesRegex(tiktok_upload.TikTokUploadConfigurationError, "4 GB"):
            self._publish()
        self.probe.assert_not_called()

    def test_duration_limit_is_checked_before_initializing_a_post(self):
        self.probe.return_value.stdout = "181"
        with patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator()), patch.object(
            tiktok_upload, "_api_post"
        ) as init, self.assertRaisesRegex(tiktok_upload.TikTokUploadConfigurationError, "up to 180 seconds"):
            self._publish()
        init.assert_not_called()
        self.assertEqual(self.probe.call_args.args[0][0], "ffprobe")

    def test_invalid_duration_is_rejected_without_a_provider_request(self):
        for duration in ("N/A", "nan", "0"):
            with self.subTest(duration=duration):
                self.probe.return_value.stdout = duration
                with self.assertRaises(tiktok_upload.TikTokUploadConfigurationError):
                    self._publish()

    def test_missing_creator_limit_and_disallowed_privacy_do_not_initialize(self):
        for creator in (self._creator(maxVideoPostDurationSec=None), self._creator(privacyLevelOptions=["PUBLIC_TO_EVERYONE"])):
            with self.subTest(creator=creator), patch.object(tiktok_upload, "tiktok_creator_info", return_value=creator), patch.object(
                tiktok_upload, "_api_post"
            ) as init, self.assertRaises(tiktok_upload.TikTokUploadConfigurationError):
                self._publish()
            init.assert_not_called()

    def test_account_mismatch_does_not_initialize_a_post(self):
        with patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator(username="different")), patch.object(
            tiktok_upload, "_api_post"
        ) as init, self.assertRaisesRegex(tiktok_upload.TikTokUploadConfigurationError, "account changed"):
            self._publish(expected_username="lecrown")
        init.assert_not_called()

    def test_interactions_default_off_and_business_disclosure_is_explicit(self):
        initialized = {"upload_url": "https://upload.test/video", "publish_id": "publish-123"}
        with patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator()), patch.object(
            tiktok_upload, "_api_post", return_value=initialized
        ) as init, patch.object(tiktok_upload.requests, "put", return_value=_Response({})):
            self._publish()
        post_info = init.call_args.args[1]["post_info"]
        self.assertTrue(all(post_info[field] for field in ("disable_comment", "disable_duet", "disable_stitch")))
        self.assertFalse(post_info["brand_organic_toggle"])

    def test_creator_disabled_flags_override_requested_interactions(self):
        initialized = {"upload_url": "https://upload.test/video", "publish_id": "publish-123"}
        with patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator(commentDisabled=True, stitchDisabled=True)), patch.object(
            tiktok_upload, "_api_post", return_value=initialized
        ) as init, patch.object(tiktok_upload.requests, "put", return_value=_Response({})):
            self._publish(allow_comment=True, allow_duet=True, allow_stitch=True, brand_organic=True)
        post_info = init.call_args.args[1]["post_info"]
        self.assertTrue(post_info["disable_comment"])
        self.assertTrue(post_info["disable_stitch"])
        self.assertFalse(post_info["disable_duet"])
        self.assertTrue(post_info["brand_organic_toggle"])

    def test_transfer_failure_retains_publish_id_for_status_recovery(self):
        initialized = {"upload_url": "https://upload.test/video", "publish_id": "publish-123"}
        for result in (_Response({}, ok=False, text="Rejected"), tiktok_upload.requests.Timeout("signed upload url")):
            with self.subTest(result=result), patch.object(tiktok_upload, "tiktok_creator_info", return_value=self._creator()), patch.object(
                tiktok_upload, "_api_post", return_value=initialized
            ), patch.object(tiktok_upload.requests, "put", side_effect=result if isinstance(result, Exception) else None, return_value=result), self.assertRaises(
                tiktok_upload.TikTokUploadTransferError
            ) as raised:
                self._publish()
            self.assertEqual(raised.exception.publish_id, "publish-123")
            self.assertEqual(raised.exception.status, "UPLOAD_FAILED")
            self.assertNotIn("signed upload url", str(raised.exception))
