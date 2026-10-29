import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import blender_previs
from app.gpu_admission import GpuAdmissionError


class BlenderPrevisTests(unittest.TestCase):
    def test_genesis_one_one_fallback_translates_prompts_into_one_descending_planet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_input = Path(temp_dir) / "input"
            project_input.mkdir()
            (project_input / "scenes.json").write_text(json.dumps({
                "scenes": [{
                    "title": "Genesis 1:1",
                    "VO": "In the beginning God created the heaven and the earth.",
                    "motionPrompt": "The planet begins higher and moves lower into the scene.",
                    "motionPlan": {"summary": "Move one planet downward."},
                    "timeline": [{"prompt": "One solitary newly forming planet in a primordial void."}],
                }],
            }), encoding="utf-8")
            with mock.patch.object(blender_previs, "p_input", return_value=project_input), mock.patch.object(
                blender_previs, "governed_post", side_effect=blender_previs.requests.RequestException("offline")
            ):
                brief = blender_previs.generate_scene_3d_brief("genesis", 1)

        planets = [item for item in brief["objects"] if item["id"] == "forming-earth"]
        self.assertEqual(len(planets), 1)
        self.assertGreater(planets[0]["startPosition"][2], planets[0]["endPosition"][2])
        self.assertIn("One solitary newly forming planet", brief["imagePrompt"])
        self.assertIn("moves lower", brief["motionPrompt"])
        self.assertIn("no duplicate world", brief["videoPrompt"])
        self.assertEqual(brief["source"], "deterministic-fallback")

    def test_genesis_one_two_fallback_stages_fixed_world_waves_and_spirit_light(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_input = Path(temp_dir) / "input"
            project_input.mkdir()
            (project_input / "scenes.json").write_text(json.dumps({
                "scenes": [{
                    "title": "Genesis 1:2",
                    "VO": "The Spirit of God moved upon the face of the waters.",
                    "motionPrompt": "Waves advance while diffuse light crosses the water.",
                    "motionPlan": {"summary": "Move light across advancing waves."},
                    "timeline": [{"prompt": "One enormous world above a dark primordial ocean."}],
                }],
            }), encoding="utf-8")
            with mock.patch.object(blender_previs, "p_input", return_value=project_input), mock.patch.object(
                blender_previs, "governed_post", side_effect=blender_previs.requests.RequestException("offline")
            ):
                brief = blender_previs.generate_scene_3d_brief("genesis", 1)

        world = next(item for item in brief["objects"] if item["id"] == "unfinished-earth")
        waves = [item for item in brief["objects"] if item["id"] in {"near-wave", "far-wave"}]
        spirit = next(item for item in brief["objects"] if item["id"] == "spirit-light")
        self.assertEqual(world["startPosition"], world["endPosition"])
        self.assertTrue(all(item["endPosition"][1] < item["startPosition"][1] for item in waves))
        self.assertGreater(spirit["endPosition"][0], spirit["startPosition"][0])
        self.assertIn("never by a person, orb, or solid object", " ".join(brief["continuityRules"]))
        self.assertIn("dark primordial ocean", brief["videoPrompt"])
        self.assertEqual(brief["source"], "deterministic-fallback")

    def test_sanitizer_rejects_arbitrary_blender_primitives(self):
        scene = {"title": "Psalm 1:3"}
        fallback = blender_previs._generic_fallback_brief(scene, "Tree by water", "Leaves move")
        brief = blender_previs._sanitize_brief({
            "objects": [{
                "id": "tree", "label": "Tree", "primitive": "python-script", "scale": [1, 1, 2],
                "startPosition": [0, 0, 1], "endPosition": [0, 0, 1], "rotationDegrees": [0, 0, 0],
            }],
        }, fallback)

        self.assertEqual(brief["objects"][0]["primitive"], "sphere")

    def test_gpu_admission_failure_uses_cpu_safe_genesis_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_input = Path(temp_dir) / "input"
            project_input.mkdir()
            (project_input / "scenes.json").write_text(json.dumps({
                "scenes": [{
                    "title": "Genesis 1:1",
                    "VO": "In the beginning God created the heaven and the earth.",
                    "timeline": [{"prompt": "One solitary newly forming planet."}],
                }],
            }), encoding="utf-8")
            with mock.patch.object(blender_previs, "p_input", return_value=project_input), mock.patch.object(
                blender_previs, "governed_post", side_effect=GpuAdmissionError("optimizer timed out")
            ):
                brief = blender_previs.generate_scene_3d_brief("genesis", 1)

        self.assertEqual(brief["source"], "deterministic-fallback")
        self.assertEqual([item["id"] for item in brief["objects"]].count("forming-earth"), 1)

    def test_genesis_one_one_model_cannot_reverse_locked_planet_vector(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_input = Path(temp_dir) / "input"
            project_input.mkdir()
            (project_input / "scenes.json").write_text(json.dumps({
                "scenes": [{
                    "title": "Genesis 1:1",
                    "VO": "In the beginning God created the heaven and the earth.",
                    "timeline": [{"prompt": "One forming planet."}],
                }],
            }), encoding="utf-8")
            response = mock.Mock()
            response.json.return_value = {"response": json.dumps({
                "objects": [{
                    "id": "planet",
                    "label": "Planet",
                    "primitive": "sphere",
                    "scale": [1, 1, 1],
                    "material": "stone",
                    "startPosition": [0, 0, -2],
                    "endPosition": [0, 0, 2],
                    "rotationDegrees": [0, 0, 0],
                    "action": "The planet rises.",
                }],
                "videoPrompt": "The planet rises from below.",
            })}
            with mock.patch.object(
                blender_previs,
                "p_input",
                return_value=project_input,
            ), mock.patch.object(
                blender_previs,
                "governed_post",
                return_value=response,
            ):
                brief = blender_previs.generate_scene_3d_brief("genesis", 1)

        planet = next(item for item in brief["objects"] if item["id"] == "forming-earth")
        self.assertGreater(planet["startPosition"][2], planet["endPosition"][2])
        self.assertIn("moves smoothly downward", brief["videoPrompt"])

    def test_guided_video_inputs_require_and_return_reviewed_guide_track(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_input = Path(temp_dir) / "input"
            motion_dir = project_input / "motion"
            motion_dir.mkdir(parents=True)
            guide_path = motion_dir / "scene_001-3d-guide.mp4"
            depth_path = motion_dir / "scene_001-3d-depth.mp4"
            guide_path.write_bytes(b"reviewed-guide")
            depth_path.write_bytes(b"reviewed-depth")
            (project_input / "scenes.json").write_text(json.dumps({
                "scenes": [{
                    "title": "Genesis 1:1",
                    "previsualization": {
                        "status": "ready",
                        "video": guide_path.name,
                        "depthVideo": depth_path.name,
                        "brief": {
                            "videoPrompt": "One planet descends.",
                            "continuityRules": ["Exactly one planet is visible."],
                            "motionPlan": {"summary": "Move the planet lower."},
                        },
                    },
                }],
            }), encoding="utf-8")
            with mock.patch.object(blender_previs, "p_input", return_value=project_input):
                prompt, plan, resolved_guide, resolved_depth = blender_previs.guided_video_inputs(
                    "genesis",
                    1,
                )

        self.assertIn("One planet descends", prompt)
        self.assertIn("Exactly one planet", prompt)
        self.assertEqual(plan["summary"], "Move the planet lower.")
        self.assertEqual(resolved_guide, guide_path)
        self.assertEqual(resolved_depth, depth_path)


if __name__ == "__main__":
    unittest.main()
