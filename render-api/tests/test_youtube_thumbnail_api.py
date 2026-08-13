import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import api
from app.schemas import YouTubeThumbnailRequest, YouTubeUploadRequest


class YouTubeThumbnailApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_automatically_applies_and_persists_thumbnail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "video.mp4").write_bytes(b"video")
            (output_dir / "thumbnail.jpg").write_bytes(b"thumbnail")
            saved_states = []

            with patch.object(api, "p_output", return_value=output_dir), patch.object(
                api, "upload_video_to_youtube", return_value="video-123"
            ), patch.object(api, "set_youtube_thumbnail") as set_thumbnail, patch.object(
                api, "read_project_state", return_value={"title": "Listing"}
            ), patch.object(
                api, "save_project_state", side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state))
            ):
                result = await api.youtube_upload(
                    "project-1",
                    YouTubeUploadRequest(title="Listing", profile="mandarin"),
                )

            self.assertEqual(result.videoId, "video-123")
            self.assertTrue(result.thumbnailApplied)
            set_thumbnail.assert_called_once_with(
                video_id="video-123",
                thumbnail_path=output_dir / "thumbnail.jpg",
                profile="mandarin",
            )
            self.assertTrue(saved_states[-1]["youtubeUpload"]["thumbnailApplied"])

    async def test_upload_stays_successful_when_thumbnail_needs_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "video.mp4").write_bytes(b"video")
            saved_states = []

            with patch.object(api, "p_output", return_value=output_dir), patch.object(
                api, "upload_video_to_youtube", return_value="video-456"
            ), patch.object(
                api, "set_youtube_thumbnail", side_effect=FileNotFoundError("thumbnail missing")
            ), patch.object(
                api, "read_project_state", return_value={"title": "Listing"}
            ), patch.object(
                api, "save_project_state", side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state))
            ):
                result = await api.youtube_upload(
                    "project-1",
                    YouTubeUploadRequest(title="Listing"),
                )

            self.assertEqual(result.videoId, "video-456")
            self.assertFalse(result.thumbnailApplied)
            self.assertEqual(result.thumbnailError, "YouTube thumbnail failed: thumbnail missing")
            self.assertEqual(saved_states[-1]["youtubeUpload"]["videoId"], "video-456")

    async def test_upload_explains_custom_thumbnail_eligibility_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "video.mp4").write_bytes(b"video")
            (output_dir / "thumbnail.jpg").write_bytes(b"thumbnail")
            permission_error = RuntimeError(
                "The authenticated user doesn't have permissions to upload and set custom video thumbnails."
            )

            with patch.object(api, "p_output", return_value=output_dir), patch.object(
                api, "upload_video_to_youtube", return_value="video-456"
            ), patch.object(
                api, "set_youtube_thumbnail", side_effect=permission_error
            ), patch.object(
                api, "read_project_state", return_value={"title": "Listing"}
            ), patch.object(api, "save_project_state"):
                result = await api.youtube_upload(
                    "project-1",
                    YouTubeUploadRequest(title="Listing"),
                )

            self.assertIn("Feature eligibility", result.thumbnailError)
            self.assertIn("Apply thumbnail again", result.thumbnailError)

    async def test_manual_thumbnail_uses_recorded_video_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "thumbnail.jpg").write_bytes(b"thumbnail")
            saved_states = []

            with patch.object(api, "p_output", return_value=output_dir), patch.object(
                api, "set_youtube_thumbnail"
            ) as set_thumbnail, patch.object(
                api,
                "read_project_state",
                return_value={"youtubeUpload": {"videoId": "recorded-789"}},
            ), patch.object(
                api, "save_project_state", side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state))
            ):
                result = await api.youtube_thumbnail(
                    "project-1",
                    YouTubeThumbnailRequest(profile="mandarin"),
                )

            self.assertEqual(result.videoId, "recorded-789")
            set_thumbnail.assert_called_once_with(
                video_id="recorded-789",
                thumbnail_path=output_dir / "thumbnail.jpg",
                profile="mandarin",
            )
            self.assertTrue(saved_states[-1]["youtubeUpload"]["thumbnailApplied"])


if __name__ == "__main__":
    unittest.main()
