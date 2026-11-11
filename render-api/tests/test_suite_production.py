import json
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.suite_production import (
    SuiteProductionError,
    apply_production_package,
    approve_plan,
    attach_scene_clip,
    parse_production_package,
)


SCENES = [
    {"title": "Arrival", "VO": "A polished lobby welcomes guests.", "images": ["GK-7.JPG"]},
    {"title": "Office", "VO": "Furnished private offices are available.", "images": ["GK-26.JPG"]},
]


def package():
    return {
        "scenes": [
            {
                "scene": 1,
                "visualPrompt": "Keep the lobby photograph and add one person walking to the desk.",
                "motionPlan": {
                    "duration_seconds": 4,
                    "framing": "locked lobby",
                    "actors": [{"id": "guest", "role": "fictional visitor"}],
                    "action_beats": [
                        {"seconds": "0.0-2.0", "action": "walk toward the desk"},
                        {"seconds": "2.0-4.0", "action": "stop and look at the directory"},
                    ],
                    "collision_zones": ["stay on the floor"],
                },
            },
            {
                "scene": 2,
                "visualPrompt": "Keep the office photograph and add a seated person.",
                "motionPlan": {
                    "duration_seconds": 4,
                    "framing": "locked office",
                    "actors": [{"id": "worker", "role": "fictional office worker"}],
                    "action_beats": [
                        {"seconds": "0.0-4.0", "action": "sit and turn toward the desk"},
                    ],
                },
            },
        ]
    }


class SuiteProductionTest(TestCase):
    def test_package_keeps_one_plan_per_scene_section(self):
        prepared = apply_production_package(SCENES, package())

        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[0]["planStatus"], "draft")
        self.assertIn("lobby", prepared[0]["visualPrompt"])
        self.assertEqual(prepared[1]["motionPlan"]["duration_seconds"], 4)

    def test_approval_does_not_accept_model_python(self):
        prepared = apply_production_package(SCENES, package())[0]
        prepared["motionPlan"]["python"] = "import os; os.system('echo bad')"
        approved = approve_plan(prepared)

        self.assertEqual(approved["planStatus"], "approved")
        self.assertEqual(approved["renderStatus"], "queued")
        self.assertNotIn("python", approved["motionPlan"])

    def test_finished_scene_clip_is_what_the_youtube_stitch_plays(self):
        scene = {"title": "Arrival", "images": ["GK-7.JPG", "GK-18.JPG"], "timeline": [{"image": "GK-7.JPG"}, {"image": "GK-18.JPG"}]}
        updated = attach_scene_clip(scene, "scene-002-scene.mp4", "phronesis")

        self.assertEqual(updated["timeline"][0]["video"], "scene-002-scene.mp4")
        self.assertEqual(updated["timeline"][0]["motionGeneration"]["provider"], "phronesis")
        self.assertEqual(updated["timeline"][1]["image"], "GK-18.JPG")

    def test_parse_rejects_a_short_package(self):
        raw = json.dumps({"scenes": package()["scenes"][:1]})
        with self.assertRaises(SuiteProductionError):
            parse_production_package(raw, SCENES)
