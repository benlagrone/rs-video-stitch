import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import api
from app.schemas import TikTokPublishRequest
from app.tiktok_upload import TikTokUploadTransferError


class TikTokApiTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return TikTokPublishRequest(
            filename="tour.mp4", caption="2438 White Oak #Houston",
            privacyLevel="SELF_ONLY", expectedCreatorUsername="lecrown",
            brandOrganic=True,
        )

    def test_publish_requires_explicit_privacy_and_reviewed_account(self):
        with self.assertRaises(ValidationError):
            TikTokPublishRequest(caption="A tour")

    async def test_publish_persists_selected_video_without_replacing_other_project_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            (output / "tour.mp4").write_bytes(b"video")
            state = {"title": "2438 White Oak", "sceneTitles": {"1": "Living room"}}
            with patch.object(api, "p_output", return_value=output), patch.object(
                api, "read_project_state", return_value=state
            ), patch.object(api, "save_project_state") as save, patch.object(
                api, "publish_video_to_tiktok", return_value={"publishId": "post-1", "status": "PROCESSING"}
            ) as publish:
                result = await api.tiktok_publish("project", self.request())

        self.assertEqual(result.status, "PROCESSING")
        self.assertEqual(publish.call_args.kwargs["expected_username"], "lecrown")
        self.assertTrue(publish.call_args.kwargs["brand_organic"])
        saved = save.call_args.args[1]
        self.assertEqual(saved["sceneTitles"], {"1": "Living room"})
        self.assertEqual(saved["tiktokPublish"]["filename"], "tour.mp4")
        self.assertNotIn("publishedAt", saved["tiktokPublish"])

    async def test_failed_transfer_preserves_publish_id_for_status_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            (output / "tour.mp4").write_bytes(b"video")
            with patch.object(api, "p_output", return_value=output), patch.object(
                api, "read_project_state", return_value={}
            ), patch.object(api, "save_project_state") as save, patch.object(
                api, "publish_video_to_tiktok", side_effect=TikTokUploadTransferError("Transfer failed", publish_id="post-recover")
            ):
                result = await api.tiktok_publish("project", self.request())

        self.assertEqual(result.status, "UPLOAD_FAILED")
        self.assertEqual(result.publishId, "post-recover")
        self.assertEqual(save.call_args.args[1]["tiktokPublish"]["publishId"], "post-recover")
        self.assertIn("Transfer failed", result.error)

    async def test_status_checks_recorded_id_without_starting_another_post(self):
        state = {"tiktokPublish": {"publishId": "post-recover", "status": "UPLOAD_FAILED"}}
        with patch.object(api, "read_project_state", return_value=state), patch.object(
            api, "save_project_state"
        ), patch.object(api, "tiktok_publish_status", return_value="PUBLISH_COMPLETE") as status, patch.object(
            api, "publish_video_to_tiktok"
        ) as publish:
            result = await api.tiktok_publish_progress("project", "post-recover")
        self.assertEqual(result["status"], "PUBLISH_COMPLETE")
        status.assert_called_once_with("post-recover")
        publish.assert_not_called()

    async def test_status_rejects_a_different_projects_publish_id(self):
        with patch.object(api, "read_project_state", return_value={"tiktokPublish": {"publishId": "own-post"}}), patch.object(
            api, "tiktok_publish_status"
        ) as status:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_publish_progress("project", "other-post")
        self.assertEqual(error.exception.status_code, 404)
        status.assert_not_called()
