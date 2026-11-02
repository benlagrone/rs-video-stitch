import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.thumbnail_styles import generate_project_thumbnail


def _has_drawtext():
    if not shutil.which("ffmpeg"):
        return False
    return "drawtext" in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class ThumbnailStylesTests(unittest.TestCase):
    @unittest.skipUnless(_has_drawtext(), "FFmpeg drawtext is required for card integration")
    def test_black_card_works_without_scene_images_and_localizes_brokerage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = {"title": "2438 White Oak | 休斯顿 Heights 房产参观", "hyperframes": {"leadCardRight1": "挂牌经纪公司：LeCrown Properties", "leadCardRight2": "经纪人：Jie Huang"}}
            thumbnail = generate_project_thumbnail(root / "input", root / "output", root / "work", {"scenes": []}, state, "black-agent-card")
            self.assertEqual(thumbnail.name, "thumbnail.jpg")
            self.assertLess(thumbnail.stat().st_size, 2_000_000)
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(thumbnail)]))
            self.assertEqual((probe["streams"][0]["width"], probe["streams"][0]["height"]), (1280, 720))
            self.assertEqual((root / "work" / "thumbnail-brokerage.txt").read_text(), "挂牌经纪公司：LeCrown Properties")

    def test_first_frame_prefers_completed_video_over_source_and_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            # A red first frame and blue remaining frames make a later seek observable.
            video = output / "latest.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=red:s=1280x720:r=2:d=0.5", "-f", "lavfi", "-i", "color=blue:s=1280x720:r=2:d=0.5", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", str(video)], check=True)
            thumbnail = generate_project_thumbnail(root / "input", output, root / "work", {"sourceFootage": [{"filename": "missing.mov"}]}, {"outputName": video.name}, "first-frame")
            pixel = subprocess.check_output(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(thumbnail), "-vf", "crop=2:2:10:10,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"])
            self.assertGreater(pixel[0], 200)
            self.assertLess(pixel[1], 40)
            self.assertLess(pixel[2], 40)

    @unittest.skipUnless(_has_drawtext(), "FFmpeg drawtext is required for card integration")
    def test_bible_black_card_has_no_property_agent_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            generate_project_thumbnail(root / "input", root / "output", root / "work", {}, {"workflow": "bible-video", "title": "Genesis 1"}, "black-agent-card")
            self.assertFalse((root / "work" / "thumbnail-brokerage.txt").exists())
            self.assertFalse((root / "work" / "thumbnail-phone.txt").exists())

    def test_first_frame_requires_a_visual_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(FileNotFoundError):
                generate_project_thumbnail(root / "input", root / "output", root / "work", {}, {}, "first-frame")


class ThumbnailCardPlanningTests(unittest.TestCase):
    def test_card_uses_persisted_copy_and_circular_portrait_without_visual_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            commands = []
            def run(command, _log=None):
                commands.append(command)
                Path(command[-1]).write_bytes(b"jpeg")
                return ""
            with patch("app.thumbnail_styles._font", return_value=root / "font.ttf"), patch("app.thumbnail_styles._run", side_effect=run):
                generate_project_thumbnail(root / "input", root / "output", root / "work", {}, {"title": "2438 White Oak | Houston Heights", "hyperframes": {"leadCardRight1": "挂牌经纪公司：LeCrown Properties", "leadCardRight2": "经纪人：Jie Huang"}}, "black-agent-card")
            graph = commands[-1][commands[-1].index("-filter_complex") + 1]
            self.assertIn("color=c=0x090f1c:s=1280x720:d=0.1", commands[-1])
            self.assertIn("crop=360:360:395:35", graph)
            self.assertIn("pow(X-W/2,2)", graph)
            self.assertIn("expansion=none", graph)
            self.assertEqual((root / "work" / "thumbnail-brokerage.txt").read_text(), "挂牌经纪公司：LeCrown Properties")

    def test_bible_card_does_not_add_a_real_estate_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def run(command, _log=None):
                Path(command[-1]).write_bytes(b"jpeg")
                return ""
            with patch("app.thumbnail_styles._font", return_value=root / "font.ttf"), patch("app.thumbnail_styles._run", side_effect=run):
                generate_project_thumbnail(root / "input", root / "output", root / "work", {}, {"title": "Genesis 1", "workflow": "bible-video"}, "black-agent-card")
            self.assertEqual((root / "work" / "thumbnail-label.txt").read_text(), "SCRIPTURE VIDEO")
            self.assertFalse((root / "work" / "thumbnail-brokerage.txt").exists())


if __name__ == "__main__":
    unittest.main()
