import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import vertical_framing as framing


def sample(left=0, right=0, top=0, bottom=0, dark=False, logo=False):
    width, height = framing._SAMPLE_WIDTH, framing._SAMPLE_HEIGHT
    frame = bytearray(width * height)
    for y in range(top, height - bottom):
        for x in range(left, width - right):
            frame[y * width + x] = (8 if dark else 85 + ((x + y) % 120))
    if logo:
        for y in range(3, 15):
            for x in range(width - 30, width - 3):
                frame[y * width + x] = 200
    return bytes(frame)


class VerticalFramingTests(unittest.TestCase):
    def test_pillarboxed_portrait_is_unwrapped_even_with_a_corner_logo(self):
        bounds = framing._picture_bounds(sample(left=64, right=64, logo=True))
        self.assertEqual(bounds, (64, 0, 64, 108))
        crop = framing._pixel_crop(bounds, 1920, 1080)
        self.assertLess(crop["width"], crop["height"])
        self.assertLessEqual(crop["x"], 640)
        self.assertGreaterEqual(crop["x"] + crop["width"], 1280)

    def test_landscape_picture_without_bars_is_not_cropped(self):
        self.assertIsNone(framing._picture_bounds(sample()))

    def test_existing_letterbox_is_removed_before_vertical_fitting(self):
        self.assertEqual(framing._picture_bounds(sample(top=18, bottom=18)), (0, 18, 192, 72))

    def test_dark_or_asymmetric_scene_does_not_establish_padding(self):
        self.assertIsNone(framing._picture_bounds(sample(left=60, right=60, dark=True)))
        self.assertIsNone(framing._picture_bounds(sample(left=60, right=15)))
        self.assertIsNone(framing._picture_bounds(bytes(framing._SAMPLE_WIDTH * framing._SAMPLE_HEIGHT)))

    def test_dark_walls_with_only_a_lit_center_are_not_removed(self):
        width, height = framing._SAMPLE_WIDTH, framing._SAMPLE_HEIGHT
        frame = bytearray(width * height)
        for y in range(40, 68):
            for x in range(80, 112):
                frame[y * width + x] = 160
        self.assertIsNone(framing._picture_bounds(bytes(frame)))

    def test_mixed_orientation_creates_stable_per_shot_runs(self):
        portrait = {"x": 638, "y": 0, "width": 644, "height": 1080}
        runs = framing._framing_runs([portrait] * 20 + [None] * 20, 4.0, 1920, 1080, 1)
        self.assertEqual(len(runs), 2)
        self.assertEqual([(row["start"], row["duration"], row["orientation"]) for row in runs], [(0.0, 2.0, "portrait"), (2.0, 2.0, "landscape")])
        self.assertAlmostEqual(sum(row["duration"] for row in runs), 4)

    def test_isolated_noise_is_not_a_crop_and_nearby_bounds_do_not_jitter(self):
        portrait = {"x": 638, "y": 0, "width": 644, "height": 1080}
        self.assertIsNone(framing._framing_runs([None, portrait, None], .3, 1920, 1080, 1)[0]["crop"])
        shifted = {**portrait, "x": 640, "width": 642}
        runs = framing._framing_runs([portrait, shifted, portrait, shifted, portrait, portrait], .6, 1920, 1080, 1)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["duration"], .6)

    def test_short_landscape_insert_does_not_inherit_neighboring_portrait_crop(self):
        portrait = {"x": 638, "y": 0, "width": 644, "height": 1080}
        runs = framing._framing_runs([portrait] * 5 + [None] * 2 + [portrait] * 5, 1.2, 1920, 1080, 1)
        self.assertEqual(len(runs), 3)
        self.assertIsNone(runs[1]["crop"])
        self.assertAlmostEqual(runs[1]["duration"], .2)

    def test_rotation_and_pixel_aspect_are_part_of_display_geometry(self):
        probe = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1", "side_data_list": [{"rotation": -90}]}]}
        self.assertEqual(framing._geometry(probe), (1080, 1920, 1.0))
        probe["streams"][0].update(width=720, height=576, sample_aspect_ratio="16:15", side_data_list=[])
        self.assertEqual(framing._geometry(probe), (720, 576, 16 / 15))

    def test_filter_crops_before_adding_one_background_and_preserves_foreground(self):
        graph = framing.vertical_filter("1:v", "picture", crop={"x": 638, "y": 0, "width": 644, "height": 1080})
        self.assertTrue(graph.startswith("[1:v]crop=644:1080:638:0,"))
        self.assertEqual(graph.count("split=2"), 1)
        self.assertIn("force_original_aspect_ratio=decrease[picture_fg]", graph)
        self.assertTrue(graph.endswith("[picture]"))
        with self.assertRaises(ValueError):
            framing.vertical_filter("0:v;evil", "v")

    def test_decoder_failure_preserves_full_frame(self):
        probe = {"streams": [{"codec_type": "video", "width": 1080, "height": 1920}]}
        with patch.object(framing.subprocess, "run", side_effect=subprocess.SubprocessError("decode failed")):
            runs = framing.analyze_framing(Path("source.mp4"), 3, 6.1, probe)
        self.assertEqual(runs, [{"start": 0.0, "crop": None, "duration": 6.1, "orientation": "portrait", "confidence": "full-frame"}])


@unittest.skipUnless(os.getenv("MEDIASTUDIO_FRAMING_FFMPEG_TEST") == "1" and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Set MEDIASTUDIO_FRAMING_FFMPEG_TEST=1 for decoding/encoding QA")
class VerticalFramingEncodingTests(unittest.TestCase):
    def test_real_mixed_master_unwraps_portrait_and_keeps_landscape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            master = root / "mixed.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:r=10:d=2",
                "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=2", "-filter_complex",
                "[0:v]scale=202:360,setsar=1,pad=640:360:(ow-iw)/2:(oh-ih)/2[p];[p][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(master),
            ], check=True)
            before = master.read_bytes(), master.stat().st_mtime_ns
            runs = framing.analyze_framing(master, 0, 4)
            self.assertEqual(len(runs), 2)
            self.assertEqual([row["orientation"] for row in runs], ["portrait", "landscape"])
            self.assertAlmostEqual(runs[1]["start"], 2, delta=.11)
            output = root / "portrait.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-i", str(master), "-t", "1", "-filter_complex",
                framing.vertical_filter(crop=runs[0]["crop"], width=270, height=480), "-map", "[v]",
                "-c:v", "libx264", "-preset", "ultrafast", str(output),
            ], check=True)
            info = framing._probe(output)
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (270, 480))
            self.assertEqual((master.read_bytes(), master.stat().st_mtime_ns), before)
