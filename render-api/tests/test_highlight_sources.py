import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import highlight_sources as sources


class NativeSourceMapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.input, self.work = self.root / "input", self.root / "work"
        self.master = self.file("master.mp4")
        self.file("input/images/portrait.jpg")
        self.file("input/images/landscape.jpg")
        self.file("work/scene_00.mp4")
        self.file("work/scene_00/seg_00.mp4")
        self.file("work/scene_00/seg_01.mp4")
        self.document = {"scenes": [{"title": "Office", "images": ["portrait.jpg", "landscape.jpg"]}]}
        self.lengths = {"master.mp4": 6.024, "scene_00.mp4": 6.024, "seg_00.mp4": 2.0, "seg_01.mp4": 4.0}
        for name, path in (("p_input", self.input), ("p_work", self.work)):
            mock = patch.object(sources, name, return_value=path)
            mock.start()
            self.addCleanup(mock.stop)

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        return path

    def duration(self, path):
        return self.lengths[path.name]

    def mapping(self, **kwargs):
        with patch.object(sources, "_visual_match", return_value=True):
            return sources.native_source_map("project", self.master, self.document, {}, self.duration, **kwargs)

    def test_photo_map_reconciles_aac_padding_and_preserves_native_paths(self):
        mapping = self.mapping()
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping[0]["assetPath"], self.input / "images/portrait.jpg")
        self.assertEqual(mapping[1]["assetPath"], self.input / "images/landscape.jpg")
        self.assertAlmostEqual(mapping[0]["sourceEnd"], 2.008)
        self.assertAlmostEqual(mapping[-1]["sourceEnd"], 6.024)
        self.assertEqual(mapping[0]["kind"], "image")
        self.assertEqual(mapping[0]["assetTimeScale"], 0)
        self.assertIsInstance(mapping[0]["fingerprint"]["mtimeNs"], str)

    def test_legacy_transition_alias_and_intro_are_timed(self):
        self.file("work/hyperframes_intro.mp4")
        self.file("work/hyperframes_transition_00.mp4")
        self.file("work/scene_01.mp4")
        self.file("work/scene_01/seg_00.mp4")
        self.document["scenes"].append({"title": "Reception", "images": ["landscape.jpg"]})
        self.lengths.update({"hyperframes_intro.mp4": 2.4, "hyperframes_transition_00.mp4": 1.2, "scene_01.mp4": 2.0, "master.mp4": 11.624})
        # The same segment basename appears in another scene; use its path.
        def duration(path):
            return 2.0 if path.parent.name == "scene_01" else self.duration(path)
        with patch.object(sources, "_visual_match", return_value=True):
            mapping = sources.native_source_map("project", self.master, self.document, {"useIntro": True}, duration)
        self.assertEqual(len(mapping), 3)
        self.assertAlmostEqual(mapping[0]["sourceStart"], 2.4)
        self.assertAlmostEqual(mapping[-1]["sourceStart"], 9.624)
        self.assertEqual(mapping[-1]["sceneTitle"], "Reception")

    def test_equal_timing_cannot_substitute_an_asset_with_different_pixels(self):
        with patch.object(sources, "_visual_match", side_effect=[True, False]) as visual:
            mapping = sources.native_source_map("project", self.master, self.document, {}, self.duration)
        self.assertEqual(visual.call_count, 2)
        self.assertEqual(len(mapping), 1)
        self.assertEqual(mapping[0]["assetPath"].name, "portrait.jpg")

    def test_different_master_duration_does_not_reuse_retained_visuals(self):
        self.lengths["master.mp4"] = 20
        with patch.object(sources, "_visual_match") as visual:
            self.assertEqual(sources.native_source_map("project", self.master, self.document, {}, self.duration), [])
        visual.assert_not_called()

    def test_only_requested_windows_are_visually_verified(self):
        with patch.object(sources, "_visual_match", return_value=True) as visual:
            mapping = sources.native_source_map("project", self.master, self.document, {}, self.duration, windows=[(2.5, 4)])
        self.assertEqual(len(mapping), 1)
        self.assertEqual(visual.call_count, 1)
        self.assertEqual(mapping[0]["assetPath"].name, "landscape.jpg")

    def test_source_footage_uses_original_rotation_aware_video_and_timing(self):
        self.file("input/footage/recorded.mov")
        self.file("work/footage/clip_00.mp4")
        self.lengths.update({"recorded.mov": 6.0, "clip_00.mp4": 6.024})
        with patch.object(sources, "_visual_match", return_value=True):
            mapping = sources.native_source_map("project", self.master, {"sourceFootage": [{"filename": "recorded.mov", "title": "Entry"}]}, {}, self.duration)
        self.assertEqual(mapping[0]["assetPath"], self.input / "footage/recorded.mov")
        self.assertEqual(mapping[0]["kind"], "video")
        self.assertAlmostEqual(mapping[0]["assetTimeScale"], 6.0 / 6.024)

    def test_generated_motion_keeps_its_native_playback_rate(self):
        self.file("input/motion/pan.mp4")
        self.lengths["pan.mp4"] = 1.0
        self.document["scenes"][0]["timeline"] = [{"image": "portrait.jpg", "video": "pan.mp4"}]
        mapping = self.mapping()
        self.assertEqual(mapping[0]["assetPath"], self.input / "motion/pan.mp4")
        self.assertEqual(mapping[0]["kind"], "video")
        self.assertAlmostEqual(mapping[0]["assetTimeScale"], 1.0 / 2.008)

    def test_split_window_covers_transitions_and_tracks_native_offsets(self):
        mapping = [{"sourceStart": 2, "sourceEnd": 5, "kind": "video", "assetStart": 1, "assetTimeScale": 0.5, "assetPath": Path("native.mov")},
                   {"sourceStart": 7, "sourceEnd": 10, "kind": "image", "assetStart": 0, "assetTimeScale": 0, "assetPath": Path("photo.jpg")}]
        pieces = sources.split_window(mapping, 3, 11)
        self.assertEqual([piece["kind"] for piece in pieces], ["video", "master", "image", "master"])
        self.assertAlmostEqual(pieces[0]["assetStart"], 1.5)
        self.assertAlmostEqual(sum(piece["duration"] for piece in pieces), 8)
        self.assertEqual([(piece["sourceStart"], piece["sourceEnd"]) for piece in pieces], [(3, 5), (5, 7), (7, 10), (10, 11)])

    def test_missing_native_asset_is_a_master_fallback_region(self):
        (self.input / "images/portrait.jpg").unlink()
        pieces = sources.split_window(self.mapping(), 0, 6.024)
        self.assertEqual([piece["kind"] for piece in pieces], ["master", "image"])

    def test_window_rejects_invalid_timestamps(self):
        for start, end in ((-1, 2), (2, 2), (0, math.inf), (math.nan, 1)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                sources.split_window([], start, end)

    def test_similarity_rejects_blank_or_different_frames(self):
        self.assertFalse(sources._similar(bytes([0]) * 100, bytes([0]) * 100))
        varied = bytes([value for pixel in range(128 * 54) for value in (pixel % 256,) * 3])
        self.assertTrue(sources._similar(varied, varied))
        self.assertFalse(sources._similar(varied, bytes(255 - value for value in varied)))

    def test_still_verification_does_not_seek_the_image_demuxer(self):
        expected = bytes(128 * 54 * 3)
        result = subprocess.CompletedProcess([], 0, stdout=expected, stderr=b"")
        with patch.object(sources.subprocess, "run", return_value=result) as run:
            self.assertEqual(sources._frame.__wrapped__("photo.JPG", 20, "timestamp", 0), expected)
        self.assertNotIn("-ss", run.call_args.args[0])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg source-binding verification")
class NativeSourcePixelTests(unittest.TestCase):
    def test_native_photo_matches_its_master_but_a_replacement_photo_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native, proxy, master = root / "photo.png", root / "proxy.mp4", root / "master.mp4"
            def run(arguments):
                subprocess.run(["ffmpeg", "-v", "error", "-y", *arguments], check=True, capture_output=True)
            run(["-f", "lavfi", "-i", "testsrc2=s=160x240", "-frames:v", "1", str(native)])
            run(["-loop", "1", "-i", str(native), "-t", "1", "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(proxy)])
            run(["-i", str(proxy), "-c:v", "libx264", str(master)])
            item = {"sourceStart": 0.0, "sourceEnd": 1.0, "assetPath": native, "proxyPath": proxy,
                    "kind": "image", "assetStart": 0.0, "assetTimeScale": 0.0}
            self.assertTrue(sources._visual_match(master, item))
            run(["-f", "lavfi", "-i", "testsrc=s=160x240", "-frames:v", "1", str(native)])
            self.assertFalse(sources._visual_match(master, item))
