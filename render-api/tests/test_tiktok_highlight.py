import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import tiktok_highlight as highlight
from app import media_ai_policy


class HighlightPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.output = self.root / "output"
        self.work = self.root / "work"
        for path in (self.input, self.output, self.work):
            path.mkdir()
        self.master = self.output / "youtube-master.mp4"
        self.master.write_bytes(b"unchanged source master")
        self.state = {"title": "2438 White Oak | Houston Heights Property Tour", "language": "en", "script": "Tour the home. Contact Jie for details."}
        for name, value in (("p_input", self.input), ("p_output", self.output), ("p_work", self.work)):
            mock = patch.object(highlight, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(highlight, "read_project_state", return_value=self.state)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(highlight, "_duration", return_value=120.0)
        self.duration = mock.start()
        self.addCleanup(mock.stop)

    def plan(self, seconds=45):
        return highlight.plan_highlight("project", self.master.name, seconds)

    def test_automatic_plan_is_in_range_and_graphics_are_included_in_the_total(self):
        for seconds in (30, 45, 60):
            with self.subTest(seconds=seconds):
                plan = self.plan(seconds)
                total = sum(row["sourceEnd"] - row["sourceStart"] for row in plan["segments"])
                total += 2 + 4 + 0.4 * (len(plan["segments"]) - 1)
                self.assertAlmostEqual(total, seconds, places=2)
                self.assertEqual(plan["captionSource"], "project-text")
                self.assertEqual(plan["sourceFilename"], self.master.name)
                self.assertTrue(all(row["caption"] for row in plan["segments"]))
                self.assertTrue(all(row["sourceStart"] >= 0 and row["sourceEnd"] <= 120 for row in plan["segments"]))

    def test_planning_does_not_mutate_the_source_or_saved_project(self):
        before = self.master.read_bytes(), self.master.stat().st_mtime_ns
        state = copy.deepcopy(self.state)
        self.plan()
        self.assertEqual((self.master.read_bytes(), self.master.stat().st_mtime_ns), before)
        self.assertEqual(self.state, state)
        self.assertEqual(list(self.output.iterdir()), [self.master])

    def test_short_source_is_rejected_instead_of_repeating_footage(self):
        self.duration.return_value = 10
        with self.assertRaisesRegex(ValueError, "too short"):
            self.plan(30)

    def test_unmapped_video_uses_a_neutral_listing_caption_not_an_invented_room(self):
        self.state["script"] = "A generous garden. A separate apartment."
        plan = self.plan()
        self.assertEqual(plan["selectionMethod"], "distributed-source-windows")
        self.assertTrue(all(segment["caption"] == self.state["title"] for segment in plan["segments"]))
        self.assertIn("A generous garden.", plan["suggestedCaptionCopy"])

    def test_verified_section_timing_uses_the_saved_source_clip_title(self):
        footage = self.work / "footage"
        footage.mkdir()
        for index in range(2):
            (footage / f"clip_{index:02d}.mp4").write_bytes(b"source proxy")
        (self.input / "scenes.json").write_text(json.dumps({"sourceFootage": [{"filename": "front.mov", "title": "Front Porch and Entry"}, {"filename": "inside.mov", "title": "Interior Tour"}]}))
        self.duration.side_effect = lambda path: 120 if path.resolve() == self.master.resolve() else 60
        plan = self.plan(30)
        self.assertEqual(plan["selectionMethod"], "verified-source-section-ranges")
        for segment in plan["segments"]:
            if segment["sourceStart"] < 60:
                self.assertLessEqual(segment["sourceEnd"], 60)
                self.assertEqual(segment["caption"], "Front Porch and Entry")
            else:
                self.assertEqual(segment["caption"], "Interior Tour")

    def test_mandarin_and_only_verified_contact_details_are_preserved(self):
        self.state.update({"title": "2438 White Oak 房产参观", "language": "zh-CN", "tiktokHighlightContact": {"email": "jie@example.test"}})
        plan = self.plan()
        self.assertEqual(plan["language"], "zh-CN")
        self.assertEqual(plan["contact"]["email"], "jie@example.test")
        del self.state["tiktokHighlightContact"]
        self.assertEqual(self.plan()["contact"]["email"], "")

    def test_legacy_use_intro_is_counted_when_verifying_source_section_ranges(self):
        footage = self.work / "footage"
        footage.mkdir()
        for index in range(2):
            (footage / f"clip_{index:02d}.mp4").write_bytes(b"source proxy")
        intro = self.work / "hyperframes_intro.mp4"
        intro.write_bytes(b"intro proxy")
        (self.input / "scenes.json").write_text(json.dumps({"sourceFootage": [{"filename": "front.mov", "title": "Front Porch and Entry"}, {"filename": "inside.mov", "title": "Interior Tour"}]}))
        self.state["useIntro"] = True
        self.state["renderOptions"] = {}
        self.duration.side_effect = lambda path: 120 if path.resolve() == self.master.resolve() else (10 if path == intro else 55)
        plan = self.plan(30)
        self.assertEqual(plan["selectionMethod"], "verified-source-section-ranges")
        self.assertTrue(all(segment["sourceStart"] >= 10 for segment in plan["segments"]))
        self.assertEqual({segment["caption"] for segment in plan["segments"]}, {"Front Porch and Entry", "Interior Tour"})

    def test_explicit_intro_disabled_overrides_legacy_use_intro(self):
        footage = self.work / "footage"
        footage.mkdir()
        for index in range(2):
            (footage / f"clip_{index:02d}.mp4").write_bytes(b"source proxy")
        (self.work / "hyperframes_intro.mp4").write_bytes(b"old intro proxy")
        (self.input / "scenes.json").write_text(json.dumps({"sourceFootage": [{"filename": "front.mov", "title": "Front Porch and Entry"}, {"filename": "inside.mov", "title": "Interior Tour"}]}))
        self.state.update({"useIntro": True, "renderOptions": {"introEnabled": False}})
        self.duration.side_effect = lambda path: 110 if path.resolve() == self.master.resolve() else 55
        plan = self.plan(30)
        self.assertEqual(plan["selectionMethod"], "verified-source-section-ranges")
        self.assertEqual({segment["caption"] for segment in plan["segments"]}, {"Front Porch and Entry", "Interior Tour"})

    def test_tampered_source_paths_and_other_highlight_sources_are_rejected(self):
        for filename in ("../youtube-master.mp4", "folder\\master.mp4", "thumbnail.jpg", "tiktok_highlight_master.mp4"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                highlight.plan_highlight("project", filename, 30)

    def test_changed_master_requires_a_fresh_plan(self):
        plan = self.plan()
        self.master.write_bytes(b"new rendered version")
        with self.assertRaisesRegex(ValueError, "source video changed"):
            highlight.validate_plan("project", plan)

    def test_source_fingerprint_survives_javascript_number_round_trip(self):
        timestamp = 2 ** 60 + 12345
        os.utime(self.master, ns=(timestamp, timestamp))
        plan = self.plan()
        fingerprint = plan["sourceFingerprint"]
        self.assertIsInstance(fingerprint["mtimeNs"], str)
        self.assertEqual(fingerprint["mtimeNs"], str(self.master.stat().st_mtime_ns))
        self.assertGreater(int(fingerprint["mtimeNs"]), 2 ** 53)
        # JavaScript JSON.parse treats every JSON number as a double. The
        # timestamp string must retain every digit through that browser path.
        browser_plan = json.loads(json.dumps(plan), parse_int=float)
        validated = highlight.validate_plan("project", browser_plan)
        self.assertEqual(validated["sourceFingerprint"], fingerprint)
        self.master.write_bytes(b"a different completed render")
        with self.assertRaisesRegex(ValueError, "source video changed"):
            highlight.validate_plan("project", browser_plan)

    def test_invalid_ranges_and_repeated_footage_are_rejected(self):
        plan = self.plan()
        for field, value in (("sourceStart", -1), ("sourceEnd", 130), ("sourceStart", float("nan"))):
            invalid = copy.deepcopy(plan)
            invalid["segments"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                highlight.validate_plan("project", invalid)
        invalid = copy.deepcopy(plan)
        invalid["segments"][1].update({key: invalid["segments"][0][key] for key in ("sourceStart", "sourceEnd")})
        with self.assertRaisesRegex(ValueError, "overlapping"):
            highlight.validate_plan("project", invalid)

    def test_too_long_edited_plan_and_bad_contact_data_are_rejected(self):
        plan = self.plan()
        plan["durationSeconds"] = 61
        with self.assertRaises(ValueError):
            highlight.validate_plan("project", plan)
        plan = self.plan()
        plan["contact"] = "wrong type"
        with self.assertRaisesRegex(ValueError, "contact"):
            highlight.validate_plan("project", plan)

    def test_caption_ass_escapes_formatting_and_places_cjk_inside_the_safe_area(self):
        path = self.work / "caption.ass"
        highlight._write_ass(path, "宽敞的客厅与舒适空间 {\\pos(0,0)}", 4.5)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Noto Sans CJK SC", text)
        self.assertIn(",100,210,675,1", text)
        self.assertIn("\\{", text)
        self.assertIn("宽敞", text)

    def test_long_cjk_feature_caption_keeps_every_character_in_sequential_safe_groups(self):
        caption = "宽敞明亮的客厅连接舒适的用餐空间欢迎预约参观" * 7
        caption = caption[:180]
        groups = highlight._caption_schedule(caption, 6.0)
        self.assertGreater(len(caption), 56)
        self.assertGreater(len(groups), 1)
        self.assertEqual("".join(text.replace("\n", "") for _start, _end, text in groups), caption)
        self.assertTrue(all(len(text.splitlines()) <= 3 for _start, _end, text in groups))
        self.assertTrue(all(len(line) <= 14 for _start, _end, text in groups for line in text.splitlines()))
        path = self.work / "long-cjk.ass"
        highlight._write_ass(path, caption, 6.0)
        dialogue = [line.split(",", 9)[-1].replace("\\N", "") for line in path.read_text().splitlines() if line.startswith("Dialogue:")]
        self.assertEqual("".join(dialogue), caption)
        self.assertEqual(len(dialogue), len(groups))
        self.assertAlmostEqual(groups[0][0], 0.12)
        self.assertAlmostEqual(groups[-1][1], 5.92)
        self.assertTrue(all(left[1] <= right[0] + 1e-9 for left, right in zip(groups, groups[1:])))

    def test_more_than_four_english_lines_and_unbroken_words_are_not_dropped(self):
        caption = "A welcoming home with bright spaces and useful details for every visitor to discover during a private property tour with Jie Huang and LeCrown Properties in Houston."
        self.assertGreater(len(highlight._caption_lines(caption).splitlines()), 4)
        groups = highlight._caption_schedule(caption, 7.0)
        self.assertEqual(" ".join(text.replace("\n", " ") for _start, _end, text in groups), caption)
        self.assertTrue(all(len(text.splitlines()) <= 3 for _start, _end, text in groups))
        unbroken = "x" * 180
        long_groups = highlight._caption_schedule(unbroken, 7.0)
        self.assertEqual("".join(text.replace("\n", "") for _start, _end, text in long_groups), unbroken)
        self.assertTrue(all(len(line) <= 29 for _start, _end, text in long_groups for line in text.splitlines()))


@unittest.skipUnless(os.getenv("MEDIASTUDIO_HIGHLIGHT_FFMPEG_TEST") == "1" and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Set MEDIASTUDIO_HIGHLIGHT_FFMPEG_TEST=1 for actual encoding QA")
class HighlightEncodingTests(unittest.TestCase):
    def test_real_30_second_output_preserves_source_and_has_audio_captions_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            work = root / "work"
            input_dir = root / "input"
            for folder in (output, work, input_dir):
                folder.mkdir()
            master = output / "tour.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=40", "-f", "lavfi", "-i", "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(master)], check=True)
            before_hash = hashlib.sha256(master.read_bytes()).hexdigest()
            before_mtime = master.stat().st_mtime_ns
            media_ai_policy.write_media_provenance(master, ai_generated=False, reasons=["Deterministic test footage; no AI-generated media."])
            counter = []

            def graphics(output_dir, *, kind, duration, **_kwargs):
                card = Path(output_dir) / f"fake_graphic_{len(counter)}.mp4"
                counter.append(kind)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=navy:s=160x284:r=30:d={duration}", "-c:v", "libx264", "-preset", "ultrafast", str(card)], check=True)
                return card

            with patch.object(highlight, "p_input", return_value=input_dir), patch.object(highlight, "p_output", return_value=output), patch.object(media_ai_policy, "p_output", return_value=output), patch.object(highlight, "p_work", return_value=work), patch.object(highlight, "read_project_state", return_value={"title": "Property tour"}), patch("app.tiktok_highlight_graphics.render_highlight_graphics", side_effect=graphics):
                plan = highlight.plan_highlight("project", master.name, 30)
                progress = []
                result = highlight.render_highlight("project", plan, progress=lambda stage, value: progress.append(value))
            self.assertNotEqual(result, master)
            self.assertEqual(hashlib.sha256(master.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(master.stat().st_mtime_ns, before_mtime)
            probe = highlight._probe(result)
            video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertTrue(any(stream["codec_type"] == "audio" for stream in probe["streams"]))
            self.assertAlmostEqual(float(probe["format"]["duration"]), 30, delta=0.15)
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(counter[0], "hook")
            self.assertEqual(counter[-1], "outro")
            self.assertIn("transition", counter)
            self.assertIn("Property tour", result.with_suffix(".srt").read_text())
            provenance = json.loads(result.with_suffix(".highlight.json").read_text())
            self.assertTrue(provenance["sourceUnchanged"])
            self.assertEqual(provenance["captionSource"], "project-text")
            self.assertFalse(provenance["sourceAiMedia"]["isAigc"])
            self.assertFalse(json.loads(result.with_suffix(".media.json").read_text())["aiGenerated"])
