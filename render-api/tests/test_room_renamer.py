import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import requests

from app.room_renamer import (
    RoomRenamerClient,
    RoomRenamerError,
    is_generic_header,
    localized_room_title,
)
from app import api


class _Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class RoomRenamerTests(unittest.TestCase):
    def test_project_image_inventory_excludes_fonts_and_other_non_images(self):
        state = {
            "images": [
                {"name": "front.jpg"},
                {"name": "ArialUnicode.ttf"},
                {"name": "../outside.png"},
            ]
        }
        with patch.object(
            api,
            "list_asset_files",
            return_value=["front.jpg", "kitchen.jpeg", "ArialUnicode.ttf", "notes.json"],
        ):
            names = api._state_image_names(state, "harmony-zh")

        self.assertEqual(names, ["front.jpg", "kitchen.jpeg"])

    def test_detects_numbered_fallback_headers(self):
        self.assertTrue(is_generic_header("Property Photo 1"))
        self.assertTrue(is_generic_header("房产照片 7"))
        self.assertFalse(is_generic_header("Living Room"))

    def test_localizes_canonical_room_names(self):
        self.assertEqual(localized_room_title("street_view", "en-US"), "Front Exterior")
        self.assertEqual(localized_room_title("street_view", "zh-CN"), "住宅正面")
        self.assertEqual(localized_room_title("living_room", "zh-CN"), "客厅")

    def test_client_calls_protected_batch_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "front.jpg"
            image.write_bytes(b"image")
            response = _Response(
                {"model": "room-resnet50", "mode": "fine-tuned", "results": [{"filename": "front.jpg", "label": "street_view", "confidence": 0.9}]}
            )
            with patch("app.room_renamer.requests.post", return_value=response) as post:
                result = RoomRenamerClient("http://phronesis:8014", token="secret").classify([("front.jpg", image)])

        self.assertEqual(result["results"][0]["label"], "street_view")
        self.assertEqual(post.call_args.args[0], "http://phronesis:8014/v1/classify")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    def test_client_fails_closed_when_service_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "front.jpg"
            image.write_bytes(b"image")
            with patch("app.room_renamer.requests.post", side_effect=OSError("offline")):
                with self.assertRaises(RoomRenamerError):
                    RoomRenamerClient("http://phronesis:8014").classify([("front.jpg", image)])

    def test_client_preserves_provider_error_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "ArialUnicode.ttf"
            image.write_bytes(b"font")
            response = requests.Response()
            response.status_code = 400
            response._content = json.dumps(
                {"detail": "ArialUnicode.ttf: Unsupported image format"}
            ).encode("utf-8")
            with patch("app.room_renamer.requests.post", return_value=response):
                with self.assertRaisesRegex(RoomRenamerError, "ArialUnicode.ttf"):
                    RoomRenamerClient("http://phronesis:8014").classify(
                        [("ArialUnicode.ttf", image)]
                    )

    def test_persistence_replaces_generic_mandarin_header_and_scene_title(self):
        state = {
            "title": "Harmony Cove 中文版",
            "language": "zh-CN",
            "images": [{"name": "front.jpg"}],
            "imageHeaders": {"front.jpg": "Property Photo 1"},
            "imageRoomInfo": {},
        }
        scenes = {
            "scenes": [
                {
                    "title": "Property Photo 1",
                    "images": ["front.jpg"],
                    "timeline": [{"image": "front.jpg", "header": "Property Photo 1"}],
                }
            ]
        }
        with patch.object(api, "save_project_state"), patch.object(api, "_load_scenes", return_value=scenes), patch.object(
            api, "save_scenes"
        ) as save_scenes, patch.object(api, "_write_room_annotations"), patch.object(
            api, "_state_image_names", return_value=["front.jpg"]
        ):
            results = api._persist_room_results(
                "harmony-zh",
                state,
                [{"filename": "front.jpg", "label": "street_view", "confidence": 0.91, "source": "room-renamer:test"}],
                model="test",
            )

        self.assertEqual(results[0]["displayLabel"], "住宅正面")
        self.assertEqual(state["imageHeaders"]["front.jpg"], "住宅正面")
        saved_payload = save_scenes.call_args.args[1]
        self.assertIn('"title": "住宅正面"', saved_payload)
        self.assertNotIn("Property Photo 1", saved_payload)

    def test_required_classification_rejects_partial_provider_response(self):
        state = {
            "title": "Harmony Cove 中文版",
            "language": "zh-CN",
            "images": [{"name": "front.jpg"}, {"name": "kitchen.jpg"}],
            "imageHeaders": {
                "front.jpg": "Property Photo 1",
                "kitchen.jpg": "Property Photo 2",
            },
            "imageRoomInfo": {},
        }
        provider = {
            "model": "test",
            "mode": "local",
            "results": [
                {"filename": "front.jpg", "label": "street_view", "confidence": 0.91}
            ],
        }
        with patch.object(api, "read_project_state", return_value=state), patch.object(
            api, "_state_image_names", return_value=["front.jpg", "kitchen.jpg"]
        ), patch.object(api, "project_asset_path") as asset_path, patch.object(
            api, "_room_renamer_client"
        ) as client, patch.object(api, "_persist_room_results") as persist:
            asset_path.return_value.exists.return_value = True
            asset_path.return_value.is_file.return_value = True
            client.return_value.classify.return_value = provider
            persist.return_value = [
                {
                    "filename": "front.jpg",
                    "label": "street_view",
                    "displayLabel": "住宅正面",
                }
            ]
            with self.assertRaisesRegex(RoomRenamerError, "kitchen.jpg"):
                api._classify_saved_project("harmony-zh")


if __name__ == "__main__":
    unittest.main()
