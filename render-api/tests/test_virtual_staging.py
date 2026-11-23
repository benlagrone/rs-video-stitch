import base64
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app.grok_image_client import GrokImageError, complete_json, edit_image
from app.virtual_staging import (
    ANALYSIS_INSTRUCTIONS,
    VirtualStagingError,
    edit_prompt,
    parse_staging_plan,
    plan_listing,
    staged_filename,
)
from app.virtual_staging_workflow import StagingEditRequest, StagingPlanRequest, create_staged_photos, create_staging_plan


LIVING = {
    "filename": "living.jpg",
    "group": "main-living",
    "anchor": "living.jpg",
    "action": "enhance",
    "brief": "Lift the dark interior and recover the blown windows without inventing the view. Keep the fireplace and warm wood white balance.",
}
BATH = {
    "filename": "bath.jpg",
    "group": "bath",
    "anchor": "bath.jpg",
    "action": "polish",
    "brief": "A small exposure lift on the existing vanity. No new fixtures.",
}
DEFECT = {
    "filename": "crack.jpg",
    "group": "leave",
    "anchor": "crack.jpg",
    "action": "leave",
    "brief": "Close view of existing plaster wear. Do not hide it.",
}


def sample_plan():
    return parse_staging_plan(
        {
            "styleName": "natural daylight",
            "style": "warm wood white balance, recovered windows, no invented sky",
            "styleReason": "Wood floors, white trim, and an overcast bungalow block.",
            "photos": [LIVING, BATH, DEFECT],
        },
        ["living.jpg", "bath.jpg", "crack.jpg"],
        property_address="2438 White Oak Dr",
        area="Houston Heights",
    )


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class VirtualStagingPlanTest(TestCase):
    def test_instructions_read_the_house_and_choose_photo_treatments(self):
        self.assertIn("house and the area", ANALYSIS_INSTRUCTIONS)
        self.assertIn("photo enhancement", ANALYSIS_INSTRUCTIONS)
        self.assertIn("Do not plan furniture", ANALYSIS_INSTRUCTIONS)
        self.assertIn("Do not hide defects", ANALYSIS_INSTRUCTIONS)

    def test_plan_keeps_one_style_and_different_photo_decisions(self):
        calls = []

        def complete(messages):
            calls.append(messages)
            batch = [part["text"].removeprefix("Photo ").removesuffix(":") for part in messages[1]["content"] if part["type"] == "text" and part["text"].startswith("Photo ")]
            photos = []
            for name in batch:
                if name == "living-far.jpg":
                    photos.append({**LIVING, "filename": name, "anchor": "living.jpg"})
                elif name == "bath.jpg":
                    photos.append(BATH)
                else:
                    photos.append(LIVING)
            return {
                "styleName": "natural daylight" if len(calls) == 1 else "glass modern",
                "style": "warm wood white balance, recovered windows",
                "styleReason": "Wood floors and a bungalow street.",
                "photos": photos,
            }

        photos = [{"filename": name, "dataUri": "data:image/jpeg;base64,aa"} for name in ("living.jpg", "living-far.jpg", "bath.jpg")]
        plan = plan_listing(photos, property_address="2438 White Oak Dr", area="Heights", complete=complete, batch_size=2)

        self.assertEqual(plan["styleName"], "natural daylight")
        self.assertIn("already decided", calls[1][1]["content"][0]["text"])
        self.assertEqual([item["action"] for item in plan["photos"]], ["enhance", "enhance", "polish"])
        self.assertEqual(plan["photos"][1]["anchor"], "living.jpg")

    def test_missing_photo_is_left_alone(self):
        plan = parse_staging_plan(
            {
                "styleName": "natural daylight",
                "style": "warm wood white balance",
                "styleReason": "Traditional trim on a tree-lined block.",
                "photos": [LIVING],
            },
            ["living.jpg", "street.jpg"],
            property_address="",
            area="",
        )
        self.assertEqual(plan["photos"][1]["action"], "leave")

    def test_stage_without_a_brief_is_rejected(self):
        with self.assertRaises(VirtualStagingError):
            parse_staging_plan(
                {
                    "styleName": "natural daylight",
                    "style": "warm wood white balance",
                    "styleReason": "Wood floors.",
                    "photos": [{**LIVING, "brief": ""}],
                },
                ["living.jpg"],
                property_address="",
                area="",
            )

    def test_edit_prompt_preserves_the_house_and_labels_the_copy(self):
        plan = sample_plan()
        prompt = edit_prompt(plan, plan["photos"][0])
        self.assertIn("Do not add furniture", prompt)
        self.assertIn("Do not remodel", prompt)
        self.assertIn("recover the blown windows", prompt)
        self.assertIn("Houston Heights", prompt)
        self.assertNotIn("Virtually staged", prompt)
        with self.assertRaises(VirtualStagingError):
            edit_prompt(plan, plan["photos"][2])

    def test_staged_name_never_replaces_the_original(self):
        self.assertEqual(staged_filename("GK-11.JPG"), "GK-11-enhanced.jpg")
        self.assertNotEqual(staged_filename("GK-11.JPG"), "GK-11.JPG")


