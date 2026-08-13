import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import art_styles, bible_workflow, motion_provider


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class BibleWorkflowTest(TestCase):
    def test_catalog_exposes_every_legacy_and_current_style(self):
        styles = art_styles.list_art_styles()

        self.assertEqual(len(styles), 77)
        self.assertEqual(len({style["id"] for style in styles}), 77)
        self.assertIn("baroque", {style["id"] for style in styles})
        self.assertIn("mortgage-family-haven", {style["id"] for style in styles})
        self.assertIn("fortress-grid-illustration", {style["id"] for style in styles})

    def test_storyboard_resolves_catalog_id_to_full_prompt(self):
        session = mock.Mock()
        session.get.return_value = _Response(
            {
                "reference": "Genesis 1:1",
                "verses": [{"book_name": "Genesis", "chapter": 1, "verse": 1, "text": "In the beginning."}],
            }
        )
        with mock.patch.object(bible_workflow.requests, "get", side_effect=session.get):
            _, scenes = bible_workflow.build_storyboard(
                {"passage": "Genesis 1:1", "translation": "kjv", "visualStyle": "baroque"}
            )

        prompt = scenes[0]["timeline"][0]["prompt"]
        self.assertIn("Art direction: Baroque", prompt)
        self.assertIn("chiaroscuro", prompt)

    def test_fetch_and_build_storyboard_preserves_each_verse(self):
        session = mock.Mock()
        session.get.return_value = _Response(
            {
                "reference": "John 3:16-17",
                "verses": [
                    {"book_name": "John", "chapter": 3, "verse": 16, "text": "For God so loved the world."},
                    {"book_name": "John", "chapter": 3, "verse": 17, "text": "For God sent not his Son to condemn."},
                ],
            }
        )
        with mock.patch.object(bible_workflow.requests, "get", side_effect=session.get):
            reference, scenes = bible_workflow.build_storyboard(
                {"passage": "John 3:16-17", "translation": "kjv", "visualStyle": "cinematic natural light"}
            )

        self.assertEqual(reference, "John 3:16-17")
        self.assertEqual([scene["title"] for scene in scenes], ["John 3:16", "John 3:17"])
        self.assertEqual(scenes[0]["VO"], "For God so loved the world.")
        self.assertIn("no modern objects", scenes[0]["timeline"][0]["prompt"])

    def test_motion_storyboard_has_visible_actions_and_locked_scene_handoffs(self):
        session = mock.Mock()
        session.get.return_value = _Response(
            {
                "reference": "Genesis 1:1-2",
                "verses": [
                    {"book_name": "Genesis", "chapter": 1, "verse": 1, "text": "In the beginning."},
                    {"book_name": "Genesis", "chapter": 1, "verse": 2, "text": "Darkness was upon the deep."},
                ],
            }
        )
        raw_plan = json.dumps(
            {
                "scenes": [
                    {"startState": "A dark empty sea", "action": "Light spreads across the water", "endState": "The water glows beneath a new light", "camera": "Track slowly forward", "continuity": "The same sea and horizon", "transition": "The glow reveals the waves"},
                    {"startState": "An unrelated response", "action": "Wind drives ripples across the water", "endState": "Ordered waves fill the frame", "camera": "Glide above the surface", "continuity": "none", "transition": "The waves carry forward"},
                ]
            }
        )
        with mock.patch.object(bible_workflow.requests, "get", side_effect=session.get), mock.patch.object(
            bible_workflow,
            "plan_motion_sequence",
            return_value=bible_workflow._parse_motion_plan(raw_plan, 2),
        ):
            _, scenes = bible_workflow.build_storyboard(
                {"passage": "Genesis 1:1-2", "translation": "kjv", "visualStyle": "baroque", "mode": "motion"}
            )

        self.assertEqual(scenes[1]["startState"], scenes[0]["endState"])
        self.assertEqual(scenes[0]["action"], "Light spreads across the water")
        self.assertNotEqual(scenes[1]["continuity"], "none")
        self.assertIn("carry forward", scenes[1]["continuity"])
        self.assertIn("The visible action is", scenes[0]["motionPrompt"])
        self.assertIn("Connection to the next shot", scenes[0]["motionPrompt"])

    def test_motion_planner_uses_fortress_ollama_for_whole_passage(self):
        session = mock.Mock()
        response_plan = {
            "scenes": [
                {"startState": "A dark sea", "action": "Light crosses the water", "endState": "A glowing sea", "camera": "Push forward", "continuity": "Same horizon", "transition": "Follow the glow"}
            ]
        }
        session.post.return_value = _Response({"response": json.dumps(response_plan)})

        plan = bible_workflow.plan_motion_sequence(
            "Genesis 1:1",
            [{"reference": "Genesis 1:1", "text": "In the beginning."}],
            "baroque",
            session=session,
        )

        self.assertEqual(plan[0]["action"], "Light crosses the water")
        call = session.post.call_args
        self.assertTrue(call.args[0].endswith("/api/generate"))
        self.assertEqual(call.kwargs["json"]["model"], bible_workflow.OLLAMA_MODEL)
        self.assertIn("exactly one shot per supplied verse", call.kwargs["json"]["prompt"])
        self.assertEqual(call.kwargs["json"]["format"], "json")

    def test_generate_still_writes_first_image(self):
        session = mock.Mock()
        session.post.return_value = _Response({"images": [base64.b64encode(b"png-data").decode("ascii")]})
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "scene.png"
            bible_workflow._generate_still("a scene", destination, session=session)
            self.assertEqual(destination.read_bytes(), b"png-data")
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual((payload["width"], payload["height"]), (1024, 576))
        self.assertEqual(payload["override_settings"]["sd_model_checkpoint"], bible_workflow.STABLE_DIFFUSION_CHECKPOINT)
        self.assertTrue(payload["override_settings_restore_afterwards"])

    def test_generate_still_appends_title_card_negative_constraints(self):
        session = mock.Mock()
        session.post.return_value = _Response({"images": [base64.b64encode(b"png-data").decode("ascii")]})
        with tempfile.TemporaryDirectory() as tmp:
            bible_workflow._generate_still(
                "Genesis title",
                Path(tmp) / "title.png",
                negative_extra="church, tower, busy center",
                session=session,
            )

        negative_prompt = session.post.call_args.kwargs["json"]["negative_prompt"]
        self.assertIn("church", negative_prompt)
        self.assertIn("busy center", negative_prompt)

    def test_motion_project_chains_each_clip_final_frame_into_next_scene(self):
        scenes = [
            {"title": "Genesis 1:1", "VO": "One", "images": ["scene_001.png"], "motionPrompt": "First action", "timeline": [{"image": "scene_001.png", "prompt": "First frame"}]},
            {"title": "Genesis 1:2", "VO": "Two", "images": ["scene_002.png"], "motionPrompt": "Second action", "timeline": [{"image": "scene_002.png", "prompt": "Unused independent frame"}]},
        ]
        payload = {"mode": "motion", "translation": "kjv", "visualStyle": "baroque", "renderOptions": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bible_workflow, "build_storyboard", return_value=("Genesis 1:1-2", scenes)
        ), mock.patch.object(bible_workflow, "ensure_dirs"), mock.patch.object(
            bible_workflow, "p_input", return_value=Path(tmp) / "input"
        ), mock.patch.object(bible_workflow, "save_scenes"), mock.patch.object(
            bible_workflow, "save_project_state"
        ), mock.patch.object(bible_workflow, "_generate_still") as generate_still, mock.patch.object(
            bible_workflow, "generate_motion_clip"
        ) as generate_motion, mock.patch.object(bible_workflow, "extract_last_frame") as extract:
            bible_workflow.prepare_bible_project("bible-test", payload, progress=mock.Mock(), log=mock.Mock())

        first_clip = Path(tmp) / "input" / "motion" / "scene_001.mp4"
        second_image = Path(tmp) / "input" / "images" / "scene_002.png"
        self.assertEqual(generate_still.call_count, 2)
        title_card_call = generate_still.call_args_list[0]
        self.assertIn("Genesis 1:1-2", title_card_call.args[0])
        self.assertIn("Baroque", title_card_call.args[0])
        self.assertIn("No words", title_card_call.args[0])
        self.assertEqual(title_card_call.args[1].name, "bible-title-card.png")
        self.assertFalse(payload["renderOptions"]["introLeaderEnabled"])
        self.assertFalse(payload["renderOptions"]["logoEnabled"])
        extract.assert_called_once_with(first_clip, second_image)
        self.assertEqual(generate_motion.call_count, 2)
        self.assertEqual(generate_motion.call_args_list[1].args[0], second_image)

    def test_regenerated_title_card_uses_saved_passage_and_selected_style(self):
        document = {
            "info": {"name": "Genesis 1 (KJV)", "passage": "Genesis 1"},
            "scenes": [{"title": "Genesis 1:1", "VO": "In the beginning God created the heaven and the earth."}],
        }
        saved_states = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bible_workflow, "p_input", return_value=Path(tmp) / "input"
        ), mock.patch.object(
            bible_workflow, "read_project_state", return_value={"title": "Genesis 1 (KJV)", "passage": "Genesis 1"}
        ), mock.patch.object(
            bible_workflow, "save_project_state", side_effect=lambda _pid, state, **_kwargs: saved_states.append(dict(state))
        ), mock.patch.object(bible_workflow, "_generate_still") as generate_still:
            input_dir = Path(tmp) / "input"
            input_dir.mkdir(parents=True)
            (input_dir / "scenes.json").write_text(json.dumps(document), encoding="utf-8")
            destination = bible_workflow.generate_bible_title_card(
                "bible-test",
                "medieval-illuminated-manuscript",
                progress=mock.Mock(),
                log=mock.Mock(),
            )

        self.assertEqual(destination.name, "bible-title-card.png")
        prompt = generate_still.call_args.args[0]
        self.assertIn("Genesis 1", prompt)
        self.assertIn("Medieval Illuminated Manuscript", prompt)
        self.assertIn("brokerage branding", prompt)
        self.assertIn("Primordial creation", prompt)
        self.assertIn("no people, animals, buildings", prompt)
        self.assertIn("gold-leaf accents", prompt)
        self.assertIn("botanical marginalia", prompt)
        self.assertIn("church", generate_still.call_args.kwargs["negative_extra"])
        self.assertNotIn("decorative title frame", generate_still.call_args.kwargs["negative_extra"])
        self.assertEqual(saved_states[-1]["visualStyle"], "medieval-illuminated-manuscript")
        self.assertFalse(saved_states[-1]["renderOptions"]["introLeaderEnabled"])
        self.assertFalse(saved_states[-1]["renderOptions"]["logoEnabled"])

    def test_scene_animation_prompt_reuses_structured_motion_plan(self):
        document = {
            "info": {"name": "Genesis 1 (KJV)"},
            "scenes": [{
                "title": "Genesis 1:1",
                "VO": "In the beginning.",
                "images": ["scene_001.png"],
                "startState": "Dark water fills the frame",
                "action": "Light travels across the water",
                "endState": "The horizon glows",
                "camera": "Slow forward push",
                "continuity": "Keep the same water and horizon",
                "transition": "The glow carries into the next shot",
                "timeline": [{"image": "scene_001.png", "prompt": "A dark sea beneath the heavens"}],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bible_workflow, "p_input", return_value=Path(tmp) / "input"
        ), mock.patch.object(
            bible_workflow, "read_project_state", return_value={"visualStyle": "baroque"}
        ):
            input_dir = Path(tmp) / "input"
            (input_dir / "images").mkdir(parents=True)
            (input_dir / "images" / "scene_001.png").write_bytes(b"png")
            (input_dir / "scenes.json").write_text(json.dumps(document), encoding="utf-8")
            prompt = bible_workflow.generate_scene_animation_prompt("bible-test", 1)

        self.assertIn("Light travels across the water", prompt)
        self.assertIn("Slow forward push", prompt)
        self.assertIn("The horizon glows", prompt)

    def test_scene_animation_prompt_falls_back_without_inventing_new_content(self):
        document = {
            "info": {"name": "Genesis 1 (KJV)"},
            "scenes": [{
                "title": "Genesis 1:1",
                "VO": "In the beginning God created the heaven and the earth.",
                "images": ["scene_001.png"],
                "timeline": [{"image": "scene_001.png", "prompt": "An existing landscape beneath the heavens"}],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bible_workflow, "p_input", return_value=Path(tmp) / "input"
        ), mock.patch.object(bible_workflow, "read_project_state", return_value={}):
            input_dir = Path(tmp) / "input"
            (input_dir / "images").mkdir(parents=True)
            (input_dir / "images" / "scene_001.png").write_bytes(b"png")
            (input_dir / "scenes.json").write_text(json.dumps(document), encoding="utf-8")
            prompt = bible_workflow.generate_scene_animation_prompt("bible-test", 1)

        self.assertIn("available light advances", prompt)
        self.assertIn("without introducing anything new", prompt)

    def test_animate_scene_preserves_still_and_attaches_motion_clip(self):
        document = {
            "info": {"name": "Genesis 1 (KJV)"},
            "scenes": [{
                "title": "Genesis 1:1",
                "VO": "In the beginning.",
                "images": ["scene_001.png"],
                "timeline": [{"image": "scene_001.png"}],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bible_workflow, "p_input", return_value=Path(tmp) / "input"
        ), mock.patch.object(
            bible_workflow, "read_project_state", return_value={"title": "Genesis 1 (KJV)"}
        ), mock.patch.object(bible_workflow, "save_scenes") as save_scenes, mock.patch.object(
            bible_workflow, "save_project_state"
        ), mock.patch.object(bible_workflow, "generate_motion_clip") as generate_motion:
            input_dir = Path(tmp) / "input"
            (input_dir / "images").mkdir(parents=True)
            still = input_dir / "images" / "scene_001.png"
            still.write_bytes(b"original-still")
            (input_dir / "scenes.json").write_text(json.dumps(document), encoding="utf-8")

            clip = bible_workflow.animate_bible_scene(
                "bible-test",
                1,
                "Light expands across the water.",
                progress=mock.Mock(),
                log=mock.Mock(),
            )
            preserved_still = still.read_bytes()

        self.assertEqual(preserved_still, b"original-still")
        self.assertEqual(clip.name, "scene_001.mp4")
        generate_motion.assert_called_once()
        saved_document = json.loads(save_scenes.call_args.args[1])
        self.assertEqual(saved_document["scenes"][0]["timeline"][0]["video"], "scene_001.mp4")
        self.assertEqual(saved_document["scenes"][0]["motionPrompt"], "Light expands across the water.")

    def test_generate_motion_submits_comfyui_workflow_and_downloads_artifact(self):
        session = mock.Mock()
        session.post.side_effect = [
            _Response({}),
            _Response({"prompt_id": "motion-1"}),
        ]
        session.get.side_effect = [
            _Response({"system": {"os": "posix"}}),
            _Response(
                {
                    "motion-1": {
                        "outputs": {
                            "58": {
                                "videos": [
                                    {"filename": "scene.mp4", "subfolder": "mediastudio", "type": "output"}
                                ]
                            }
                        }
                    }
                }
            ),
            _Response(content=b"mp4-data"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / "scene.png"
            still.write_bytes(b"png-data")
            destination = Path(tmp) / "scene.mp4"
            with mock.patch.object(motion_provider, "_verify_video") as verify:
                motion_provider.generate_motion_clip(
                    still,
                    destination,
                    prompt="a scene",
                    negative_prompt="scene cut",
                    session=session,
                )
            self.assertEqual(destination.read_bytes(), b"mp4-data")
            verify.assert_called_once_with(destination)

        upload_call, prompt_call = session.post.call_args_list
        self.assertTrue(upload_call.args[0].endswith("/upload/image"))
        self.assertTrue(prompt_call.args[0].endswith("/prompt"))
        workflow = prompt_call.kwargs["json"]["prompt"]
        self.assertEqual(workflow["56"]["inputs"]["image"], "scene.png")
        self.assertEqual(workflow["6"]["inputs"]["text"], "a scene")
        self.assertEqual(workflow["7"]["inputs"]["text"], "scene cut")
        self.assertEqual(workflow["55"]["inputs"]["length"], 81)
        self.assertEqual(workflow["57"]["inputs"]["fps"], 16)

    def test_motion_quality_gate_rejects_short_artifact(self):
        probe = {
            "streams": [{"codec_name": "h264", "width": 576, "height": 320, "nb_frames": "10"}],
            "format": {"duration": "1.25"},
        }
        completed = mock.Mock(stdout=__import__("json").dumps(probe))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            motion_provider.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(motion_provider.MotionProviderError, "failed hard gates"):
                motion_provider._verify_video(Path(tmp) / "short.mp4")

    def test_extract_last_frame_creates_next_scene_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scene_001.mp4"
            destination = Path(tmp) / "scene_002.png"

            def complete(command, **_kwargs):
                destination.write_bytes(b"png-data")
                return mock.Mock()

            with mock.patch.object(motion_provider.subprocess, "run", side_effect=complete) as run:
                motion_provider.extract_last_frame(source, destination)

        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-sseof", command)
        self.assertIn(str(source), command)
