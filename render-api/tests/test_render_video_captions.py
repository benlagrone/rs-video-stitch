"""Render captions follow spoken audio without changing its source or provenance."""
import hashlib
import json
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media_ai_policy, renderer, video_captions
from app.schemas import RenderOptions, TikTokVariantRequest


ORIGINAL = {"status": "original", "isAigc": False, "reasons": ["Original recorded media."]}


class RenderVideoCaptionsTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "listing"
        self.input = self.project / "input"
        self.output = self.project / "output"
        self.work = self.project / "work"
        for name in ("images", "footage"):
            (self.input / name).mkdir(parents=True, exist_ok=True)
        (self.input / "images" / "room.jpg").write_bytes(b"original photograph")
        (self.input / "logo.png").write_bytes(b"ordinary brand graphic")
        self.options = {"ttsApi": "none", "logoEnabled": False, "thumbnailEnabled": False}
        self.document = {"scenes": [{"title": "", "images": ["room.jpg"], "VO": "Exact scene narration."}]}
        self.durations = {}

    def _save(self):
        (self.input / "scenes.json").write_text(json.dumps(self.document), encoding="utf-8")

    @staticmethod
    def _write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    @staticmethod
    def _cue(start, end, text):
        return SimpleNamespace(start=start, end=end, text=text)

    @contextmanager
    def _renderer_stubs(self):
        with ExitStack() as stack:
            run = stack.enter_context(mock.patch.object(renderer, "run", side_effect=lambda command, log=None: self._write(command[-1], b"assembled video")))
            stack.enter_context(mock.patch.object(renderer, "ffprobe_duration", side_effect=lambda path: self.durations.get(Path(path).name, 1.0)))
            stack.enter_context(mock.patch.object(renderer, "_resolve_title_font_from_style", return_value=Path("font.ttf")))
            stack.enter_context(mock.patch.object(renderer, "_generate_selected_thumbnail"))
            stack.enter_context(mock.patch.object(renderer, "asset_path_ai_status", return_value=ORIGINAL))
            stack.enter_context(mock.patch.object(renderer, "_prepare_footage_clip", side_effect=lambda source, destination, *_args: self._write(destination, b"normalized recorded footage")))
            stack.enter_context(mock.patch.object(renderer, "_extract_footage_poster", side_effect=lambda source, destination, *_args: self._write(destination, b"poster")))
            stack.enter_context(mock.patch.object(renderer, "_render_hyperframes_clip", side_effect=lambda composition, variables, destination, *_args: self._write(destination, b"silent branded card")))
            stack.enter_context(mock.patch.object(renderer, "_resolve_logo_image", return_value=self.input / "logo.png"))
            stack.enter_context(mock.patch.object(renderer, "_overlay_logo_on_video", side_effect=lambda source, destination, *_args: self._write(destination, b"logo:" + source.read_bytes())))
            synth = stack.enter_context(mock.patch.object(renderer, "synthesize_xtts", side_effect=lambda text, destination, **_kwargs: self._write(destination, b"synthesized narration")))
            yield SimpleNamespace(run=run, synth=synth)

    def _footage_document(self):
        for name in ("part-1.mov", "part-2.mov"):
            (self.input / "footage" / name).write_bytes(b"recorded creator footage")
        self.document = {
            "vid": {"lang": "zh-CN", "api": "none"},
            "sourceFootage": [{"filename": "part-1.mov"}, {"filename": "part-2.mov"}],
            "scenes": [],
        }

    def test_caption_option_defaults_off_and_round_trips(self):
        self.assertFalse(RenderOptions().captionsEnabled)
        self.assertIs(RenderOptions(captionsEnabled=True).model_dump()["captionsEnabled"], True)
        self.assertIsNone(TikTokVariantRequest().captionsEnabled)
        self.assertIs(TikTokVariantRequest(captionsEnabled=False).captionsEnabled, False)
        self.assertIs(TikTokVariantRequest(captionsEnabled=True).captionsEnabled, True)

    def test_footage_uses_whisper_once_on_assembled_audio_after_cards_and_logo(self):
        self._footage_document()
        self.options.update({"captionsEnabled": True, "introEnabled": True, "logoEnabled": True, "hyperframes": {"leadCardMode": "hyperframes", "sceneTransitionMode": "hyperframes"}})
        self.durations.update({"hyperframes_intro.mp4": 1.5, "clip_00.mp4": 4.0, "hyperframes_footage_transition_00.mp4": 0.75, "clip_01.mp4": 6.0})
        self._save()
        stages = []
        spoken = [self._cue(2.0, 3.0, "录制视频中的原话。")]

        def transcribe(source, work_dir, **kwargs):
            self.assertEqual(source, self.work / "uncaptioned_tour.mp4")
            self.assertEqual(source.read_bytes(), b"logo:assembled video")
            self.assertEqual(work_dir, self.work)
            self.assertEqual(kwargs["language"], "zh-CN")
            self.assertEqual(kwargs["content_windows"], [(1.5, 5.5), (6.25, 12.25)])
            kwargs["progress_cb"](1.0)
            return spoken

        def burn(source, destination, cues, **kwargs):
            self.assertEqual(cues, spoken)
            self.assertEqual(kwargs["sidecar_base"], self.output / "tour")
            self.assertEqual((kwargs["width"], kwargs["height"]), (1920, 1080))
            self.assertEqual(kwargs["source_kind"], "whisper")
            self.assertEqual(kwargs["content_windows"], [(1.5, 5.5), (6.25, 12.25)])
            return self._write(destination, b"captioned recorded video")

        with self._renderer_stubs() as stubs, mock.patch("app.video_captions.transcribe_video", side_effect=transcribe) as transcriber, mock.patch("app.video_captions.script_cues") as scripts, mock.patch("app.video_captions.burn_captions", side_effect=burn):
            result = renderer.render_project("listing", self.root, self.options, "tour.mp4", progress=lambda stage, _value: stages.append(stage))
        transcriber.assert_called_once()
        scripts.assert_not_called()
        stubs.synth.assert_not_called()
        concat = next(call.args[0] for call in stubs.run.call_args_list if "concat=n=" in " ".join(call.args[0]))
        inputs = [Path(concat[index + 1]).name for index, value in enumerate(concat) if value == "-i"]
        self.assertEqual(inputs, ["hyperframes_intro.mp4", "clip_00.mp4", "hyperframes_footage_transition_00.mp4", "clip_01.mp4"])
        self.assertEqual(result.read_bytes(), b"captioned recorded video")
        self.assertLess(stages.index("CAPTION_TRANSCRIBE"), stages.index("CAPTION_BURN"))
        # Provenance must describe the captioned bytes, and ordinary captions do
        # not turn recorded footage into AI-generated imagery or narration.
        self.assertIs(media_ai_policy.asset_path_ai_status(result)["isAigc"], False)
        metadata = json.loads(result.with_suffix(".media.json").read_text())
        self.assertEqual(metadata["fingerprint"]["size"], len(b"captioned recorded video"))

    def test_footage_caption_failure_does_not_publish_uncaptioned_output(self):
        self._footage_document()
        self.options["captionsEnabled"] = True
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video", side_effect=RuntimeError("Whisper unavailable")), mock.patch("app.video_captions.burn_captions") as burn:
            with self.assertRaisesRegex(RuntimeError, "Whisper unavailable"):
                renderer.render_project("listing", self.root, self.options, "tour.mp4")
        burn.assert_not_called()
        self.assertFalse((self.output / "tour.mp4").exists())
        self.assertFalse((self.output / "tour.media.json").exists())
        self.assertTrue((self.work / "uncaptioned_tour.mp4").exists())

    def test_caption_failure_preserves_a_previous_completed_output(self):
        self._footage_document()
        self.options["captionsEnabled"] = True
        self._save()
        previous = self._write(self.output / "tour.mp4", b"previous accepted video")
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video", side_effect=RuntimeError("Whisper unavailable")):
            with self.assertRaises(RuntimeError):
                renderer.render_project("listing", self.root, self.options, "tour.mp4")
        self.assertEqual(previous.read_bytes(), b"previous accepted video")

    def test_disabled_footage_captions_do_not_invoke_whisper_or_script_timing(self):
        self._footage_document()
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video") as transcribe, mock.patch("app.video_captions.script_cues") as scripts, mock.patch("app.video_captions.burn_captions") as burn:
            result = renderer.render_project("listing", self.root, self.options, "tour.mp4")
        transcribe.assert_not_called()
        scripts.assert_not_called()
        burn.assert_not_called()
        self.assertEqual(result.read_bytes(), b"assembled video")

    def test_photo_captions_use_exact_voice_script_and_actual_scene_offsets(self):
        self.document = {
            "vid": {"lang": "en-US", "api": "xtts", "voice": "test"},
            "scenes": [
                {"title": "", "images": ["room.jpg"], "VO": "Exact first narration.", "description": "Different marketing copy."},
                {"title": "", "images": ["room.jpg"], "VO": "Exact second narration."},
            ],
        }
        self.options.update({"captionsEnabled": True, "ttsApi": "xtts", "introEnabled": True, "hyperframes": {"leadCardMode": "hyperframes", "sceneTransitionMode": "hyperframes"}})
        self.durations.update({"scene_00.wav": 3.0, "scene_01.wav": 2.5, "scene_00.mp4": 5.0, "scene_01.mp4": 8.0, "hyperframes_intro.mp4": 1.5, "hyperframes_transition_00.mp4": 0.75})
        self._save()

        def script(text, duration, start=0):
            return [self._cue(start, start + duration, text)]

        with self._renderer_stubs() as stubs, mock.patch("app.video_captions.transcribe_video") as transcribe, mock.patch("app.video_captions.script_cues", side_effect=script) as scripts, mock.patch("app.video_captions.burn_captions", side_effect=lambda source, destination, *_args, **_kwargs: self._write(destination, b"captioned photo video")) as burn:
            result = renderer.render_project("listing", self.root, self.options, "photos.mp4")
        transcribe.assert_not_called()
        self.assertEqual([call.args[0] for call in stubs.synth.call_args_list], ["Exact first narration.", "Exact second narration."])
        self.assertEqual(scripts.call_args_list, [mock.call("Exact first narration.", 3.0, start=1.5), mock.call("Exact second narration.", 2.5, start=7.25)])
        self.assertEqual([cue.text for cue in burn.call_args.args[2]], ["Exact first narration.", "Exact second narration."])
        self.assertEqual(burn.call_args.kwargs["source_kind"], "script")
        self.assertEqual(result.read_bytes(), b"captioned photo video")

    def test_photo_without_narration_uses_its_actual_clip_duration(self):
        self.options["captionsEnabled"] = True
        self.durations.update({"scene_00.wav": 1.0, "scene_00.mp4": 6.5})
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video") as transcribe, mock.patch("app.video_captions.script_cues", side_effect=lambda text, duration, start=0: [self._cue(start, start + duration, text)]) as scripts, mock.patch("app.video_captions.burn_captions", side_effect=lambda source, destination, *_args, **_kwargs: self._write(destination, b"captioned photo video")):
            renderer.render_project("listing", self.root, self.options, "photos.mp4")
        transcribe.assert_not_called()
        scripts.assert_called_once_with("Exact scene narration.", 6.5, start=0.0)

    def test_requested_captions_with_no_speech_do_not_succeed_uncaptioned(self):
        self._footage_document()
        self.options["captionsEnabled"] = True
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video", return_value=[]), mock.patch("app.video_captions.burn_captions") as burn:
            with self.assertRaisesRegex(ValueError, "no speech or narration"):
                renderer.render_project("listing", self.root, self.options, "tour.mp4")
        burn.assert_not_called()
        self.assertFalse((self.output / "tour.mp4").exists())

    def test_disabled_photo_captions_leave_existing_render_behavior(self):
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video") as transcribe, mock.patch("app.video_captions.script_cues") as scripts, mock.patch("app.video_captions.burn_captions") as burn:
            result = renderer.render_project("listing", self.root, self.options, "photos.mp4")
        transcribe.assert_not_called()
        scripts.assert_not_called()
        burn.assert_not_called()
        self.assertEqual(result.read_bytes(), b"assembled video")

    def test_empty_photo_script_does_not_fall_back_to_whisper(self):
        self.document["scenes"][0]["VO"] = ""
        self.options["captionsEnabled"] = True
        self._save()
        with self._renderer_stubs(), mock.patch("app.video_captions.transcribe_video") as transcribe, mock.patch("app.video_captions.script_cues", return_value=[]), mock.patch("app.video_captions.burn_captions") as burn:
            with self.assertRaisesRegex(ValueError, "no speech or narration"):
                renderer.render_project("listing", self.root, self.options, "photos.mp4")
        transcribe.assert_not_called()
        burn.assert_not_called()
        self.assertFalse((self.output / "photos.mp4").exists())

    def _previous_caption_bundle(self, name, kind):
        previous = self._write(self.output / name, b"previous captioned video")
        base = previous.with_suffix("")
        paths = {key: Path(str(base) + suffix) for key, suffix in (("srt", ".srt"), ("ass", ".ass"), ("json", ".captions.json"))}
        paths["srt"].write_text("Previously generated subtitles, edited by the user.")
        paths["ass"].write_text("Previous ASS styles.")
        paths["json"].write_text(json.dumps({
            "version": video_captions.CAPTION_VERSION, "sourceKind": kind, "outputFilename": previous.name,
            "videoSha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
            "inputSha256": "a" * 64, "cues": [],
            "files": {key: path.name for key, path in paths.items()},
        }))
        return previous, paths

    def test_successful_caption_off_render_archives_owned_sidecars_and_preserves_edits(self):
        for footage, name in ((False, "photos.mp4"), (True, "tour.mp4")):
            with self.subTest(footage=footage):
                if footage:
                    self._footage_document()
                self._save()
                _previous, paths = self._previous_caption_bundle(name, "whisper" if footage else "script")
                with self._renderer_stubs():
                    result = renderer.render_project("listing", self.root, self.options, name)
                self.assertEqual(result.read_bytes(), b"assembled video")
                self.assertTrue(all(not path.exists() for path in paths.values()))
                archived = list((self.work / "caption-history").rglob(paths["srt"].name))
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].read_text(), "Previously generated subtitles, edited by the user.")

    def test_failed_caption_off_render_keeps_previous_video_and_caption_bundle(self):
        self._footage_document()
        self._save()
        previous, paths = self._previous_caption_bundle("tour.mp4", "whisper")

        def partial_encode(command, log=None):
            self._write(command[-1], b"partial failed encode")
            raise RuntimeError("Encoding failed")

        with self._renderer_stubs(), mock.patch.object(renderer, "run", side_effect=partial_encode):
            with self.assertRaisesRegex(RuntimeError, "Encoding failed"):
                renderer.render_project("listing", self.root, self.options, "tour.mp4")
        self.assertEqual(previous.read_bytes(), b"previous captioned video")
        self.assertTrue(all(path.exists() for path in paths.values()))

    def test_vertical_captions_reframe_verified_clean_pixels_before_final_burn(self):
        source = self._write(self.output / "master.mp4", b"master with baked landscape captions")
        clean = self._write(self.work / "uncaptioned_master.mp4", b"clean master pixels")
        destination = self.output / "vertical.mp4"
        prepared = SimpleNamespace(clean_source=clean, source_type="script", cues=[])

        def apply(prepared_source, framed, final, **kwargs):
            self.assertIs(prepared_source, prepared)
            self.assertNotEqual(framed, final)
            self.assertTrue(framed.parent.name.startswith(".tiktok-captions-"))
            self.assertEqual(framed.read_bytes(), b"assembled video")
            return self._write(final, b"captioned portrait video")

        with self._renderer_stubs() as stubs, mock.patch("app.vertical_framing.analyze_framing", return_value=[{"start": 0, "duration": 5, "crop": None}]), mock.patch("app.caption_variants.prepare_derivative_captions", return_value=prepared) as prepare, mock.patch("app.caption_variants.apply_derivative_captions", side_effect=apply) as caption:
            result = renderer.render_tiktok_variant(source, destination, captions_enabled=True, project_id="listing", language="zh-CN")
        prepare.assert_called_once_with("listing", source, enabled=True, language="zh-CN")
        caption.assert_called_once()
        command = stubs.run.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], str(clean))
        self.assertEqual(result.read_bytes(), b"captioned portrait video")
        self.assertEqual(source.read_bytes(), b"master with baked landscape captions")

    def test_vertical_captions_off_strips_our_baked_master_text(self):
        source = self._write(self.output / "master.mp4", b"master with baked captions")
        source.with_suffix(".captions.json").write_text("{}")
        clean = self._write(self.work / "uncaptioned_master.mp4", b"clean master pixels")
        destination = self.output / "vertical.mp4"
        destination.with_suffix(".captions.json").write_text("{}")
        prepared = SimpleNamespace(clean_source=clean, source_type="script", cues=[])
        with self._renderer_stubs() as stubs, mock.patch("app.vertical_framing.analyze_framing", return_value=[{"start": 0, "duration": 5, "crop": None}]), mock.patch("app.caption_variants.prepare_derivative_captions", return_value=prepared) as prepare, mock.patch("app.caption_variants.apply_derivative_captions") as caption, mock.patch("app.caption_variants.verify_derivative_source") as verify, mock.patch("app.caption_variants.retire_caption_sidecars") as retire:
            result = renderer.render_tiktok_variant(source, destination, captions_enabled=False, project_id="listing")
        prepare.assert_called_once_with("listing", source, enabled=False, language=None)
        caption.assert_not_called()
        verify.assert_called_once_with(prepared)
        retire.assert_called_once_with(destination)
        command = stubs.run.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], str(clean))
        self.assertEqual(result.read_bytes(), b"assembled video")

    def test_vertical_caption_failure_preserves_previous_completed_output(self):
        source = self._write(self.output / "master.mp4", b"recorded master")
        clean = self._write(self.work / "uncaptioned_master.mp4", b"clean master pixels")
        destination = self._write(self.output / "vertical.mp4", b"previous accepted portrait")
        prepared = SimpleNamespace(clean_source=clean, source_type="whisper", cues=[])
        with self._renderer_stubs(), mock.patch("app.vertical_framing.analyze_framing", return_value=[{"start": 0, "duration": 5, "crop": None}]), mock.patch("app.caption_variants.prepare_derivative_captions", return_value=prepared), mock.patch("app.caption_variants.apply_derivative_captions", side_effect=RuntimeError("Whisper unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Whisper unavailable"):
                renderer.render_tiktok_variant(source, destination, captions_enabled=True, project_id="listing")
        self.assertEqual(destination.read_bytes(), b"previous accepted portrait")

    def test_vertical_caption_off_rejects_a_clean_source_changed_during_framing(self):
        source = self._write(self.output / "master.mp4", b"master with baked captions")
        source.with_suffix(".captions.json").write_text("{}")
        clean = self._write(self.work / "uncaptioned_master.mp4", b"clean master pixels")
        destination = self._write(self.output / "vertical.mp4", b"previous accepted portrait")
        prepared = SimpleNamespace(clean_source=clean, source_type="script", cues=[])
        with self._renderer_stubs(), mock.patch("app.vertical_framing.analyze_framing", return_value=[{"start": 0, "duration": 5, "crop": None}]), mock.patch("app.caption_variants.prepare_derivative_captions", return_value=prepared), mock.patch("app.caption_variants.verify_derivative_source", side_effect=RuntimeError("Clean master changed")), mock.patch("app.caption_variants.retire_caption_sidecars") as retire:
            with self.assertRaisesRegex(RuntimeError, "Clean master changed"):
                renderer.render_tiktok_variant(source, destination, captions_enabled=False, project_id="listing")
        retire.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"previous accepted portrait")

    def test_vertical_caption_request_without_project_fails_before_encoding(self):
        source = self._write(self.output / "master.mp4", b"recorded master")
        with self._renderer_stubs() as stubs:
            with self.assertRaisesRegex(ValueError, "project is required"):
                renderer.render_tiktok_variant(source, self.output / "vertical.mp4", captions_enabled=True)
        stubs.run.assert_not_called()
