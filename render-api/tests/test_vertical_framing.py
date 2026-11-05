import json
import importlib.util
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

    def test_fill_is_explicit_and_has_no_blurred_or_inset_picture(self):
        self.assertEqual(framing.vertical_filter(), framing.vertical_filter(mode="fit"))
        graph = framing.vertical_filter(mode="fill", focus_points=[(0, .25), (2, .75)])
        self.assertNotIn("boxblur", graph)
        self.assertNotIn("overlay", graph)
        self.assertNotIn("split", graph)
        self.assertIn("force_original_aspect_ratio=increase", graph)
        self.assertIn("setpts=PTS-STARTPTS,crop=1080:1920", graph)
        self.assertIn("min(iw-ow", graph)
        self.assertIn("0.250000+(0.500000)*min(max", graph)
        self.assertNotIn("if(", graph)

    def test_focus_validation_rejects_expression_injection_and_invalid_points(self):
        for points in ([(0, "if(t,1,0)")], [(0, float("nan"))], [(0, 1.1)], [(-1, .5)],
                       [(0, .5), (0, .6)], [(0, .5), (.0000001, .6)], [(2, .5), (1, .5)], [(float("inf"), .5)],
                       [(index, .5) for index in range(97)]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                framing.vertical_filter(mode="fill", focus_points=points)
        with self.assertRaises(ValueError):
            framing.vertical_filter(mode="arbitrary")

    def test_focus_smoothing_limits_pan_speed_and_preserves_a_shot_cut(self):
        rows = [(0, .2, False, False)] + [(index / 2, .8, False, False) for index in range(1, 21)]
        points = framing._smooth_focus_samples(rows, 11)
        self.assertTrue(all(abs(right[1] - left[1]) / (right[0] - left[0]) <= .07501 for left, right in zip(points, points[1:])))
        self.assertEqual(points[0], (0.0, .2))
        self.assertEqual(points[-1][0], 11)
        cut = framing._smooth_focus_samples([(0, .2, False, False), (1, .8, True, False), (2, .8, False, False)], 3)
        self.assertIn((.999, .2), cut)
        self.assertIn((1.0, .8), cut)

    def test_focus_analysis_without_installed_cv_preserves_neutral_framing(self):
        with patch.dict("sys.modules", {"cv2": None}):
            self.assertEqual(framing.analyze_fill_focus(Path("missing.mp4"), 10, 4), [(0.0, .5), (4.0, .5)])

    def test_offline_smoothing_reaches_a_sustained_subject_without_causal_lag(self):
        rows = [(index / 2, .5 if index < 6 else .25, False, False) for index in range(15)]
        points = framing._smooth_focus_samples(rows, 7.5)
        def at(second):
            for first, last in zip(points, points[1:]):
                if second <= last[0]:
                    return first[1] + (last[1] - first[1]) * (second - first[0]) / (last[0] - first[0])
            return points[-1][1]
        self.assertEqual(points[0], (0.0, .5))
        self.assertLess(at(2.5), .49)  # a known future shot can begin its pan early
        self.assertLess(at(3.5), .35)  # no several-second tail after the subject
        self.assertTrue(all(abs(right[1] - left[1]) / (right[0] - left[0]) <= .07501 for left, right in zip(points, points[1:])))


@unittest.skipUnless(importlib.util.find_spec("cv2") and importlib.util.find_spec("numpy"), "OpenCV focus tests run in the API image")
class FillFocusVisionTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.frame = np.full((180, 320), 190, dtype=np.uint8)
        # A feature-rich open-room cluster on the right, with a large isolated
        # doorway edge in the center that must not dominate the composition.
        self.frame[:, 150:156] = 20
        for y in range(40, 145, 8):
            for x in range(220, 305, 8):
                self.frame[y:y + 4, x:x + 4] = 30

    def test_detail_cluster_wins_over_a_large_blank_partition_edge(self):
        with patch.object(framing, "_face_detector", return_value=None):
            anchor, face = framing._focus_candidate(self.frame, .3164)
        self.assertGreater(anchor, .65)
        self.assertFalse(face)

    def test_confident_face_has_priority_over_background_texture(self):
        detector = unittest.mock.Mock()
        detector.detectMultiScale.return_value = self.np.array([[30, 35, 42, 42]])
        with patch.object(framing, "_face_detector", return_value=detector):
            anchor, face = framing._focus_candidate(self.frame, .3164)
        self.assertLess(anchor, .30)
        self.assertTrue(face)

    def test_credible_center_is_not_abandoned_for_busier_edge_foliage(self):
        for y in range(40, 145, 8):
            for x in range(125, 195, 8):
                self.frame[y:y + 4, x:x + 4] = 30
        with patch.object(framing, "_face_detector", return_value=None):
            anchor, face = framing._focus_candidate(self.frame, .3164)
        self.assertEqual(anchor, .5)
        self.assertFalse(face)

    def test_blank_picture_holds_prior_focus_without_panning(self):
        with patch.object(framing, "_face_detector", return_value=None):
            anchor, face = framing._focus_candidate(self.np.full((180, 320), 120, dtype=self.np.uint8), .3164, .7)
        self.assertEqual(anchor, .7)
        self.assertFalse(face)

    def test_isolated_false_face_does_not_override_the_structural_subject(self):
        detector = unittest.mock.Mock()
        detector.detectMultiScale.side_effect = [self.np.array([[30, 35, 42, 42]]), self.np.empty((0, 4))]
        history = {}
        with patch.object(framing, "_face_detector", return_value=detector):
            first = framing._confirmed_focus_candidate(self.frame, .3164, None, history)
            second = framing._confirmed_focus_candidate(self.frame, .3164, first[0], history)
        self.assertGreater(first[0], .65)
        self.assertGreater(second[0], .65)
        self.assertFalse(first[1])
        self.assertFalse(second[1])
        self.assertEqual(history, {})
        self.assertEqual(detector.detectMultiScale.call_count, 2)

    def test_nearby_persistent_face_can_override_the_background(self):
        detector = unittest.mock.Mock()
        detector.detectMultiScale.side_effect = [self.np.array([[30, 35, 42, 42]]), self.np.array([[32, 35, 42, 42]])]
        history = {}
        with patch.object(framing, "_face_detector", return_value=detector):
            first = framing._confirmed_focus_candidate(self.frame, .3164, None, history)
            second = framing._confirmed_focus_candidate(self.frame, .3164, first[0], history)
        self.assertFalse(first[1])
        self.assertTrue(second[1])
        self.assertLess(second[0], .30)


@unittest.skipUnless(os.getenv("MEDIASTUDIO_FRAMING_FFMPEG_TEST") == "1" and shutil.which("ffmpeg") and shutil.which("ffprobe"), "Set MEDIASTUDIO_FRAMING_FFMPEG_TEST=1 for decoding/encoding QA")
class VerticalFramingEncodingTests(unittest.TestCase):
    def test_full_bounded_anchor_curve_encodes_without_nested_expression_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "many-anchors.mp4"
            points = [(index / 50, .2 if index % 2 else .8) for index in range(96)]
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=2",
                "-filter_complex", framing.vertical_filter(mode="fill", focus_points=points, width=180, height=320),
                "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(output),
            ], check=True)
            video = next(stream for stream in framing._probe(output)["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (180, 320))

    def test_real_fill_encode_follows_anchors_and_has_no_blurred_bands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "fill.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=green:s=640x360:r=10:d=2,drawbox=x=35:y=0:w=200:h=360:color=red:t=fill,drawbox=x=410:y=0:w=200:h=360:color=blue:t=fill",
                "-filter_complex", framing.vertical_filter(mode="fill", focus_points=[(0, .2), (1, .8)], width=180, height=320),
                "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(output),
            ], check=True)
            for second, channel in ((.1, 0), (1.8, 2)):
                decoded = subprocess.run([
                    "ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output), "-frames:v", "1",
                    "-vf", "format=rgb24", "-f", "rawvideo", "pipe:1",
                ], capture_output=True, check=True).stdout
                for x, y in ((90, 10), (90, 160), (90, 309)):
                    pixel = decoded[(y * 180 + x) * 3:(y * 180 + x) * 3 + 3]
                    self.assertGreater(pixel[channel], 180)
                    self.assertLess(pixel[2 if channel == 0 else 0], 60)

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
