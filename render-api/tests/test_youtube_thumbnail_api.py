import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import api
from app.schemas import YouTubeMetadataRequest, YouTubeThumbnailRequest, YouTubeUploadRequest


class YouTubeThumbnailApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_regenerate_thumbnail_creates_title_card_without_youtube_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            image_dir = input_dir / "images"
            output_dir = root / "output"
            image_dir.mkdir(parents=True)
            output_dir.mkdir()
            (image_dir / "front.jpg").write_bytes(b"image")
            (input_dir / "scenes.json").write_text(json.dumps({"scenes": [{"images": ["front.jpg"]}]}))
            thumbnail = output_dir / "thumbnail.jpg"
            saved_states = []

            def create_thumbnail(**kwargs):
                thumbnail.write_bytes(b"thumbnail")
                return root / "work" / "intro.mp4", thumbnail

            with patch.object(api, "p_input", return_value=input_dir), patch.object(
                api, "p_output", return_value=output_dir
            ), patch.object(api, "read_project_state", return_value={"title": "Listing title"}), patch.object(
                api, "_resolve_leader_template", return_value=root / "leader.png"
            ), patch.object(api, "_resolve_title_font", return_value=root / "font.ttf"), patch.object(
                api, "_create_intro_card_assets", side_effect=create_thumbnail
            ) as create_card, patch.object(
                api, "save_project_state", side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state))
            ):
                result = await api.regenerate_title_card_thumbnail("project-1")

            self.assertEqual(result["filename"], "thumbnail.jpg")
            self.assertTrue(thumbnail.exists())
            self.assertEqual(create_card.call_args.kwargs["title"], "Listing title")
            self.assertEqual(create_card.call_args.kwargs["background_image"], image_dir / "front.jpg")
            self.assertEqual(saved_states[-1]["thumbnailFilename"], "thumbnail.jpg")

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

    async def test_metadata_update_uses_recorded_video_and_saves_description(self):
        saved_states = []
        with patch.object(
            api,
            "read_project_state",
            return_value={"title": "Listing", "youtubeUpload": {"videoId": "recorded-789"}},
        ), patch.object(api, "update_youtube_video_metadata") as update_metadata, patch.object(
            api,
            "save_project_state",
            side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state)),
        ):
            result = await api.youtube_metadata(
                "project-1",
                YouTubeMetadataRequest(
                    title="Listing tour",
                    description="A complete description.",
                    tags=["Houston"],
                    profile="english",
                ),
            )

        self.assertEqual(result.videoId, "recorded-789")
        update_metadata.assert_called_once_with(
            video_id="recorded-789",
            title="Listing tour",
            description="A complete description.",
            tags=["Houston"],
            category_id="22",
            privacy_status="private",
            made_for_kids=False,
            profile="english",
        )
        self.assertEqual(saved_states[-1]["youtubeDescription"], "A complete description.")

    async def test_metadata_update_explains_upload_only_oauth_scope(self):
        permission_error = RuntimeError(
            "Request had insufficient authentication scopes: insufficientPermissions"
        )
        with patch.object(
            api,
            "read_project_state",
            return_value={"youtubeUpload": {"videoId": "recorded-789"}},
        ), patch.object(
            api,
            "update_youtube_video_metadata",
            side_effect=permission_error,
        ):
            with self.assertRaises(api.HTTPException) as raised:
                await api.youtube_metadata(
                    "project-1",
                    YouTubeMetadataRequest(title="Listing tour", description="Complete description."),
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Connect YouTube", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
