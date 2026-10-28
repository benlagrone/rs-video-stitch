import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import bible_workflow


class GuidedVideoWorkflowTests(unittest.TestCase):
    def test_reviewed_blender_video_is_passed_as_model_guidance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            guide_path = Path(temp_dir) / "scene_001-3d-guide.mp4"
            depth_path = Path(temp_dir) / "scene_001-3d-depth.mp4"
            guide_path.write_bytes(b"guide")
            depth_path.write_bytes(b"depth")
            output_path = Path(temp_dir) / "scene_001.mp4"
            scene = {"previsualization": {"video": guide_path.name}, "timeline": [{}]}
            document = {"info": {"name": "Genesis 1"}, "scenes": [scene]}
            with mock.patch.object(
                bible_workflow,
                "guided_video_inputs",
                return_value=(
                    "One planet descends.",
                    {"summary": "one vector"},
                    guide_path,
                    depth_path,
                ),
            ), mock.patch.object(
                bible_workflow,
                "animate_bible_scene",
                return_value=output_path,
            ) as animate, mock.patch.object(
                bible_workflow,
                "scene_animation_context",
                return_value=(document, scene, Path(temp_dir) / "still.png", output_path),
            ), mock.patch.object(bible_workflow, "save_scenes"):
                result = bible_workflow.animate_bible_scene_from_previsualization(
                    "bible-genesis-1",
                    1,
                    progress=lambda _stage, _value: None,
                    log=lambda _message: None,
                )

        self.assertEqual(result, output_path)
        self.assertEqual(animate.call_args.kwargs["guidance_video_path"], guide_path)
        self.assertEqual(animate.call_args.kwargs["guidance_depth_path"], depth_path)
        self.assertEqual(animate.call_args.args[4]["summary"], "one vector")


if __name__ == "__main__":
    unittest.main()
