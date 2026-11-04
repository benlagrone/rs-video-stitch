"""Native framing changes visuals without replacing the selected master's audio."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import media_ai_policy
from app import tiktok_highlight as highlight


class SmartHighlightIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "output"
        self.work = self.root / "work"
        self.input = self.root / "input"
        for folder in (self.output, self.work, self.input / "images"):
            folder.mkdir(parents=True)
        self.master = self.output / "master.mp4"
        self.master.write_bytes(b"selected master video and its recorded audio")
        self.portrait = self.input / "images" / "portrait.jpg"
        self.landscape = self.input / "images" / "landscape.jpg"
        self.portrait.write_bytes(b"portrait original pixels")
        self.landscape.write_bytes(b"landscape original pixels")
        self.mapping = [
            {"sourceStart": 1.0, "sourceEnd": 13.0, "assetPath": self.portrait, "kind": "image", "assetStart": 0.0, "assetTimeScale": 0.0},
            {"sourceStart": 15.0, "sourceEnd": 27.0, "assetPath": self.landscape, "kind": "image", "assetStart": 0.0, "assetTimeScale": 0.0},
        ]
        self.plan = {"sourceFilename": self.master.name, "title": "A property tour", "hook": "Tour this home", "language": "en", "contact": {}, "durationSeconds": 30.4,
                     "segments": [{"sourceStart": 1.0, "sourceEnd": 13.0, "caption": "Portrait room"}, {"sourceStart": 15.0, "sourceEnd": 27.0, "caption": "Landscape room"}]}
        self.commands = []

    def _run(self, command, log=None):
        self.commands.append(command)
        path = Path(command[-1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"completed encoded media")
        return ""

    def _graphic(self, folder, **kwargs):
        path = Path(folder) / f"{kwargs['kind']}.mp4"
        path.write_bytes(b"ordinary HyperFrames graphic")
        return path

    def render(self, ai=False, runner=None):
        with patch.object(highlight, "validate_plan", return_value=self.plan), patch.object(highlight, "p_input", return_value=self.input), patch.object(highlight, "p_output", return_value=self.output), patch.object(highlight, "p_work", return_value=self.work), patch.object(highlight, "read_project_state", return_value={}), patch.object(highlight, "native_source_map", return_value=self.mapping) as native_map, patch.object(highlight, "_duration", return_value=30.4), patch.object(highlight, "_probe", return_value={"streams": [{"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}]}), patch.object(highlight, "artifact_ai_status", return_value={"status": "ai" if ai else "original", "isAigc": ai, "reasons": ["File-bound master media history."]}), patch.object(highlight, "_run", side_effect=runner or self._run), patch("app.tiktok_highlight_graphics.render_highlight_graphics", side_effect=self._graphic):
            result = highlight.render_highlight("listing", self.plan)
        self.assertEqual(native_map.call_args.kwargs["windows"], [(1.0, 13.0), (15.0, 27.0)])
        return result

    def test_native_portrait_and_landscape_are_used_without_rewrapping_the_master(self):
        original = self.master.read_bytes(), self.master.stat().st_mtime_ns
        result = self.render()
        visual_commands = [command for command in self.commands if "-an" in command]
        self.assertEqual(len(visual_commands), 2)
        self.assertEqual({Path(command[command.index("-i") + 1]) for command in visual_commands}, {self.portrait, self.landscape})
        self.assertTrue(all("-loop" in command for command in visual_commands))
        self.assertTrue(all(str(self.master) not in command for command in visual_commands))
        self.assertTrue(all(command[command.index("-filter_complex") + 1].count("split=2") == 1 for command in visual_commands))
        self.assertEqual((self.master.read_bytes(), self.master.stat().st_mtime_ns), original)
        manifest = json.loads(result.with_suffix(".highlight.json").read_text())
        self.assertEqual(manifest["framingMode"], "smart-native")
        self.assertEqual([row["assetFilename"] for row in manifest["visualSources"]], ["portrait.jpg", "landscape.jpg"])
        self.assertEqual(manifest["sourceSha256"], hashlib.sha256(original[0]).hexdigest())

    def test_audio_is_extracted_once_per_beat_from_the_exact_master_window(self):
        self.render()
        body = [command for command in self.commands if "-vf" in command and "ass=" in command[command.index("-vf") + 1]]
        self.assertEqual(len(body), 2)
        for command, start in zip(body, (1.0, 15.0)):
            self.assertEqual(command[command.index("-ss") + 1], f"{start:.6f}")
            self.assertEqual(command[command.index("-ss") + 3], "12.000000")
            self.assertIn(str(self.master), command)
            self.assertEqual(command[command.index("-af") + 1].count("afade=t=in"), 1)
            self.assertEqual(command[command.index("-af") + 1].count("afade=t=out"), 1)
            self.assertEqual(command.count("1:a:0"), 1)

    def test_graphics_receive_native_images_and_media_history_records_their_origin(self):
        result = self.render()
        # image_at should return native photos, not extract padded JPGs from the master.
        self.assertFalse(any("-frames:v" in command for command in self.commands))
        history = json.loads(result.with_suffix(".media.json").read_text())
        native = [item for item in history["inputs"] if item["kind"] == "highlight-native-visual"]
        self.assertEqual(len(native), 2)
        self.assertTrue(all(item["isAigc"] is False for item in native))
        self.assertTrue(all(item["basis"] == "inherited" for item in native))

    def test_known_ai_in_the_master_cannot_be_cleared_by_native_original_visuals(self):
        result = self.render(ai=True)
        self.assertTrue(json.loads(result.with_suffix(".media.json").read_text())["aiGenerated"])

    def test_explicit_native_image_generation_overrides_a_reviewed_original_master(self):
        media_ai_policy.write_media_provenance(self.portrait, ai_generated=True, reasons=["Generated property illustration."])
        result = self.render()
        self.assertTrue(json.loads(result.with_suffix(".media.json").read_text())["aiGenerated"])

    def test_a_native_asset_replaced_during_render_is_not_published(self):
        def replace_at_finalize(command, log=None):
            self._run(command, log)
            if Path(command[-1]).name.startswith(".tiktok_highlight_"):
                self.portrait.write_bytes(b"a different photograph")
        with self.assertRaisesRegex(RuntimeError, "native visual changed"):
            self.render(runner=replace_at_finalize)
        self.assertFalse(any(self.output.glob("tiktok_highlight_*.mp4")))

    def test_orientation_changes_in_an_unmapped_window_are_split_before_fitting(self):
        runs = [{"start": 0, "duration": 1.5, "crop": {"x": 650, "y": 0, "width": 620, "height": 1080}, "confidence": "black-bars"},
                {"start": 1.5, "duration": 2.5, "crop": None, "confidence": "full-frame"}]
        with patch.object(highlight, "analyze_framing", return_value=runs):
            sections = highlight._visual_sections(self.master, [], 20, 24, {})
        self.assertEqual([(row["sourceStart"], row["sourceEnd"]) for row in sections], [(20, 21.5), (21.5, 24)])
        self.assertEqual(sections[0]["crop"]["width"], 620)
        self.assertIsNone(sections[1]["crop"])

    def test_more_scenes_than_beats_uses_verified_legacy_scene_windows(self):
        scenes = [{"title": f"Room {index}", "images": [f"photo-{index}.jpg"]} for index in range(9)]
        (self.input / "scenes.json").write_text(json.dumps({"scenes": scenes}))
        for index in range(9):
            (self.work / f"scene_{index:02d}.mp4").write_bytes(b"scene")
            if index < 8:
                (self.work / f"hyperframes_transition_{index:02d}.mp4").write_bytes(b"transition")
        (self.work / "hyperframes_intro.mp4").write_bytes(b"intro")
        def duration(path):
            if path == self.master:
                return 100
            if path.name == "hyperframes_intro.mp4":
                return 2
            return 1 if "transition" in path.name else 10
        with patch.object(highlight, "p_input", return_value=self.input), patch.object(highlight, "p_output", return_value=self.output), patch.object(highlight, "p_work", return_value=self.work), patch.object(highlight, "_duration", side_effect=duration), patch.object(highlight, "read_project_state", return_value={"title": "Tour", "useIntro": True}):
            plan = highlight.plan_highlight("listing", self.master.name, 45)
        self.assertEqual(plan["selectionMethod"], "verified-source-section-subset")
        self.assertEqual(plan["framingMode"], "smart-native")
        self.assertEqual(len(plan["segments"]), 6)
        self.assertEqual(plan["segments"][0]["caption"], "Room 0")
        self.assertEqual(plan["segments"][-1]["caption"], "Room 8")
        for segment in plan["segments"]:
            index = int(segment["caption"].split()[-1])
            self.assertGreaterEqual(segment["sourceStart"], 2 + index * 11)
            self.assertLessEqual(segment["sourceEnd"], 12 + index * 11)


if __name__ == "__main__":
    unittest.main()
