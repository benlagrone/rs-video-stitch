import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import api


class OutputDeleteApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_only_requested_local_video(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            video = output_dir / "listing.mp4"
            thumbnail = output_dir / "thumbnail.jpg"
            video.write_bytes(b"video")
            thumbnail.write_bytes(b"thumbnail")

            with patch.object(api, "p_output", return_value=output_dir):
                result = await api.delete_output_video("project-1", "listing.mp4")

            self.assertEqual(result["deleted"], "listing.mp4")
            self.assertFalse(video.exists())
            self.assertTrue(thumbnail.exists())

    async def test_rejects_paths_outside_project_output(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            api, "p_output", return_value=Path(temp_dir)
        ):
            with self.assertRaises(HTTPException) as raised:
                await api.delete_output_video("project-1", "../listing.mp4")

        self.assertEqual(raised.exception.status_code, 400)

    async def test_rejects_non_video_files(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            api, "p_output", return_value=Path(temp_dir)
        ):
            with self.assertRaises(HTTPException) as raised:
                await api.delete_output_video("project-1", "thumbnail.jpg")

        self.assertEqual(raised.exception.status_code, 400)

