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
        graphs = [command[command.index("-filter_complex") + 1] for command in visual_commands]
        self.assertTrue(all("crop=1080:1920" in graph and "split=2" not in graph and "boxblur" not in graph for graph in graphs))
        self.assertEqual((self.master.read_bytes(), self.master.stat().st_mtime_ns), original)
        manifest = json.loads(result.with_suffix(".highlight.json").read_text())
        self.assertEqual(manifest["framingMode"], "smart-native")
        self.assertEqual((manifest["layout"], manifest["framingVersion"]), ("fill", 2))
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

    def test_padded_prepared_video_uses_proxy_clock_and_unwraps_picture_before_fill(self):
        raw, proxy = self.input / "recorded-hdr.mov", self.work / "clip_00.mp4"
        mapping = [{"sourceStart": 2, "sourceEnd": 20, "kind": "video", "assetPath": raw,
                    "assetStart": 0, "assetTimeScale": 2, "proxyPath": proxy, "proxyTimeScale": 0.75}]
        crop = {"x": 648, "y": 0, "width": 624, "height": 1080}
        runs = [{"start": 0, "duration": 1.5, "crop": crop, "confidence": "black-bars"},
                {"start": 1.5, "duration": 1.5, "crop": None, "confidence": "full-frame"}]
        with patch.object(highlight, "analyze_framing", return_value=runs) as analyze:
            sections = highlight._visual_sections(self.master, mapping, 6, 10, {})
        analyze.assert_called_once_with(proxy, 3.0, 3.0)
        self.assertEqual([(row["sourceStart"], row["sourceEnd"], row["assetStart"]) for row in sections], [(6, 8, 3), (8, 10, 4.5)])
        self.assertTrue(all(row["assetPath"] == proxy and row["nativeAssetPath"] == raw for row in sections))
        piece = sections[0]
        with patch.object(highlight, "analyze_fill_focus", return_value=[(0, .486), (1.5, .486)]), patch.object(highlight, "_run", side_effect=self._run):
            highlight._render_visual_section(piece, self.work / "framed.mp4")
        graph = self.commands[-1][self.commands[-1].index("-filter_complex") + 1]
        self.assertEqual(piece["focusPoints"], [(0, .5), (2, .5)])
        self.assertIn("crop=624:1080:648:0", graph)
        self.assertIn("setpts=(PTS-STARTPTS)/0.750000000", graph)
        self.assertIn("crop=1080:1920", graph)
        self.assertNotIn("boxblur", graph)

    def _native_video_fixture(self):
        raw, proxy = self.input / "recorded-hdr.mov", self.work / "clip_00.mp4"
        raw.write_bytes(b"native HDR phone pixels")
        proxy.write_bytes(b"verified tone-mapped clean proxy")
        self.mapping[0].update(kind="video", assetPath=raw, proxyPath=proxy, assetTimeScale=1, proxyTimeScale=1)
        return raw, proxy

    def test_proxy_lineage_records_raw_ai_and_uses_clean_pixels_for_body_and_cards(self):
        raw, proxy = self._native_video_fixture()
        media_ai_policy.write_media_provenance(raw, ai_generated=True, reasons=["Explicit native generation record."])
        def framing(_source, _start, length, **_kwargs):
            return [{"start": 0, "duration": length, "crop": {"x": 648, "y": 0, "width": 624, "height": 1080}, "confidence": "black-bars"}]
        with patch.object(highlight, "analyze_framing", side_effect=framing):
            result = self.render()
        commands = [command for command in self.commands if "-an" in command or "-frames:v" in command]
        self.assertFalse(any(str(raw) in command for command in commands))
        self.assertTrue(any(str(proxy) in command and "-frames:v" in command for command in commands))
        report = json.loads(result.with_suffix(".highlight.json").read_text())
        self.assertEqual(report["visualSources"][0]["assetFilename"], proxy.name)
        self.assertEqual(report["visualSources"][0]["nativeFilename"], raw.name)
        media = json.loads(result.with_suffix(".media.json").read_text())
        self.assertTrue(media["aiGenerated"])
        self.assertEqual({item["filename"] for item in media["inputs"] if item["kind"] in {"highlight-native-visual", "highlight-prepared-visual"}}, {raw.name, proxy.name, "images/landscape.jpg"})

    def test_proxy_changed_during_crop_analysis_cannot_be_rebased_as_verified(self):
        _raw, proxy = self._native_video_fixture()
        def replace_proxy(_source, _start, length, **_kwargs):
            proxy.write_bytes(b"unverified replacement proxy")
            return [{"start": 0, "duration": length, "crop": None, "confidence": "full-frame"}]
        with patch.object(highlight, "analyze_framing", side_effect=replace_proxy), self.assertRaisesRegex(RuntimeError, "native visual changed"):
            self.render()
        self.assertFalse(any(self.output.glob("tiktok_highlight_*.mp4")))

    def test_contact_is_added_once_to_clean_native_visuals_and_bound_to_media_history(self):
        logo = self.input / "contact.png"
        logo.write_bytes(b"selected contact artwork")
        media_ai_policy.write_media_provenance(logo, ai_generated=True, reasons=["Explicit artwork provenance."])
        with patch("app.full_variant._contact_logo", return_value=logo):
            result = self.render()
        graphs = [command[command.index("-filter_complex") + 1] for command in self.commands if "-an" in command]
        self.assertTrue(all(graph.count("overlay=") == 1 and "overlay=x=100:y=184" in graph for graph in graphs))
        media = json.loads(result.with_suffix(".media.json").read_text())
        contact = [item for item in media["inputs"] if item["kind"] == "highlight-contact-logo"]
        self.assertEqual(len(contact), 1)
        self.assertEqual(contact[0]["sha256"], hashlib.sha256(logo.read_bytes()).hexdigest())
        self.assertTrue(media["aiGenerated"])

    def test_unverified_master_fallback_fills_without_duplicating_burned_in_contact(self):
        piece = {"kind": "master", "assetPath": self.master, "assetStart": 20, "sourceStart": 20, "sourceEnd": 24, "crop": None}
        with patch.object(highlight, "analyze_fill_focus", return_value=[(0, .3), (4, .7)]), patch.object(highlight, "_run", side_effect=self._run):
            highlight._render_visual_section(piece, self.work / "fallback.mp4")
        graph = self.commands[-1][self.commands[-1].index("-filter_complex") + 1]
        self.assertNotIn("overlay=", graph)
        self.assertNotIn("boxblur", graph)
        self.assertIn("crop=1080:1920", graph)
        self.assertEqual(piece["focusPoints"], [(0, .3), (4, .7)])

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
