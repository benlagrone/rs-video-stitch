import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import caption_variants as captions
from app import tiktok_highlight as highlight
from app.video_captions import CaptionCue, CaptionError


class CaptionVariantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("input", "output", "work"):
            (self.root / name).mkdir()
        self.source = self.root / "output/master.mp4"
        self.source.write_bytes(b"selected master")
        self.clean = self.root / "work/uncaptioned_master.mp4"
        self.clean.write_bytes(b"clean pixels and original audio")
        self.destination = self.root / "output/tiktok_master.mp4"
        self.raw = self.root / "work/framed.mp4"
        self.raw.write_bytes(b"framed clean pixels and original audio")
        self.stack = []
        for name, replacement in (
            ("p_input", lambda _: self.root / "input"),
            ("p_output", lambda _: self.root / "output"),
            ("p_work", lambda _: self.root / "work"),
            ("read_project_state", lambda _: {"renderOptions": {"ttsLanguage": "en-US"}}),
            ("_probe", lambda _: {"format": {"duration": "8"}, "streams": [{"codec_type": "video", "width": 1080, "height": 1920}]}),
        ):
            context = patch.object(captions, name, replacement)
            context.start()
            self.addCleanup(context.stop)

    def document(self, footage=False):
        (self.root / "input/scenes.json").write_text(json.dumps({"sourceFootage": [{"filename": "recorded.mp4"}] if footage else [], "scenes": [{"VO": "Edited text must never replace old audio words."}]}))

    def manifest(self, kind="script", content_windows=None):
        value = {
            "version": captions.CAPTION_VERSION,
            "outputFilename": self.source.name,
            "sourceKind": kind,
            "source": "narration-script" if kind == "script" else "whisper",
            "videoSha256": captions._hash(self.source),
            "inputSha256": captions._hash(self.clean),
            "cues": [{"start": 0, "end": 4, "text": "one two three four"}],
            "contentWindows": content_windows,
            "files": {key: path.name for key, path in captions._sidecars(self.source).items()},
        }
        for key, path in captions._sidecars(self.source).items():
            path.write_text(json.dumps(value) if key == "json" else f"original {key}")
        return value

    def fake_burn(self, raw, destination, cues, **kwargs):
        destination.write_bytes(raw.read_bytes() + b" plus subtitles")
        source = kwargs["source"]
        value = {
            "version": captions.CAPTION_VERSION, "outputFilename": destination.name,
            "sourceKind": "script" if source == "narration-script" else "whisper",
            "source": source, "cueCount": len(cues),
            "inputSha256": captions._hash(raw), "videoSha256": captions._hash(destination),
            "cues": [{"start": cue.start, "end": cue.end, "text": cue.text} for cue in cues],
            "contentWindows": kwargs.get("content_windows"),
            "files": {key: path.name for key, path in captions._sidecars(destination).items()},
        }
        for key, path in captions._sidecars(destination).items():
            path.write_text(json.dumps(value) if key == "json" else "spoken subtitles")
        return value

    def test_off_without_engine_manifest_is_legacy_noop(self):
        self.document()
        self.assertIsNone(captions.prepare_derivative_captions("p", self.source, enabled=False))

    def test_photo_derivative_requires_matching_file_bound_cues(self):
        self.document()
        with self.assertRaisesRegex(CaptionError, "re-render the master"):
            captions.prepare_derivative_captions("p", self.source, enabled=True)
        self.manifest()
        self.source.write_bytes(b"a newer master")
        with self.assertRaisesRegex(CaptionError, "older video"):
            captions.prepare_derivative_captions("p", self.source, enabled=True)

    def test_caption_off_uses_verified_clean_pixels(self):
        self.manifest()
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=False)
        self.assertEqual(prepared.clean_source, self.clean)
        self.assertFalse(prepared.enabled)
        self.clean.write_bytes(b"new render replaced retained pixels")
        with self.assertRaisesRegex(CaptionError, "captions off"):
            captions.prepare_derivative_captions("p", self.source, enabled=False)

    def test_current_edited_script_is_not_used_for_photo_captions(self):
        self.document()
        self.manifest()
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        with patch.object(captions, "transcribe_video", side_effect=AssertionError("Photos must not use Whisper")), patch.object(captions, "burn_captions", side_effect=self.fake_burn):
            result = captions.apply_derivative_captions(prepared, self.raw, self.destination, windows=[(1, 3, 2)])
        self.assertEqual(result["cues"], [{"start": 2, "end": 4, "text": "two three"}])
        self.assertEqual(self.source.read_bytes(), b"selected master")
        self.assertEqual((self.root / "work/uncaptioned_tiktok_master.mp4").read_bytes(), self.raw.read_bytes())
        self.assertTrue(captions._recognized_manifest(self.destination, result))

    def test_footage_transcribes_final_framed_audio(self):
        self.document(footage=True)
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        def transcript(path, *args, **kwargs):
            self.assertEqual(path.read_bytes(), self.raw.read_bytes())
            return [CaptionCue(1, 2, "words from the framed video")]
        with patch.object(captions, "transcribe_video", side_effect=transcript) as whisper, patch.object(captions, "burn_captions", side_effect=self.fake_burn):
            result = captions.apply_derivative_captions(prepared, self.raw, self.destination)
        whisper.assert_called_once()
        self.assertEqual(result["source"], "whisper")

    def test_full_variant_passes_source_content_fence_to_whisper_and_burn(self):
        self.manifest(kind="whisper", content_windows=[[1.2, 3.0], [4.4, 7.0]])
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        with patch.object(captions, "transcribe_video", return_value=[CaptionCue(1.2, 2.0, "Spoken content")]) as whisper, patch.object(captions, "burn_captions", side_effect=self.fake_burn) as burn:
            result = captions.apply_derivative_captions(prepared, self.raw, self.destination)
        self.assertEqual(whisper.call_args.kwargs["content_windows"], [(1.2, 3.0), (4.4, 7.0)])
        self.assertEqual(burn.call_args.kwargs["content_windows"], [(1.2, 3.0), (4.4, 7.0)])
        self.assertEqual(result["contentWindows"], [(1.2, 3.0), (4.4, 7.0)])

    def test_highlight_fence_excludes_inserted_and_embedded_source_cards(self):
        self.manifest(kind="whisper", content_windows=[[1.2, 3.0], [4.4, 7.0]])
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        windows = [(0.0, 3.5, 2.0), (4.0, 7.0, 5.9)]
        actual = captions.derivative_content_windows(prepared, windows, duration=12.9)
        self.assertEqual([[round(bound, 3) for bound in pair] for pair in actual], [[3.2, 5.0], [6.3, 8.9]])
        self.document(footage=True)
        self.source.with_suffix(".captions.json").unlink()
        legacy = captions.prepare_derivative_captions("p", self.source, enabled=True)
        self.assertEqual(captions.derivative_content_windows(legacy, windows, 12.9), [(2.0, 5.5), (5.9, 8.9)])
        self.assertIsNone(captions.derivative_content_windows(legacy, None, 8.0))

    def test_empty_known_content_windows_do_not_become_unrestricted(self):
        self.manifest(kind="whisper", content_windows=[])
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        self.assertEqual(captions.derivative_content_windows(prepared, None, 8), [])
        self.assertEqual(captions.derivative_content_windows(prepared, [(0, 4, 2)], 8), [])

    def test_invalid_source_content_windows_require_fresh_master(self):
        for value in ([[-1, 2]], [[1, 3], [2, 4]], [[0, 20]], ["12"]):
            self.manifest(kind="whisper", content_windows=value)
            with self.subTest(value=value), self.assertRaisesRegex(CaptionError, "content windows"):
                captions.prepare_derivative_captions("p", self.source, enabled=True)

    def test_source_race_does_not_replace_previous_derivative(self):
        self.manifest()
        prepared = captions.prepare_derivative_captions("p", self.source, enabled=True)
        self.destination.write_bytes(b"previous accepted derivative")
        def mutate_after_burn(*args, **kwargs):
            value = self.fake_burn(*args, **kwargs)
            self.clean.write_bytes(b"concurrent re-render")
            return value
        with patch.object(captions, "burn_captions", side_effect=mutate_after_burn):
            with self.assertRaisesRegex(CaptionError, "clean master changed"):
                captions.apply_derivative_captions(prepared, self.raw, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"previous accepted derivative")
        self.assertFalse(captions._sidecars(self.destination)["json"].exists())

    def test_off_retirement_archives_user_edits_but_not_feature_sidecars(self):
        self.manifest()
        captions._sidecars(self.source)["srt"].write_text("User corrected this transcript")
        feature = self.source.with_suffix(".features.srt")
        feature.write_text("Separate feature titles")
        self.source.write_bytes(b"successful new caption-off render")
        archived = captions.retire_caption_sidecars(self.source)
        self.assertEqual((archived / "master.srt").read_text(), "User corrected this transcript")
        self.assertFalse(captions._sidecars(self.source)["json"].exists())
        self.assertEqual(feature.read_text(), "Separate feature titles")
        self.assertEqual(self.source.read_bytes(), b"successful new caption-off render")

    def test_arbitrary_sidecars_are_not_archived(self):
        self.source.with_suffix(".srt").write_text("User subtitles")
        self.source.with_suffix(".captions.json").write_text('{"version":1,"notes":"user file"}')
        self.assertIsNone(captions.retire_caption_sidecars(self.source))
        self.assertEqual(self.source.with_suffix(".srt").read_text(), "User subtitles")

    def test_highlight_mapping_leaves_cards_blank_and_preserves_cjk(self):
        cues = [CaptionCue(0, 4, "one two three four"), CaptionCue(4, 8, "您好欢迎")]
        result = captions.remap_script_cues(cues, [(1, 3, 2), (5, 7, 4.4)], duration=10.4)
        self.assertEqual(result, [CaptionCue(2, 4, "two three"), CaptionCue(4.4, 6.4, "好欢")])
        with self.assertRaises(CaptionError):
            captions.remap_script_cues(cues, [(0, 2, 2), (4, 6, 3)], duration=10)

    def test_highlight_plan_rejects_string_caption_switch(self):
        self.document()
        with patch.object(highlight, "p_output", return_value=self.root / "output"), patch.object(highlight, "p_input", return_value=self.root / "input"), patch.object(highlight, "p_work", return_value=self.root / "work"), patch.object(highlight, "_duration", return_value=120), patch.object(highlight, "read_project_state", return_value={"title": "Property tour"}):
            plan = highlight.plan_highlight("p", self.source.name, 30)
            plan["spokenCaptionsEnabled"] = "false"
            with self.assertRaisesRegex(ValueError, "enabled or disabled"):
                highlight.validate_plan("p", plan)

    def test_highlight_uses_clean_master_and_keeps_speech_and_feature_srt_separate(self):
        self.document()
        self.manifest()
        plan = {
            "sourceFilename": self.source.name, "title": "Property tour", "hook": "Tour", "language": "en",
            "contact": {}, "durationSeconds": 30,
            "segments": [{"sourceStart": 0, "sourceEnd": 11.8, "caption": "Feature one"}, {"sourceStart": 20, "sourceEnd": 31.8, "caption": "Feature two"}],
        }
        probe = {"format": {"duration": "30"}, "streams": [{"codec_type": "video", "width": 1080, "height": 1920}, {"codec_type": "audio"}]}
        body_sources = []
        def run(command, log=None):
            Path(command[-1]).write_bytes(b"clean assembled output")
            return ""
        def body(source, sections, destination, *args):
            body_sources.append(source)
            destination.write_bytes(b"clean body")
        for enabled in (True, False):
            with self.subTest(enabled=enabled), ExitStack() as stack:
                plan["spokenCaptionsEnabled"] = enabled
                replacements = {
                    "validate_plan": lambda *_: plan,
                    "p_output": lambda _: self.root / "output", "p_input": lambda _: self.root / "input", "p_work": lambda _: self.root / "work",
                    "read_project_state": lambda _: {}, "_probe": lambda _: probe, "_duration": lambda _: 30,
                    "artifact_ai_status": lambda *_: {"isAigc": False, "reasons": ["Original test media"]},
                    "native_source_map": lambda *_args, **_kwargs: [],
                    "split_window": lambda *_args: [{"kind": "master", "assetStart": 0}],
                    "analyze_framing": lambda *_args, **_kwargs: [{"crop": None}],
                    "_visual_sections": lambda *_args: [],
                    "_run": run, "_render_body_section": body,
                    "_normalize_clip": lambda _source, destination, *_args: destination.write_bytes(b"silent graphic"),
                }
                for name, replacement in replacements.items():
                    stack.enter_context(patch.object(highlight, name, replacement))
                stack.enter_context(patch("app.renderer._record_media_asset"))
                stack.enter_context(patch("app.tiktok_highlight_graphics._brand_logo", return_value=self.root / "logo.png"))
                stack.enter_context(patch("app.tiktok_highlight_graphics._hyperframes_root", return_value=self.root))
                stack.enter_context(patch("app.tiktok_highlight_graphics.render_highlight_graphics", return_value=self.root / "graphic.mp4"))
                stack.enter_context(patch.object(captions, "_probe", return_value=probe))
                stack.enter_context(patch.object(captions, "burn_captions", side_effect=self.fake_burn))
                whisper = stack.enter_context(patch.object(captions, "transcribe_video", side_effect=AssertionError("Photo highlights must use script cues")))
                progress = []
                result = highlight.render_highlight("p", plan, progress=lambda stage, value: progress.append((stage, value)))
                whisper.assert_not_called()
            self.assertTrue(result.is_file())
            self.assertTrue(all(path == self.clean for path in body_sources))
            self.assertEqual(self.source.read_bytes(), b"selected master")
            self.assertEqual([value for _stage, value in progress], sorted(value for _stage, value in progress))
            if enabled:
                self.assertEqual(result.with_suffix(".srt").read_text(), "spoken subtitles")
                self.assertIn("Feature one", result.with_suffix(".features.srt").read_text())
                self.assertIn("CAPTION_BURN", [stage for stage, _value in progress])
                manifest = json.loads(result.with_suffix(".captions.json").read_text())
                self.assertEqual(manifest["cues"][0], {"start": 2, "end": 6, "text": "one two three four"})
                self.assertEqual([[round(bound, 3) for bound in pair] for pair in manifest["contentWindows"]], [[2.0, 13.8], [14.2, 26.0]])
            else:
                self.assertIn("Feature one", result.with_suffix(".srt").read_text())
                self.assertFalse(result.with_suffix(".captions.json").exists())


if __name__ == "__main__":
    unittest.main()
