import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import api
from app.schemas import TikTokMediaReviewRequest, TikTokPublishRequest, TikTokVariantRequest


class TikTokSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        (self.output / "tour.mp4").write_bytes(b"original video")
        stat = (self.output / "tour.mp4").stat()
        self.fingerprint = {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns)}
        for name, value in (("p_output", self.output), ("read_project_state", {"outputName": "tour.mp4", "youtubeUpload": {"videoId": "keep"}}), ("is_lecrown_project", True)):
            mock = patch.object(api, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def publish_request(self, **overrides):
        return TikTokPublishRequest(**{
            "filename": "tour.mp4", "caption": "Property tour", "privacyLevel": "SELF_ONLY",
            "expectedCreatorUsername": "lecrown", **overrides,
        })

    async def test_lecrown_settings_use_requested_defaults_and_do_not_guess_unknown_media(self):
        unknown = {"status": "unknown", "isAigc": None, "basis": "unknown", "required": False, "reasons": []}
        with patch.object(api, "artifact_ai_status", return_value=unknown):
            result = await api.tiktok_settings("property", "tour.mp4")
        self.assertEqual(result["defaults"], {"allowComment": True, "allowDuet": False, "allowStitch": False, "brandOrganic": True})
        self.assertEqual(result["profile"], "lecrown")
        self.assertEqual(result["defaultsVersion"], 2)
        self.assertIsNone(result["ai"]["isAigc"])

    async def test_original_render_publishes_with_ai_off_and_lecrown_defaults(self):
        with patch.object(api, "artifact_ai_status", return_value={"status": "original", "isAigc": False}), patch.object(
            api, "publish_video_to_tiktok", return_value={"publishId": "post", "status": "PROCESSING"}
        ) as publish, patch.object(api, "save_project_state") as save:
            await api.tiktok_publish("property", self.publish_request())
        self.assertFalse(publish.call_args.kwargs["is_aigc"])
        self.assertTrue(publish.call_args.kwargs["allow_comment"])
        self.assertFalse(publish.call_args.kwargs["allow_duet"])
        self.assertFalse(publish.call_args.kwargs["allow_stitch"])
        self.assertTrue(publish.call_args.kwargs["brand_organic"])
        self.assertEqual(save.call_args.args[1]["youtubeUpload"], {"videoId": "keep"})

    async def test_generated_media_enables_ai_label_and_keeps_explicit_comment_choice(self):
        with patch.object(api, "artifact_ai_status", return_value={"status": "ai", "isAigc": True, "required": True}), patch.object(
            api, "publish_video_to_tiktok", return_value={"publishId": "post", "status": "PROCESSING"}
        ) as publish, patch.object(api, "save_project_state"):
            await api.tiktok_publish("property", self.publish_request(allowComment=False))
        self.assertTrue(publish.call_args.kwargs["is_aigc"])
        self.assertFalse(publish.call_args.kwargs["allow_comment"])

    async def test_stale_ai_checkbox_cannot_clear_a_required_label(self):
        with patch.object(api, "artifact_ai_status", return_value={"status": "ai", "isAigc": True, "required": True}), patch.object(api, "publish_video_to_tiktok") as publish:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_publish("property", self.publish_request(isAigc=False))
        self.assertEqual(error.exception.status_code, 409)
        publish.assert_not_called()

    async def test_unknown_origin_requires_review_even_if_client_sends_a_checkbox_value(self):
        for choice in (True, False):
            with self.subTest(choice=choice), patch.object(api, "artifact_ai_status", return_value={"status": "unknown", "isAigc": None}), patch.object(api, "publish_video_to_tiktok") as publish:
                with self.assertRaises(HTTPException) as error:
                    await api.tiktok_publish("property", self.publish_request(isAigc=choice))
                self.assertEqual(error.exception.status_code, 400)
                publish.assert_not_called()

    async def test_operator_review_is_bound_to_the_selected_file(self):
        statuses = [{"status": "unknown", "isAigc": None, "fingerprint": self.fingerprint}, {"status": "original", "isAigc": False, "basis": "operator-review"}]
        with patch.object(api, "artifact_ai_status", side_effect=statuses), patch.object(api, "write_media_provenance") as write:
            result = await api.tiktok_media_review("property", TikTokMediaReviewRequest(filename="tour.mp4", isAigc=False, fingerprint=self.fingerprint))
        self.assertEqual(write.call_args.args[0], self.output / "tour.mp4")
        self.assertEqual(write.call_args.kwargs["source"], {"kind": "operator-review"})
        self.assertEqual(write.call_args.kwargs["expected_fingerprint"], self.fingerprint)
        self.assertFalse(result["ai"]["isAigc"])

    async def test_review_cannot_override_recorded_generation_evidence(self):
        with patch.object(api, "artifact_ai_status", return_value={"status": "ai", "isAigc": True, "required": True, "fingerprint": self.fingerprint}), patch.object(api, "write_media_provenance") as write:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_media_review("property", TikTokMediaReviewRequest(filename="tour.mp4", isAigc=False, fingerprint=self.fingerprint))
        self.assertEqual(error.exception.status_code, 409)
        write.assert_not_called()

    async def test_confirming_required_ai_preserves_the_generation_evidence(self):
        assessment = {"status": "ai", "isAigc": True, "required": True, "basis": "recorded", "fingerprint": self.fingerprint}
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "write_media_provenance") as write:
            result = await api.tiktok_media_review("property", TikTokMediaReviewRequest(filename="tour.mp4", isAigc=True, fingerprint=self.fingerprint))
        self.assertEqual(result["ai"], assessment)
        write.assert_not_called()

    async def test_review_rejects_a_replaced_video(self):
        with patch.object(api, "artifact_ai_status", return_value={"status": "unknown", "isAigc": None, "fingerprint": {"size": 20, "mtimeNs": "new"}}), patch.object(api, "write_media_provenance") as write:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_media_review("property", TikTokMediaReviewRequest(filename="tour.mp4", isAigc=False, fingerprint=self.fingerprint))
        self.assertEqual(error.exception.status_code, 409)
        write.assert_not_called()

    async def test_vertical_variant_inherits_the_exact_source_version(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint, "reasons": ["Recorded footage"]}
        def convert(source, destination):
            destination.write_bytes(b"vertical video")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "save_project_state"):
            result = await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(result.filename, "tiktok_tour.mp4")
        self.assertFalse(write.call_args.kwargs["ai_generated"])
        self.assertEqual(write.call_args.kwargs["source"]["filename"], "tour.mp4")
        self.assertEqual(len(write.call_args.kwargs["source"]["sha256"]), 64)

    async def test_vertical_variant_rejects_source_replacement_during_conversion(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(source, destination):
            destination.write_bytes(b"vertical video")
            source.write_bytes(b"new source version")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "save_project_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        write.assert_not_called()
        save.assert_not_called()
        self.assertFalse((self.output / "tiktok_tour.mp4").exists())

    async def test_invalid_file_has_a_clear_validation_error(self):
        with patch.object(api, "artifact_ai_status", side_effect=ValueError("Choose an existing project video")):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_settings("property", "../tour.mp4")
        self.assertEqual(error.exception.status_code, 422)
