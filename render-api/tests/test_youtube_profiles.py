import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import youtube_upload


class YouTubeProfileTests(unittest.TestCase):
    def test_mandarin_uses_a_separate_token_file(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"YOUTUBE_TOKEN_FILE": str(Path(temp_dir) / "youtube_token.json")},
            clear=False,
        ):
            self.assertEqual(youtube_upload._token_file_path("english"), Path(temp_dir) / "youtube_token.json")
            self.assertEqual(
                youtube_upload._token_file_path("mandarin"),
                Path(temp_dir) / "youtube_token_mandarin.json",
            )

    def test_authorization_state_identifies_the_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "YOUTUBE_STATE_FILE": str(Path(temp_dir) / "youtube_oauth_state.txt"),
                "YOUTUBE_REDIRECT_URI": "http://localhost:8082/v1/youtube/auth/callback",
            },
            clear=False,
        ), patch.object(youtube_upload, "_auth_flow") as auth_flow:
            flow = MagicMock()
            flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", None)
            auth_flow.return_value = flow

            result = youtube_upload.youtube_authorization_url("mandarin")
            state_path = Path(temp_dir) / "youtube_oauth_state_mandarin.txt"
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))["state"]

            self.assertEqual(result["profile"], "mandarin")
            self.assertTrue(saved_state.startswith("mandarin."))
            self.assertEqual(youtube_upload._profile_from_state(saved_state), "mandarin")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(youtube_upload.YouTubeUploadConfigurationError):
            youtube_upload.normalize_youtube_profile("cantonese")

    def test_loopback_bridge_removes_manual_callback_requirement(self):
        config = {"installed": {"client_id": "test"}}
        redirect_uri = "http://localhost:8082/v1/youtube/auth/callback"
        with patch.dict(os.environ, {"YOUTUBE_LOOPBACK_BRIDGE": "true"}, clear=False):
            self.assertFalse(youtube_upload._manual_callback_required(config, redirect_uri))

    def test_loopback_without_bridge_keeps_manual_callback_fallback(self):
        config = {"installed": {"client_id": "test"}}
        redirect_uri = "http://localhost:8082/v1/youtube/auth/callback"
        with patch.dict(os.environ, {"YOUTUBE_LOOPBACK_BRIDGE": "false"}, clear=False):
            self.assertTrue(youtube_upload._manual_callback_required(config, redirect_uri))


if __name__ == "__main__":
    unittest.main()
