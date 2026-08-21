import unittest
from unittest import mock

from app import api
from app.schemas import SceneAnimationBatchRequest, SceneAnimationPromptRequest, SceneAnimationRequest


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
        ) as generate_prompt:
            result = await api.scene_animation_prompt(
                "bible-genesis-1",
                3,
                SceneAnimationPromptRequest(cameraBehavior="pan-right"),
            )

        self.assertEqual(result.projectId, "bible-genesis-1")
        self.assertEqual(result.sceneIndex, 3)
        self.assertIn("camera advances", result.prompt)
        generate_prompt.assert_called_once_with(
            "bible-genesis-1", 3, camera_behavior="pan-right"
        )

    async def test_animate_scene_queues_one_scene_without_removing_still(self):
        database = _Database()
        with mock.patch.object(api, "scene_animation_context"):
            result = await api.animate_scene(
                "bible-genesis-1",
                2,
                SceneAnimationRequest(prompt="Water ripples outward.", cameraBehavior="locked"),
                db=database,
            )

        queued_job = next(value for value in database.added if hasattr(value, "payload"))
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(queued_job.payload["workflow"], "scene-animation")
        self.assertEqual(queued_job.payload["sceneIndex"], 2)
        self.assertEqual(queued_job.payload["prompt"], "Water ripples outward.")
        self.assertEqual(queued_job.payload["cameraBehavior"], "locked")
        self.assertTrue(database.committed)

    async def test_animate_all_scenes_queues_each_still_in_storyboard_order(self):
        database = _Database()
        document = {
            "scenes": [
                {"timeline": [{"image": "scene_001.png"}]},
                {"timeline": [{"image": "scene_002.png", "video": "scene_002.mp4"}]},
                {"timeline": [{"image": "scene_003.png"}]},
            ]
        }
        scenes_path = mock.MagicMock()
        scenes_path.exists.return_value = True
        scenes_path.read_text.return_value = __import__("json").dumps(document)
        with mock.patch.object(api, "p_input") as project_input, mock.patch.object(api, "scene_animation_context"):
            project_input.return_value.__truediv__.return_value = scenes_path
            result = await api.animate_all_scenes(
                "bible-genesis-1",
                SceneAnimationBatchRequest(
                    prompts={1: "Light moves.", 3: "Water moves."},
                    cameraBehaviors={1: "locked", 3: "pan-right"},
                ),
                db=database,
            )

        queued_jobs = [value for value in database.added if hasattr(value, "payload")]
        self.assertEqual(result["queuedCount"], 2)
        self.assertEqual(result["skippedSceneIndexes"], [2])
        self.assertEqual([job.payload["sceneIndex"] for job in queued_jobs], [1, 3])
        self.assertEqual([job.payload["prompt"] for job in queued_jobs], ["Light moves.", "Water moves."])
        self.assertEqual([job.payload["cameraBehavior"] for job in queued_jobs], ["locked", "pan-right"])
        self.assertTrue(database.committed)

    async def test_animate_all_scenes_can_reanimate_existing_motion(self):
        database = _Database()
        document = {"scenes": [{"timeline": [{"image": "scene_001.png", "video": "scene_001.mp4"}]}]}
        scenes_path = mock.MagicMock()
        scenes_path.exists.return_value = True
        scenes_path.read_text.return_value = __import__("json").dumps(document)
        with mock.patch.object(api, "p_input") as project_input, mock.patch.object(api, "scene_animation_context"):
            project_input.return_value.__truediv__.return_value = scenes_path
            result = await api.animate_all_scenes(
                "bible-genesis-1",
                SceneAnimationBatchRequest(includeAnimated=True),
                db=database,
            )

        self.assertEqual(result["queuedCount"], 1)
        self.assertEqual(result["skippedSceneIndexes"], [])

    async def test_regenerate_all_stills_queues_one_ordered_batch(self):
        database = _Database()
        document = {"scenes": [{"title": "Genesis 1:1"}, {"title": "Genesis 1:2"}]}
        scenes_path = mock.MagicMock()
        scenes_path.exists.return_value = True
        scenes_path.read_text.return_value = __import__("json").dumps(document)
        with mock.patch.object(api, "p_input") as project_input:
            project_input.return_value.__truediv__.return_value = scenes_path
            result = await api.regenerate_all_scene_stills("bible-genesis-1", db=database)

        queued_job = next(value for value in database.added if hasattr(value, "payload"))
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(result["sceneIndexes"], [1, 2])
        self.assertEqual(queued_job.payload["workflow"], "bible-scene-stills")
        self.assertEqual(queued_job.payload["sceneIndexes"], [1, 2])
        self.assertTrue(database.committed)


if __name__ == "__main__":
    unittest.main()
