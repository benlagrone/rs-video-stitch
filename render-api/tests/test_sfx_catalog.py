from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.sfx_catalog import SfxCatalogError, list_sfx_catalog


class SfxCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.environment = patch.dict(os.environ, {"SFX_LIBRARY_ROOT": str(self.root)}, clear=False)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp_dir.cleanup()

    def write_catalog(self, assets: list[dict], policy: str = "no-attribution-only") -> None:
        (self.root / "catalog.json").write_text(
            json.dumps({"schema_version": 1, "policy": policy, "assets": assets}),
            encoding="utf-8",
        )

    def test_missing_catalog_returns_safe_empty_inventory(self):
        result = list_sfx_catalog()
        self.assertEqual(result["policy"], "no-attribution-only")
        self.assertEqual(result["owner"], "fortress.sextant:mediastudio-sfx-catalog")
        self.assertEqual(result["assets"], [])

    def test_lists_only_no_attribution_social_assets_without_paths(self):
        assets_dir = self.root / "assets"
        assets_dir.mkdir()
        (assets_dir / "rain.wav").write_bytes(b"RIFF....WAVE")
        self.write_catalog(
            [
                {
                    "id": "sfx-rain",
                    "title": "Gentle rain",
                    "provider": "pixabay",
                    "stored_filename": "rain.wav",
                    "source_url": "https://pixabay.com/sound-effects/rain-123/",
                    "license_name": "Pixabay Content License",
                    "license_url": "https://pixabay.com/service/license-summary/",
                    "attribution_required": False,
                    "commercial_use": True,
                    "social_media_use": True,
                    "bytes": 12,
                    "tags": ["rain", "ambience"],
                },
                {
                    "id": "sfx-credit",
                    "title": "Requires credit",
                    "provider": "mixkit",
                    "stored_filename": "credit.wav",
                    "attribution_required": True,
                    "commercial_use": True,
                    "social_media_use": True,
                },
                {
                    "id": "sfx-unknown",
                    "title": "Unknown license",
                    "provider": "freesound",
                    "stored_filename": "unknown.wav",
                    "attribution_required": False,
                    "commercial_use": True,
                    "social_media_use": True,
                },
            ]
        )

        result = list_sfx_catalog()
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["assets"][0]["available"])
        self.assertNotIn("storedFilename", result["assets"][0])
        self.assertNotIn("path", result["assets"][0])

    def test_rejects_noncompliant_catalog_policy(self):
        self.write_catalog([], policy="attribution-allowed")
        with self.assertRaises(SfxCatalogError):
            list_sfx_catalog()


if __name__ == "__main__":
    unittest.main()