class GrokImageClientTest(TestCase):
    def test_edit_reads_base64_image_bytes(self):
        encoded = base64.b64encode(b"staged-photo").decode("ascii")
        session = mock.Mock()
        session.post.return_value = _Response({"data": [{"b64_json": encoded}]})
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            image = edit_image("stage the room", "data:image/jpeg;base64,aa", session)
        self.assertEqual(image, b"staged-photo")
        self.assertIn("/images/edits", session.post.call_args.args[0])

    def test_chat_plan_is_parsed(self):
        session = mock.Mock()
        session.post.return_value = _Response({"choices": [{"message": {"content": "{\"styleName\": \"warm transitional\"}"}}]})
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            payload = complete_json([{"role": "user", "content": "read the house"}], session)
        self.assertEqual(payload["styleName"], "warm transitional")

    def test_provider_error_is_reported(self):
        session = mock.Mock()
        session.post.return_value = _Response({"error": "bad prompt"}, status_code=400)
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            with self.assertRaises(GrokImageError):
                edit_image("stage", "data:image/jpeg;base64,aa", session)


class VirtualStagingRouteTest(TestCase):
    def test_unconfirmed_edit_does_not_call_grok(self):
        with self.assertRaises(HTTPException) as raised:
            create_staged_photos("listing", StagingEditRequest(confirmed=False, plan=sample_plan()))
        self.assertEqual(raised.exception.status_code, 409)

    def test_confirmed_edit_writes_a_sibling_and_keeps_the_original(self):
        plan = sample_plan()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "living.jpg"
            original = b"\xff\xd8\xfforiginal"
            source.write_bytes(original)
            with mock.patch("app.virtual_staging_workflow.project_asset_path", return_value=source), \
                    mock.patch("app.virtual_staging_workflow.edit_image", return_value=b"\xff\xd8\xffstaged") as edit, \
                    mock.patch("app.virtual_staging_workflow._remember_staged_image"):
                result = create_staged_photos("listing", StagingEditRequest(confirmed=True, plan=plan, filenames=["living.jpg"]))
            self.assertEqual(source.read_bytes(), original)
            sibling = Path(temp_dir) / "living-enhanced.jpg"
            self.assertEqual(sibling.read_bytes(), b"\xff\xd8\xffstaged")
            self.assertEqual(result["results"][0]["filename"], sibling.name)
            self.assertIn("recover the blown windows", edit.call_args.args[0])

    def test_plan_route_stores_the_decision(self):
        plan = sample_plan()
        with mock.patch("app.virtual_staging_workflow._source_filenames", return_value=["living.jpg"]), \
                mock.patch("app.virtual_staging_workflow.read_project_state", return_value={"propertyAddress": "2438 White Oak Dr"}), \
                mock.patch("app.virtual_staging_workflow._preview_data_uri", return_value="data:image/jpeg;base64,aa"), \
                mock.patch("app.virtual_staging_workflow.plan_listing", return_value=plan) as planned, \
                mock.patch("app.virtual_staging_workflow._store_plan") as store:
            result = create_staging_plan("listing", StagingPlanRequest(area="Houston Heights"))
        self.assertEqual(result["plan"]["styleName"], "natural daylight")
        self.assertEqual(planned.call_args.kwargs["area"], "Houston Heights")
        store.assert_called_once()
