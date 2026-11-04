import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import media_ai_policy as policy


class MediaAiPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.input = self.root / "input"
        self.output.mkdir()
        self.input.mkdir()
        for name, path in (("p_output", self.output), ("p_input", self.input)):
            replacement = patch.object(policy, name, return_value=path)
            replacement.start()
            self.addCleanup(replacement.stop)

    def video(self, filename="master.mp4", content=b"recorded media"):
        path = self.output / filename
        path.write_bytes(content)
        return path

    def status(self, path):
        return policy.artifact_ai_status("project", path.name)

    def record(self, path, ai, source=None):
        return policy.write_media_provenance(path, ai_generated=ai, reasons=["Actual media origin"], source=source)

    def source(self, path):
        return {"kind": "deterministic-derivative", "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_completed_file_record_is_snapshot_bound_without_changing_media(self):
        for ai, status, required in ((True, "ai", True), (False, "original", False), (None, "unknown", False)):
            with self.subTest(ai=ai):
                video = self.video()
                before = video.read_bytes(), video.stat().st_mtime_ns
                sidecar = self.record(video, ai)
                data = json.loads(sidecar.read_text())
                self.assertIsInstance(data["fingerprint"]["mtimeNs"], str)
                self.assertEqual((video.read_bytes(), video.stat().st_mtime_ns), before)
                assessment = self.status(video)
                self.assertEqual(assessment["status"], status)
                self.assertIs(assessment["required"], required)
                self.assertEqual(assessment["basis"], "recorded")

    def test_replaced_video_does_not_reuse_old_ai_or_original_history(self):
        for ai in (True, False):
            video = self.video()
            self.record(video, ai)
            video.write_bytes(b"different completed render")
            self.assertIsNone(self.status(video)["isAigc"])

    def test_review_writer_rejects_an_expected_fingerprint_from_an_older_render(self):
        video = self.video()
        expected = self.status(video)["fingerprint"]
        video.write_bytes(b"new render replaces the reviewed video")
        with self.assertRaisesRegex(ValueError, "video changed"):
            policy.write_media_provenance(
                video, ai_generated=False, reasons=["Reviewed original"],
                source={"kind": "operator-review"}, expected_fingerprint=expected,
            )
        self.assertFalse(video.with_suffix(".media.json").exists())
        self.assertIsNone(self.status(video)["isAigc"])

    def test_review_writer_binds_the_supplied_current_fingerprint(self):
        video = self.video()
        expected = self.status(video)["fingerprint"]
        sidecar = policy.write_media_provenance(
            video, ai_generated=False, reasons=["Reviewed original"],
            source={"kind": "operator-review"}, expected_fingerprint=expected,
        )
        self.assertEqual(json.loads(sidecar.read_text())["fingerprint"], expected)
        self.assertIs(self.status(video)["isAigc"], False)

    def test_writer_rechecks_output_before_replacing_the_history(self):
        video = self.video()
        sidecar = self.record(video, True)
        previous_history = sidecar.read_bytes()
        expected = self.status(video)["fingerprint"]
        original_write = Path.write_text

        def replace_during_write(path, text, *args, **kwargs):
            result = original_write(path, text, *args, **kwargs)
            if path.name.endswith(".tmp"):
                video.write_bytes(b"simultaneous completed render")
            return result

        with patch.object(Path, "write_text", replace_during_write), self.assertRaisesRegex(ValueError, "video changed"):
            policy.write_media_provenance(
                video, ai_generated=False, reasons=["Reviewed original"],
                source={"kind": "operator-review"}, expected_fingerprint=expected,
            )
        self.assertEqual(sidecar.read_bytes(), previous_history)
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_unknown_old_video_does_not_infer_media_from_editor_settings(self):
        video = self.video()
        (self.input / "project.json").write_text(json.dumps({
            "ttsApi": "vibevoice", "ollamaModel": "local-model", "useHyperframes": True,
        }))
        self.assertEqual(self.status(video)["status"], "unknown")

    def test_operator_review_is_specific_to_one_output_and_can_be_corrected(self):
        video = self.video()
        other = self.video("other.mp4")
        self.record(video, True, {"kind": "operator-review"})
        assessment = self.status(video)
        self.assertTrue(assessment["isAigc"])
        self.assertEqual(assessment["basis"], "operator-review")
        self.assertFalse(assessment["required"])
        self.assertIsNone(self.status(other)["isAigc"])
        self.record(video, False, {"kind": "operator-review"})
        self.assertIs(self.status(video)["isAigc"], False)

    def test_unknown_derivative_resolves_when_exact_source_is_reviewed(self):
        master = self.video()
        derivative = self.video("vertical.mp4", b"same recorded media with ordinary graphics")
        self.record(derivative, None, self.source(master))
        self.assertIsNone(self.status(derivative)["isAigc"])
        self.record(master, False, {"kind": "operator-review"})
        assessment = self.status(derivative)
        self.assertIs(assessment["isAigc"], False)
        self.assertEqual(assessment["basis"], "inherited")
        master.write_bytes(b"different master render")
        self.assertIsNone(self.status(derivative)["isAigc"])

    def test_derivative_cannot_hide_actual_synthetic_source_with_operator_false(self):
        master = self.video()
        self.record(master, True)
        derivative = self.video("vertical.mp4")
        source = {**self.source(master), "kind": "operator-review"}
        self.record(derivative, False, source)
        assessment = self.status(derivative)
        self.assertTrue(assessment["isAigc"])
        self.assertTrue(assessment["required"])
        self.assertEqual(assessment["basis"], "inherited")

    def test_required_propagates_parent_basis_through_multiple_derivatives(self):
        master = self.video()
        self.record(master, True, {"kind": "operator-review"})
        first = self.video("first.mp4")
        self.record(first, None, self.source(master))
        second = self.video("second.mp4")
        self.record(second, None, self.source(first))
        assessment = self.status(second)
        self.assertTrue(assessment["isAigc"])
        self.assertFalse(assessment["required"])

    def test_legacy_highlight_inherits_only_exact_source_and_unchanged_output(self):
        master = self.video()
        self.record(master, False, {"kind": "operator-review"})
        highlight = self.video("highlight.mp4")
        manifest = highlight.with_suffix(".highlight.json")
        manifest.write_text(json.dumps({
            "filename": highlight.name, "sourceFilename": master.name,
            "sourceSha256": self.source(master)["sha256"], "sourceUnchanged": True,
        }))
        self.assertIs(self.status(highlight)["isAigc"], False)
        later = manifest.stat().st_mtime_ns + 1_000_000_000
        os.utime(highlight, ns=(later, later))
        self.assertIsNone(self.status(highlight)["isAigc"])

    def test_legacy_highlight_does_not_classify_by_unbound_project_settings(self):
        master = self.video()
        highlight = self.video("highlight.mp4")
        highlight.with_suffix(".highlight.json").write_text(json.dumps({
            "filename": highlight.name, "sourceFilename": master.name,
            "sourceSha256": "0" * 64, "sourceUnchanged": True,
            "ttsApi": "none", "useHyperframes": True,
        }))
        self.record(master, False)
        self.assertIsNone(self.status(highlight)["isAigc"])

    def test_source_cycles_and_malformed_records_are_unknown(self):
        first = self.video("first.mp4")
        second = self.video("second.mp4")
        self.record(first, None, self.source(second))
        self.record(second, None, self.source(first))
        self.assertIsNone(self.status(first)["isAigc"])
        record = self.record(first, False)
        data = json.loads(record.read_text())
        data["source"] = "malformed"
        record.write_text(json.dumps(data))
        self.assertIs(self.status(first)["isAigc"], False)
        record.write_text("not json")
        self.assertIsNone(self.status(first)["isAigc"])

    def test_output_path_traversal_and_symlinks_are_not_accepted(self):
        for filename in ("../master.mp4", "folder\\master.mp4", "", "/tmp/master.mp4"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                policy.artifact_ai_status("project", filename)
        other = self.root / "external.mp4"
        other.write_bytes(b"external")
        link = self.output / "link.mp4"
        link.symlink_to(other)
        with self.assertRaises(FileNotFoundError):
            self.status(link)

    def test_asset_ai_generation_is_explicit_and_editor_assistance_is_ignored(self):
        image = self.video("image.jpg")
        for record in (
            {"imageGeneration": {"model": "generated-still-model", "seed": 12}},
            {"motionGeneration": {"motionSeed": 34, "sourceImageFingerprint": "source"}},
            {"provider": "comfyui"}, {"generator": "stable-diffusion"}, {"aiGenerated": True},
        ):
            with self.subTest(record=record):
                self.assertTrue(policy.asset_path_ai_status(image, provenance=record)["isAigc"])
        for record in (
            {}, {"origin": "upload"}, {"provider": "ollama", "model": "room-classifier"},
            {"roomRenamer": {"model": "vision-model"}, "caption": "Living room"},
            {"provider": "hyperframes"}, {"provider": "ffmpeg"},
            {"motionGeneration": {"method": "blender", "model": "scene"}},
            {"imageGeneration": {"method": "hyperframes", "prompt": "title text"}},
        ):
            with self.subTest(record=record):
                self.assertIsNone(policy.asset_path_ai_status(image, provenance=record)["isAigc"])

    def test_original_asset_declaration_must_match_any_supplied_binding(self):
        image = self.video("image.jpg")
        self.assertIs(policy.asset_path_ai_status(image, provenance={"origin": "recorded"})["isAigc"], False)
        self.assertIsNone(policy.asset_path_ai_status(image, provenance={"origin": "recorded", "sha256": "0" * 64})["isAigc"])
        fingerprint = {"size": image.stat().st_size, "mtimeNs": str(image.stat().st_mtime_ns)}
        self.assertIs(policy.asset_path_ai_status(image, provenance={"aiGenerated": False, "fingerprint": fingerprint})["isAigc"], False)
        image.write_bytes(b"new image")
        self.assertIsNone(policy.asset_path_ai_status(image, provenance={"aiGenerated": False, "fingerprint": fingerprint})["isAigc"])

    def test_inputs_are_preserved_for_inspection(self):
        video = self.video()
        inputs = [{"kind": "voice", "sha256": "source", "isAigc": True}]
        sidecar = policy.write_media_provenance(video, ai_generated=True, reasons=["Narration synthesized"], inputs=inputs)
        self.assertEqual(json.loads(sidecar.read_text())["inputs"], inputs)

    def test_lecrown_presets_do_not_override_explicit_other_workflows(self):
        for pid, state in (
            ("lecrown-tour", {}), ("2438-tour", {"youtubeProfile": "english"}),
            ("2438-tour", {"youtubeProfile": "mandarin"}),
            ("tour", {"brokerage": "LeCrown Properties"}),
            ("tour", {"propertyAddress": "2438 White Oak"}),
        ):
            self.assertTrue(policy.is_lecrown_project(pid, state))
        for pid, state in (
            ("lecrown-tour", {"youtubeProfile": "animals"}),
            ("lecrown-tour", {"projectType": "bible-video"}),
            ("bible-test", {"youtubeProfile": "english"}),
            ("animals-test", {"tiktokDefaultsProfile": "lecrown"}),
            ("unrelated", {}),
        ):
            self.assertFalse(policy.is_lecrown_project(pid, state))


if __name__ == "__main__":
    unittest.main()
