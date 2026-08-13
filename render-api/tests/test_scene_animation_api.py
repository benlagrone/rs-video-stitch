import unittest
from unittest import mock

from app import api
from app.schemas import SceneAnimationRequest


class _Database:
    def __init__(self):
        self.added = []
        self.committed = False

    def get(self, _model, _identifier):
        return None

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True


class SceneAnimationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_animation_prompt_returns_fortress_generated_prompt(self):
        with mock.patch.object(
            api,
            "generate_scene_animation_prompt",
            return_value="Clouds sweep apart while the camera advances toward the light.",
        ):
            result = await api.scene_animation_prompt("bible-genesis-1", 3)

        self.assertEqual(result.projectId, "bible-genesis-1")
        self.assertEqual(result.sceneIndex, 3)
        self.assertIn("camera advances", result.prompt)

    async def test_animate_scene_queues_one_scene_without_removing_still(self):
        database = _Database()
        with mock.patch.object(api, "scene_animation_context"):
            result = await api.animate_scene(
                "bible-genesis-1",
                2,
                SceneAnimationRequest(prompt="Water ripples outward."),
                db=database,
            )

        queued_job = next(value for value in database.added if hasattr(value, "payload"))
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(queued_job.payload["workflow"], "scene-animation")
        self.assertEqual(queued_job.payload["sceneIndex"], 2)
        self.assertEqual(queued_job.payload["prompt"], "Water ripples outward.")
        self.assertTrue(database.committed)


if __name__ == "__main__":
    unittest.main()
