import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.grok_video_client import GrokVideoError, download_video, submit_video, wait_for_video
from app.grok_video_request import GrokVideoRequestError, build_grok_video_request, motion_keyframe_times
from app.grok_video_workflow import (
    GrokVideoPlanRequest,
    ImageRef,
    build_project_request,
    run_grok_video_job,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class GrokVideoRequestTest(TestCase):
    def test_coffee_sip_prompt_uses_the_3d_shot_timing(self):
        built = build_project_request(
            "unused",
            GrokVideoPlanRequest(shotId="coffee-sip", useBlockout=False),
        )
        prompt = built["plan"]["prompt"]

        self.assertEqual(built["body"]["model"], "grok-imagine-video-1.5")
        self.assertEqual(built["body"]["duration"], 4)
        self.assertIn("right hand grips the cup handle", prompt)
        self.assertIn("cup rim visibly reaches the mouth", prompt)
        self.assertNotIn("reference_images", built["body"])
        self.assertEqual(
            [round(item, 3) for item in built["plan"]["keyframeTimes"]],
            [0.667, 1.667, 2.667, 3.333],
        )

    def test_plate_is_the_pinned_opening_and_references_start_at_image_1(self):
        built = build_grok_video_request(
            {
                "id": "office-pass",
                "title": "Office pass",
                "duration_seconds": 4,
                "framing": "locked wide shot of the office floor",
                "action_beats": [
                    {"seconds": "0.0-2.0", "action": "a seated person looks at a monitor"},
                    {"seconds": "2.0-4.0", "action": "the person returns to the keyboard"},
                ],
            },
            plate=(PNG, "image/png", "office plate (floor.png)"),
            appearance=[(PNG, "image/png", "person (guest.png)")],
            resolution="1080p",
        )

        self.assertTrue(built["body"]["image"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(built["body"]["reference_images"]), 1)
        self.assertIn("<IMAGE_1> is the appearance reference person (guest.png)", built["plan"]["prompt"])
        self.assertEqual(built["body"]["resolution"], "720p")
        self.assertIn("opening frame is pinned", built["plan"]["prompt"])
        self.assertIn("background photograph", built["plan"]["prompt"])

    def test_phronesis_requires_a_background_still(self):
        with self.assertRaises(GrokVideoRequestError):
            build_project_request(
                "unused",
                GrokVideoPlanRequest(shotId="coffee-sip", destination="phronesis", useBlockout=False),
            )

    def test_phronesis_animates_the_background_still_without_calling_grok(self):
        request = GrokVideoPlanRequest(
            shotId="coffee-sip",
            destination="phronesis",
            useBlockout=False,
            background=ImageRef(subdir="images", filename="office.png", role="room"),
        )
        with mock.patch(
            "app.grok_video_workflow._load_image",
            return_value=(PNG, "image/png", "room (office.png)"),
        ):
            built = build_project_request("project", request)
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "office.png"
            image.write_bytes(PNG)
            payload = {
                "request": request.model_dump(mode="json"),
                "approvedPrompt": built["plan"]["prompt"],
            }
            with mock.patch("app.grok_video_workflow._load_image", return_value=(PNG, "image/png", "room (office.png)")), \
                    mock.patch("app.grok_video_workflow.project_asset_path", return_value=image), \
                    mock.patch("app.grok_video_workflow.p_output", return_value=Path(temp_dir)), \
                    mock.patch("app.motion_provider.generate_motion_clip", return_value={"status": "accepted"}) as motion, \
                    mock.patch("app.grok_video_workflow.submit_video") as submit:
                run_grok_video_job("project", payload, progress=lambda *_: None, log=lambda *_: None)

        submit.assert_not_called()
        self.assertEqual(motion.call_args.args[0], image)
        self.assertIn("background photograph", motion.call_args.kwargs["prompt"])
        self.assertEqual(built["plan"]["layers"]["motion"], "sextant-blender:coffee-sip")

    def test_nearby_beats_share_one_keyframe_slot(self):
        shot = {
            "id": "tight",
            "title": "Tight",
            "duration_seconds": 4,
            "framing": "locked",
            "action_beats": [
                {"seconds": "0.0-0.7", "action": "settle"},
                {"seconds": "0.7-0.8", "action": "shift"},
                {"seconds": "0.8-4.0", "action": "hold"},
            ],
        }

        self.assertEqual(motion_keyframe_times(shot), [2 / 3])

    def test_shot_without_a_blockout_still_builds_a_timed_request(self):
        built = build_project_request(
            "unused",
            GrokVideoPlanRequest(shotId="realtor-entry-stop", useBlockout=True),
        )

        self.assertIn("three natural steps", built["plan"]["prompt"])
        self.assertTrue(any("no rendered 3D blockout" in warning for warning in built["plan"]["warnings"]))

    def test_client_submits_polls_and_downloads_without_a_live_call(self):
        session = mock.Mock()
        session.post.return_value = _Response({"request_id": "req-1"})
        session.get.side_effect = [
            _Response({"status": "pending"}),
            _Response({"status": "done", "video": {"url": "https://vid.example/clip.mp4", "duration": 4}}),
            _Response(content=b"video-bytes"),
        ]
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            request_id = submit_video({"model": "grok-imagine-video-1.5", "prompt": "hold"}, session)
            result = wait_for_video(request_id, session, poll_seconds=0)
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "clip.mp4"
                download_video(result["video"]["url"], destination, session)
                self.assertEqual(destination.read_bytes(), b"video-bytes")

        self.assertEqual(request_id, "req-1")
        self.assertNotIn("test-key", str(session.post.call_args.args))
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_reviewed_job_stops_when_the_plan_changes(self):
        payload = {
            "request": {"shotId": "coffee-sip", "useBlockout": False},
            "approvedPrompt": "an older prompt",
        }

        with mock.patch("app.grok_video_workflow.submit_video") as submit:
            with self.assertRaises(GrokVideoError):
                run_grok_video_job("unused", payload, progress=lambda *_: None, log=lambda *_: None)

        submit.assert_not_called()

    def test_missing_api_key_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            with self.assertRaises(GrokVideoError):
                submit_video({"prompt": "hold"}, mock.Mock())
