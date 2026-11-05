import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import api
from app.schemas import TikTokMediaReviewRequest, TikTokPublishRequest, TikTokVariantRequest


class TikTokSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        self.work = self.output / "work"
        self.state_path = self.output / "input" / "project_state.json"
        (self.output / "tour.mp4").write_bytes(b"original video")
        stat = (self.output / "tour.mp4").stat()
        self.fingerprint = {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns)}
        for name, value in (("p_output", self.output), ("p_work", self.work), ("p_state", self.state_path), ("ensure_dirs", None), ("read_project_state", {"outputName": "tour.mp4", "youtubeUpload": {"videoId": "keep"}}), ("is_lecrown_project", True)):
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
        def convert(source, destination, **_options):
            destination.write_bytes(b"vertical video")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "_save_tiktok_variant_state"):
            result = await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(result.filename, "tiktok_tour.mp4")
        self.assertFalse(write.call_args.kwargs["ai_generated"])
        self.assertEqual(write.call_args.kwargs["source"]["filename"], "tour.mp4")
        self.assertEqual(len(write.call_args.kwargs["source"]["sha256"]), 64)
        self.assertEqual(result.framingMode, "fill")
        self.assertEqual(result.variant["framingMode"], "fill")
        self.assertEqual(result.variant["sourceSha256"], hashlib.sha256(b"original video").hexdigest())

    async def test_vertical_variant_rejects_source_replacement_during_conversion(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(source, destination, **_options):
            destination.write_bytes(b"vertical video")
            source.write_bytes(b"new source version")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "_save_tiktok_variant_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        write.assert_not_called()
        save.assert_not_called()
        self.assertFalse((self.output / "tiktok_tour.mp4").exists())

    async def test_vertical_variant_passes_caption_source_settings_to_renderer(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"captioned vertical video")
        saved = {"renderOptions": {"captionsEnabled": False, "ttsLanguage": "zh-CN"}}
        with patch.object(api, "read_project_state", return_value=saved), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert) as render, patch.object(api, "write_media_provenance"), patch.object(api, "_save_tiktok_variant_state") as save:
            await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4", captionsEnabled=True))
        self.assertEqual(render.call_args.kwargs, {"captions_enabled": True, "project_id": "property", "language": "zh-CN", "framing_mode": "fill"})
        self.assertTrue(save.call_args.args[1]["tiktokVariant"]["captionsEnabled"])

    async def test_explicit_captions_off_overrides_a_saved_enabled_setting(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"vertical video")
        with patch.object(api, "read_project_state", return_value={"renderOptions": {"captionsEnabled": True}}), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert) as render, patch.object(api, "write_media_provenance"), patch.object(api, "_save_tiktok_variant_state") as save:
            await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4", captionsEnabled=False))
        self.assertEqual(render.call_args.kwargs, {"captions_enabled": False, "project_id": "property", "language": None, "framing_mode": "fill"})
        self.assertFalse(save.call_args.args[1]["tiktokVariant"]["captionsEnabled"])

    def previous_variant_bundle(self):
        previous = {}
        for path in api._tiktok_variant_bundle(self.output / "tiktok_tour.mp4", self.work):
            path.parent.mkdir(parents=True, exist_ok=True)
            previous[path] = b'{"addedMedia": []}' if path.suffixes[-2:] == [".framing", ".json"] else f"previous {path.name}".encode()
            path.write_bytes(previous[path])
        return previous

    def framing_report(self, destination, additions):
        return {
            "version": 1, "sourceFilename": "tour.mp4", "outputFilename": destination.name,
            "sourceIdentity": {**self.fingerprint, "sha256": hashlib.sha256(b"original video").hexdigest()},
            "outputSha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "addedMedia": additions,
        }

    def test_variant_framing_defaults_to_fill_and_accepts_only_supported_modes(self):
        self.assertEqual(TikTokVariantRequest().framingMode, "fill")
        self.assertEqual(TikTokVariantRequest(framingMode="fit").framingMode, "fit")
        with self.assertRaises(ValidationError):
            TikTokVariantRequest(framingMode="stretch")

    async def test_fit_mode_and_project_context_are_passed_even_when_captions_are_off(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"fit vertical video")
        saved = {"language": "zh-CN", "renderOptions": {"captionsEnabled": False}, "youtubeUpload": {"videoId": "keep"}}
        with patch.object(api, "read_project_state", return_value=saved), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert) as render, patch.object(api, "write_media_provenance"), patch.object(api, "_save_tiktok_variant_state") as save:
            result = await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4", framingMode="fit"))
        self.assertEqual(render.call_args.kwargs, {"project_id": "property", "captions_enabled": False, "language": "zh-CN", "framing_mode": "fit"})
        state = save.call_args.args[1]
        self.assertEqual(state["tiktokVideoFilename"], "tiktok_tour.mp4")
        self.assertEqual(state["tiktokFramingMode"], "fit")
        self.assertEqual(state["youtubeUpload"], {"videoId": "keep"})
        self.assertEqual(result.variant, state["tiktokVariant"])
        self.assertEqual(result.framingMode, "fit")

    async def test_success_archives_previous_video_sidecars_and_clean_master(self):
        previous = self.previous_variant_bundle()
        assessment = {"status": "ai", "isAigc": True, "fingerprint": self.fingerprint, "reasons": ["Generated media"]}
        def convert(_source, destination, **_options):
            for path in previous:
                path.write_bytes(f"new {path.name}".encode())
            destination.with_suffix(".framing.json").write_text('{"addedMedia": []}')
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "_save_tiktok_variant_state"):
            await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        archives = list((self.work / "tiktok-variant-history" / "tiktok_tour").iterdir())
        self.assertEqual(len(archives), 1)
        for path, content in previous.items():
            section = "work" if path.parent == self.work else "output"
            self.assertEqual((archives[0] / section / path.name).read_bytes(), content)
        self.assertEqual((self.output / "tour.mp4").read_bytes(), b"original video")
        self.assertEqual((self.output / "tiktok_tour.mp4").read_bytes(), b"new tiktok_tour.mp4")

    async def test_changed_source_restores_previous_video_and_whole_known_bundle(self):
        previous = self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(source, _destination, **_options):
            for path in previous:
                path.write_bytes(f"new {path.name}".encode())
            source.write_bytes(b"a different source version")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as provenance, patch.object(api, "_save_tiktok_variant_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        provenance.assert_not_called()
        save.assert_not_called()
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_provenance_failure_restores_previous_accepted_variant(self):
        previous = self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance", side_effect=ValueError("provenance rejected")), patch.object(api, "_save_tiktok_variant_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        save.assert_not_called()
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_state_save_failure_restores_video_and_updated_media_provenance(self):
        previous = self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "_save_tiktok_variant_state", side_effect=OSError("disk unavailable")):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 502)
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_atomic_state_write_failure_keeps_prior_state_bytes_and_variant(self):
        previous = self.previous_variant_bundle()
        self.state_path.parent.mkdir(parents=True)
        old_state = b'{"title": "Original", "editorNote": "keep exactly"}\n'
        self.state_path.write_bytes(old_state)
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
        replace = api.os.replace
        def fail_state_replace(source, destination):
            if Path(destination) == self.state_path:
                raise OSError("state publish failed")
            return replace(source, destination)
        with patch.object(api, "read_project_state", return_value=json.loads(old_state)), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api.os, "replace", side_effect=fail_state_replace):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(self.state_path.read_bytes(), old_state)
        self.assertEqual(list(self.state_path.parent.glob(".*.tmp")), [])
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_success_saves_latest_unrelated_state_and_returns_the_file_bound_variant(self):
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
        before = {"title": "Property", "editorNote": "old"}
        latest = {"title": "Property", "editorNote": "edited while conversion ran", "youtubeUpload": {"videoId": "keep"}}
        with patch.object(api, "read_project_state", side_effect=[before, latest]), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert):
            result = await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4", framingMode="fit"))
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["editorNote"], "edited while conversion ran")
        self.assertEqual(saved["youtubeUpload"], {"videoId": "keep"})
        self.assertEqual(saved["tiktokVariant"], result.variant)
        self.assertEqual(saved["tiktokFramingMode"], "fit")
        self.assertEqual(saved["tiktokVideoFilename"], result.filename)

    async def test_rollback_preserves_an_external_video_replacement(self):
        self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"our conversion")
        def replace_then_fail(destination, **_options):
            destination.write_bytes(b"a separate writer's version")
            destination.with_suffix(".media.json").write_bytes(b"separate writer's history")
            raise ValueError("output changed")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance", side_effect=replace_then_fail), patch.object(api, "_save_tiktok_variant_state") as save:
            with self.assertRaises(HTTPException):
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        save.assert_not_called()
        self.assertEqual((self.output / "tiktok_tour.mp4").read_bytes(), b"a separate writer's version")
        self.assertEqual((self.output / "tiktok_tour.media.json").read_bytes(), b"separate writer's history")

    async def test_rollback_rechecks_external_replacement_after_copying_the_backup(self):
        self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        destination = self.output / "tiktok_tour.mp4"
        def convert(_source, output, **_options):
            output.write_bytes(b"our conversion")
        copy = api.shutil.copy2
        def replace_during_restore(source, target):
            result = copy(source, target)
            if Path(target).name.startswith(f".{destination.name}.") and Path(target).suffix == ".restore":
                destination.write_bytes(b"external replacement during backup copy")
            return result
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance", side_effect=ValueError("history rejected")), patch.object(api.shutil, "copy2", side_effect=replace_during_restore):
            with self.assertRaises(HTTPException):
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(destination.read_bytes(), b"external replacement during backup copy")

    async def test_concurrent_editor_save_during_state_fsync_is_preserved(self):
        previous = self.previous_variant_bundle()
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text('{"editorNote": "before conversion"}')
        concurrent_state = b'{"editorNote": "new concurrent edit", "unrelated": 42}'
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
        fsync = api.os.fsync
        def edit_during_fsync(fd):
            fsync(fd)
            self.state_path.write_bytes(concurrent_state)
        with patch.object(api, "read_project_state", side_effect=lambda _pid: json.loads(self.state_path.read_text())), patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api.os, "fsync", side_effect=edit_during_fsync):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("edits were preserved", error.exception.detail)
        self.assertEqual(self.state_path.read_bytes(), concurrent_state)
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_added_media_uses_strict_tri_state_ai_disclosure(self):
        for parent_ai, addition_ai, expected in ((False, True, True), (False, None, None), (True, False, True), (None, False, None)):
            with self.subTest(parent_ai=parent_ai, addition_ai=addition_ai):
                addition = {"filename": "logo/custom.png", "kind": "portrait-card-logo", "sha256": "c" * 64, "isAigc": addition_ai, "reasons": ["Selected logo origin"]}
                assessment = {"isAigc": parent_ai, "fingerprint": self.fingerprint, "reasons": ["Selected master origin"]}
                def convert(_source, destination, **_options):
                    destination.write_bytes(b"vertical video with added logo")
                    destination.with_suffix(".framing.json").write_text(json.dumps(self.framing_report(destination, [addition])))
                with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "_save_tiktok_variant_state"):
                    await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
                self.assertIs(write.call_args.kwargs["ai_generated"], expected)
                self.assertEqual(write.call_args.kwargs["inputs"], [addition])
                self.assertEqual(write.call_args.kwargs["source"]["sha256"], hashlib.sha256(b"original video").hexdigest())

    async def test_added_media_requires_framing_history_bound_to_both_source_and_output(self):
        previous = self.previous_variant_bundle()
        assessment = {"isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            destination.write_bytes(b"new vertical video")
            report = self.framing_report(destination, [{"isAigc": True}])
            report["outputSha256"] = "0" * 64
            destination.with_suffix(".framing.json").write_text(json.dumps(report))
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance") as write, patch.object(api, "_save_tiktok_variant_state") as save:
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("does not match", error.exception.detail)
        write.assert_not_called()
        save.assert_not_called()
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_atomic_renderer_failure_keeps_the_previous_bundle_and_releases_lock(self):
        previous = self.previous_variant_bundle()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def failed_conversion(_source, _destination, **_options):
            (self.work / ".private-partial.mp4").write_bytes(b"partial encode")
            raise RuntimeError("encoder failed before publish")
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=failed_conversion), patch.object(api, "_save_tiktok_variant_state"):
            for _attempt in range(2):
                with self.assertRaises(HTTPException) as error:
                    await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
                self.assertEqual(error.exception.status_code, 502)
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)

    async def test_concurrent_conversion_of_the_same_variant_is_rejected_until_finished(self):
        started, release = threading.Event(), threading.Event()
        assessment = {"status": "original", "isAigc": False, "fingerprint": self.fingerprint}
        def convert(_source, destination, **_options):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test conversion timed out")
            destination.write_bytes(b"vertical video")
        async def wait_for_start():
            while not started.is_set():
                await asyncio.sleep(0.01)
        with patch.object(api, "artifact_ai_status", return_value=assessment), patch.object(api, "render_tiktok_variant", side_effect=convert), patch.object(api, "write_media_provenance"), patch.object(api, "_save_tiktok_variant_state"):
            first = asyncio.create_task(api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4")))
            try:
                await asyncio.wait_for(wait_for_start(), timeout=2)
                with self.assertRaises(HTTPException) as error:
                    await api.tiktok_variant("property", TikTokVariantRequest(filename="tour.mp4"))
                self.assertEqual(error.exception.status_code, 409)
            finally:
                release.set()
                await first

    async def test_invalid_file_has_a_clear_validation_error(self):
        with patch.object(api, "artifact_ai_status", side_effect=ValueError("Choose an existing project video")):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_settings("property", "../tour.mp4")
        self.assertEqual(error.exception.status_code, 422)
