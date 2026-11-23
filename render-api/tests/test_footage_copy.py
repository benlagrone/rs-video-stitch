import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.footage_copy import (
    clean_transition_title,
    prepare_footage_copy,
    render_options_for_footage,
    spoken_title,
    unsupported_words,
)
from app import storage


class FootageCopyTests(unittest.TestCase):
    def test_clean_transition_title_strips_labels_emoji_and_json(self):
        self.assertEqual(clean_transition_title("Transition: Near Chinatown"), "Near Chinatown")
        self.assertEqual(clean_transition_title('{"title": "Front Desk and Ballroom"}'), "Front Desk and Ballroom")
        self.assertEqual(clean_transition_title("🎥 Transition: Renovated Gym"), "Renovated Gym")

    def test_spoken_title_uses_the_opening_words(self):
        self.assertEqual(
            spoken_title("Hello, do you need office space in Houston, Texas? More words follow."),
            "do you need office space in",
        )

    def test_unsupported_words_ignore_transcript_words(self):
        transcript = "Office space in Houston near Chinatown and the Galleria."
        self.assertEqual(unsupported_words(["Office space in Houston"], transcript), [])
        self.assertIn("abandoned", unsupported_words(["Abandoned Houston Building"], transcript))

    def test_render_options_replace_a_stale_lead_card(self):
        options = render_options_for_footage(
            {
                "introTitle": "Old card",
                "hyperframes": {"leadCardMode": "hyperframes", "transitionTitles": {}},
            },
            {
                "footageCopy": {"status": "ready"},
                "introLines": ["9800 Richmond", "Near the Galleria", ""],
                "hyperframes": {"transitionTitles": {"0": "Front Desk"}},
            },
        )
        self.assertEqual(options["introTitle"], "9800 Richmond\nNear the Galleria")
        self.assertEqual(options["hyperframes"]["transitionTitles"], {"0": "Front Desk"})
        self.assertEqual(options["hyperframes"]["leadCardMode"], "hyperframes")

    def test_prepare_writes_transcript_text_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            root = projects / "p-stitch"
            footage = root / "input" / "footage"
            footage.mkdir(parents=True)
            (footage / "one.mp4").write_bytes(b"one")
            (footage / "two.mp4").write_bytes(b"two")
            state = {
                "title": "9800 Richmond",
                "language": "en-US",
                "sourceFootage": [
                    {"name": "one.mp4", "title": "", "showTitle": False},
                    {"name": "two.mp4", "title": "", "showTitle": False},
                ],
                "hyperframes": {"leadCardMode": "hyperframes"},
                "introLines": ["", "", ""],
            }
            (root / "input" / "project_state.json").write_text(json.dumps(state), encoding="utf-8")
            (root / "input" / "scenes.json").write_text(json.dumps({
                "info": {"name": "9800 Richmond"},
                "sourceFootage": [
                    {"filename": "one.mp4", "title": "", "showTitle": False},
                    {"filename": "two.mp4", "title": "", "showTitle": False},
                ],
                "scenes": [],
            }), encoding="utf-8")

            def transcribe(path, _work, _language):
                return "Houston office near Chinatown." if path.name == "one.mp4" else "Renovated building with a gym."

            with patch.object(storage, "PROJECTS_ROOT", projects):
                first = prepare_footage_copy(
                    "p-stitch",
                    transcribe=transcribe,
                    lead_lines=lambda *_args: ["9800 Richmond", "Near Chinatown", "Renovated building with a gym"],
                    transition_title=lambda outgoing, incoming, _narration, _language: incoming.split()[0],
                    youtube_description=lambda _title, transcript: transcript,
                    tiktok_caption=lambda *_args: "Tour the renovated building #9800Richmond #Houston #Office",
                )
                saved = json.loads((root / "input" / "project_state.json").read_text(encoding="utf-8"))
                second = prepare_footage_copy(
                    "p-stitch",
                    transcribe=lambda *_args: (_ for _ in ()).throw(AssertionError("transcribe again")),
                    lead_lines=lambda *_args: (_ for _ in ()).throw(AssertionError("lead again")),
                    transition_title=lambda *_args: (_ for _ in ()).throw(AssertionError("title again")),
                    youtube_description=lambda *_args: (_ for _ in ()).throw(AssertionError("youtube again")),
                    tiktok_caption=lambda *_args: (_ for _ in ()).throw(AssertionError("tiktok again")),
                )
                scenes = json.loads((root / "input" / "scenes.json").read_text(encoding="utf-8"))

        self.assertTrue(first["generated"])
        self.assertFalse(second["generated"])
        self.assertEqual(saved["introLines"][:3], ["9800 Richmond", "Near Chinatown", "Renovated building with a gym"])
        self.assertIn("Houston office near Chinatown.", saved["script"])
        self.assertEqual(saved["hyperframes"]["transitionTitles"]["0"], "Renovated")
        self.assertEqual(saved["footageCopy"]["status"], "ready")
        self.assertEqual([item["title"] for item in scenes["sourceFootage"]], ["Houston", "Renovated"])

    def test_existing_transcript_copy_is_kept_until_clips_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            projects = Path(temp_dir) / "projects"
            root = projects / "p-kept"
            footage = root / "input" / "footage"
            footage.mkdir(parents=True)
            (footage / "one.mp4").write_bytes(b"one")
            state = {
                "title": "9800 Richmond",
                "language": "en-US",
                "script": "Office space in Houston near Chinatown.",
                "introLines": ["9800 Richmond", "Near Chinatown", ""],
                "sourceFootage": [{"name": "one.mp4", "title": "Near Chinatown", "showTitle": False}],
                "hyperframes": {"transitionTitles": {}},
            }
            (root / "input" / "project_state.json").write_text(json.dumps(state), encoding="utf-8")
            with patch.object(storage, "PROJECTS_ROOT", projects):
                result = prepare_footage_copy(
                    "p-kept",
                    transcribe=lambda *_args: (_ for _ in ()).throw(AssertionError("transcribe")),
                    lead_lines=lambda *_args: (_ for _ in ()).throw(AssertionError("lead")),
                    transition_title=lambda *_args: (_ for _ in ()).throw(AssertionError("title")),
                    youtube_description=lambda *_args: (_ for _ in ()).throw(AssertionError("youtube")),
                    tiktok_caption=lambda *_args: (_ for _ in ()).throw(AssertionError("tiktok")),
                )
            saved = json.loads((root / "input" / "project_state.json").read_text(encoding="utf-8"))
        self.assertFalse(result["generated"])
        self.assertEqual(saved["footageCopy"]["status"], "ready")
        self.assertEqual(saved["script"], "Office space in Houston near Chinatown.")


if __name__ == "__main__":
    unittest.main()
