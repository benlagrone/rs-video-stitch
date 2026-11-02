import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from app import api


def _request(range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": "/asset", "headers": headers})


class ProjectAssetStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_footage_asset_honors_byte_ranges_and_streams_inline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            footage = Path(temp_dir) / "listing.mov"
            footage.write_bytes(b"0123456789")

            with patch.object(api, "project_asset_path", return_value=footage):
                response = await api.get_project_asset(
                    "project-1",
                    "footage",
                    footage.name,
                    _request("bytes=2-5"),
                )

            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["accept-ranges"], "bytes")
            self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
            self.assertEqual(response.headers["content-disposition"], 'inline; filename="listing.mov"')
            payload = b"".join([chunk async for chunk in response.body_iterator])
            self.assertEqual(payload, b"2345")
