"""AI disclosure follows consumed render media rather than later project settings."""
import hashlib
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media_ai_policy, renderer, tiktok_highlight
from app.tts import TTSConfigurationError


ORIGINAL = {"status": "original", "isAigc": False, "reasons": ["Recorded original media was established."]}
UNKNOWN = {"status": "unknown", "isAigc": None, "reasons": ["The uploaded media origin is unverified."]}


class RenderMediaProvenanceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "listing"
        self.input = self.project / "input"
        (self.input / "images").mkdir(parents=True)
        (self.input / "images" / "room.jpg").write_bytes(b"recorded photograph")
        self.document = {
            "vid": {"api": "xtts", "voice": "test", "lang": "en"},
            "scenes": [{"images": ["room.jpg"], "VO": "A room in this property."}],
        }
        self.options = {"ttsApi": "none", "logoEnabled": False, "thumbnailEnabled": False}

    def _save(self):
        (self.input / "scenes.json").write_text(json.dumps(self.document), encoding="utf-8")

    @staticmethod
    def _fake_run(command, log=None):
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"completed encoded media")

    def _render(self, asset_assessment=ORIGINAL, **patches):
        self._save()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(renderer, "run", side_effect=self._fake_run))
            stack.enter_context(mock.patch.object(renderer, "ffprobe_duration", return_value=1.0))
            stack.enter_context(mock.patch.object(renderer, "_resolve_title_font_from_style", return_value=Path("font.ttf")))
            stack.enter_context(mock.patch.object(renderer, "_generate_selected_thumbnail"))
            stack.enter_context(mock.patch.object(renderer, "asset_path_ai_status", return_value=asset_assessment))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(renderer, name, **value))
            return renderer.render_project("listing", self.root, self.options, "listing.mp4")

    def test_successful_tts_is_frozen_even_after_project_voice_is_disabled(self):
        self.options["ttsApi"] = "xtts"
        output = self._render(synthesize_xtts={"side_effect": lambda text, path, **kwargs: path.write_bytes(b"synthetic speech")})
        self.document["vid"]["api"] = "none"
        self.options["ttsApi"] = "none"
        self._save()
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], True)
        provenance = json.loads(output.with_suffix(".media.json").read_text())
        narration = next(item for item in provenance["inputs"] if item["kind"] == "synthesized-narration")
        self.assertEqual(narration["provider"], "xtts")
        self.assertEqual(narration["sha256"], hashlib.sha256(b"synthetic speech").hexdigest())

    def test_disabled_tts_does_not_label_real_images_as_ai(self):
        synth = mock.Mock()
        output = self._render(synthesize_xtts={"new": synth})
        synth.assert_not_called()
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], False)

    def test_prerecorded_voice_overrides_tts_and_keeps_unknown_origin(self):
        voice = self.input / "voices" / "00.wav"
        voice.parent.mkdir()
        voice.write_bytes(b"uploaded narration")
        self.options.update({"voiceDir": "voices", "ttsApi": "xtts"})
        synth = mock.Mock()

        def classify(path, **kwargs):
            return UNKNOWN if path == voice else ORIGINAL

        self._save()
        with mock.patch.object(renderer, "asset_path_ai_status", side_effect=classify):
            # Preserve the path-aware classifier across the standard render mocks.
            with mock.patch.object(renderer, "run", side_effect=self._fake_run), mock.patch.object(renderer, "ffprobe_duration", return_value=1.0), mock.patch.object(renderer, "_resolve_title_font_from_style", return_value=Path("font.ttf")), mock.patch.object(renderer, "_generate_selected_thumbnail"), mock.patch.object(renderer, "synthesize_xtts", synth):
                output = renderer.render_project("listing", self.root, self.options, "listing.mp4")
        synth.assert_not_called()
        self.assertIsNone(media_ai_policy.asset_path_ai_status(output)["isAigc"])

    def test_failed_synthesis_followed_by_silence_is_not_synthetic_speech(self):
        self.options["ttsApi"] = "xtts"
        output = self._render(
            synthesize_xtts={"side_effect": TTSConfigurationError("Unavailable")},
            _synthesize_flite={"side_effect": RuntimeError("Unavailable")},
        )
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], False)

    def test_successful_flite_fallback_is_synthetic_speech(self):
        self.options["ttsApi"] = "xtts"
        output = self._render(
            synthesize_xtts={"side_effect": TTSConfigurationError("Unavailable")},
            _synthesize_flite={"side_effect": lambda text, path, *_args: path.write_bytes(b"flite speech")},
        )
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], True)

    def test_source_footage_ignores_unused_tts_configuration(self):
        footage = self.input / "footage" / "tour.mov"
        footage.parent.mkdir()
        footage.write_bytes(b"recorded tour")
        self.document["sourceFootage"] = [{"filename": "tour.mov"}]
        self.options["ttsApi"] = "xtts"
        synth = mock.Mock()

        def create_clip(source, destination, *_args):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"prepared clip")
            return destination

        output = self._render(
            synthesize_xtts={"new": synth},
            _prepare_footage_clip={"side_effect": create_clip},
            _extract_footage_poster={"side_effect": create_clip},
        )
        synth.assert_not_called()
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], False)
        provenance = json.loads(output.with_suffix(".media.json").read_text())
        self.assertEqual([item["kind"] for item in provenance["inputs"]], ["source-footage"])

    def test_consumed_generation_metadata_is_passed_with_the_matching_asset(self):
        self.document["scenes"][0]["timeline"] = [
            {"image": "not-used.jpg", "imageGeneration": {"model": "unused-model"}},
            {"image": "room.jpg", "imageGeneration": {"model": "selected-model", "seed": 42}},
        ]
        self._save()
        inputs = []

        def classify(path, *, provenance=None):
            inputs.append((path, provenance))
            return {"status": "ai", "isAigc": True, "reasons": ["Explicit generated asset."]}

        with mock.patch.object(renderer, "run", side_effect=self._fake_run), mock.patch.object(renderer, "ffprobe_duration", return_value=1.0), mock.patch.object(renderer, "_resolve_title_font_from_style", return_value=Path("font.ttf")), mock.patch.object(renderer, "_generate_selected_thumbnail"), mock.patch.object(renderer, "asset_path_ai_status", side_effect=classify):
            output = renderer.render_project("listing", self.root, self.options, "listing.mp4")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0][1]["imageGeneration"]["model"], "selected-model")
        self.assertIs(media_ai_policy.asset_path_ai_status(output)["isAigc"], True)

    def test_highlight_inherits_the_frozen_source_sha_and_assessment(self):
        output = self.project / "output"
        output.mkdir()
        source = output / "master.mp4"
        source.write_bytes(b"original landscape master")
        plan = {"sourceFilename": source.name, "title": "Property tour", "hook": "See this home", "language": "en", "contact": {}, "durationSeconds": 30.0, "segments": [{"sourceStart": 0, "sourceEnd": 24, "caption": "Original room footage"}]}

        def graphic(work, **kwargs):
            path = work / f"{kwargs['kind']}.mp4"
            path.write_bytes(b"conventional title graphic")
            return path

        with mock.patch.object(tiktok_highlight, "validate_plan", return_value=plan), mock.patch.object(tiktok_highlight, "p_output", return_value=output), mock.patch.object(tiktok_highlight, "p_work", return_value=self.project / "work"), mock.patch.object(tiktok_highlight, "_probe", return_value={"streams": [{"codec_type": "audio"}]}), mock.patch.object(tiktok_highlight, "_duration", return_value=30.0), mock.patch.object(tiktok_highlight, "_run", side_effect=self._fake_run), mock.patch.object(tiktok_highlight, "artifact_ai_status", return_value=ORIGINAL) as assessment, mock.patch("app.tiktok_highlight_graphics.render_highlight_graphics", side_effect=graphic):
            result = tiktok_highlight.render_highlight("listing", plan)
        assessment.assert_called_once_with("listing", "master.mp4")
        snapshot = json.loads(result.with_suffix(".highlight.json").read_text())["sourceAiMedia"]
        self.assertEqual(snapshot["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertIs(snapshot["isAigc"], False)
        self.assertIs(media_ai_policy.asset_path_ai_status(result)["isAigc"], False)
