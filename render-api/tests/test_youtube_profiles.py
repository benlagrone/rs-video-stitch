import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import youtube_upload


class YouTubeProfileTests(unittest.TestCase):
    def test_oauth_requests_upload_and_metadata_scopes(self):
        self.assertIn("https://www.googleapis.com/auth/youtube.upload", youtube_upload.SCOPES)
        self.assertIn("https://www.googleapis.com/auth/youtube.force-ssl", youtube_upload.SCOPES)

    def test_auth_status_reports_upload_only_token_needs_reconnect(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"YOUTUBE_TOKEN_FILE": str(Path(temp_dir) / "youtube_token.json")},
            clear=False,
        ), patch.object(youtube_upload, "_credentials_from_token_file", return_value=MagicMock()):
            token_path = Path(temp_dir) / "youtube_token.json"
            token_path.write_text(
                json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.upload"]}),
                encoding="utf-8",
            )
            status = youtube_upload.youtube_auth_status("english")

        self.assertTrue(status["authenticated"])
        self.assertFalse(status["metadataAuthorized"])

    def test_existing_token_load_preserves_its_granted_scopes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "youtube_token.json"
            token_path.write_text("{}", encoding="utf-8")
            credentials_class = MagicMock()
            credentials = MagicMock(expired=False)
            credentials_class.from_authorized_user_file.return_value = credentials
            with patch.object(
                youtube_upload,
                "_google_modules",
                return_value={"Credentials": credentials_class},
            ):
                result = youtube_upload._credentials_from_token_file(token_path)

        self.assertIs(result, credentials)
        credentials_class.from_authorized_user_file.assert_called_once_with(str(token_path))

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

    def test_animals_uses_a_separate_token_file(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"YOUTUBE_TOKEN_FILE": str(Path(temp_dir) / "youtube_token.json")},
            clear=False,
        ):
            self.assertEqual(
                youtube_upload._token_file_path("animals"),
                Path(temp_dir) / "youtube_token_animals.json",
            )
            self.assertEqual(youtube_upload._token_file_path("bible"), Path(temp_dir) / "youtube_token_animals.json")

    def test_channel_catalog_returns_every_profile_with_live_identity_when_connected(self):
        status_by_profile = {
            "english": {"profile": "english", "authenticated": True, "metadataAuthorized": True},
            "mandarin": {"profile": "mandarin", "authenticated": False, "metadataAuthorized": False},
            "animals": {"profile": "animals", "authenticated": False, "metadataAuthorized": False},
        }
        youtube = MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "UC_grGcaDW3AUszA8_MVGyJg",
                    "snippet": {
                        "title": "LeCrown Properties",
                        "customUrl": "@lecrownproperties",
                        "thumbnails": {"default": {"url": "https://example.test/lecrown.jpg"}},
                    },
                }
            ]
        }
        with patch.object(
            youtube_upload,
            "youtube_auth_status",
            side_effect=lambda profile: status_by_profile[profile],
        ), patch.object(youtube_upload, "authenticate_youtube", return_value=youtube):
            catalog = youtube_upload.youtube_channel_catalog(force=True)

        self.assertEqual([channel["profile"] for channel in catalog], ["english", "mandarin", "animals"])
        self.assertEqual(catalog[0]["channelName"], "LeCrown Properties")
        self.assertEqual(catalog[0]["iconUrl"], "https://example.test/lecrown.jpg")
        self.assertTrue(catalog[0]["matchesExpectedChannel"])
        self.assertEqual(catalog[1]["channelName"], "皇冠物业")
        self.assertEqual(catalog[2]["channelName"], "Animals")
        self.assertIn("animal-safari-kids.png", catalog[2]["iconUrl"])

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

    def test_set_thumbnail_uses_selected_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail_path = Path(temp_dir) / "thumbnail.jpg"
            thumbnail_path.write_bytes(b"jpeg")
            media_upload = MagicMock()
            youtube = MagicMock()
            with patch.object(
                youtube_upload,
                "_google_modules",
                return_value={"MediaFileUpload": media_upload},
            ), patch.object(
                youtube_upload,
                "authenticate_youtube",
                return_value=youtube,
            ) as authenticate:
                youtube_upload.set_youtube_thumbnail(
                    video_id="video-123",
                    thumbnail_path=thumbnail_path,
                    profile="mandarin",
                )

            authenticate.assert_called_once_with("mandarin")
            media_upload.assert_called_once_with(
                str(thumbnail_path),
                mimetype="image/jpeg",
                resumable=False,
            )
            youtube.thumbnails.return_value.set.assert_called_once_with(
                videoId="video-123",
                media_body=media_upload.return_value,
            )
            youtube.thumbnails.return_value.set.return_value.execute.assert_called_once_with()

    def test_set_thumbnail_rejects_files_larger_than_two_mb(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail_path = Path(temp_dir) / "thumbnail.png"
            thumbnail_path.write_bytes(b"x" * 2_000_001)

            with self.assertRaisesRegex(ValueError, "2 MB or smaller"):
                youtube_upload.set_youtube_thumbnail(
                    video_id="video-123",
                    thumbnail_path=thumbnail_path,
                )

    def test_update_metadata_uses_selected_profile(self):
        youtube = MagicMock()
        with patch.object(
            youtube_upload,
            "authenticate_youtube",
            return_value=youtube,
        ) as authenticate:
            youtube_upload.update_youtube_video_metadata(
                video_id="video-123",
                title="Listing tour",
                description="A complete property description.",
                tags=["Houston", "real estate"],
                privacy_status="unlisted",
                profile="mandarin",
            )

        authenticate.assert_called_once_with("mandarin")
        youtube.videos.return_value.update.assert_called_once_with(
            part="snippet,status",
            body={
                "id": "video-123",
                "snippet": {
                    "title": "Listing tour",
                    "description": "A complete property description.",
                    "tags": ["Houston", "real estate"],
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": "unlisted",
                    "selfDeclaredMadeForKids": False,
                },
            },
        )
        youtube.videos.return_value.update.return_value.execute.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
