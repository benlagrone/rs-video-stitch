from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "sfx_mcp.py"
SPEC = importlib.util.spec_from_file_location("sfx_mcp", SERVER_PATH)
assert SPEC and SPEC.loader
sfx_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sfx_mcp)


def write_minimal_wav(path: Path) -> None:
    path.write_bytes(b"RIFF" + (4).to_bytes(4, "little") + b"WAVE")


class SfxMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.library = self.root / "library"
        self.projects = self.root / "projects"
        self.environment = patch.dict(
            os.environ,
            {
                "SFX_IMPORT_ROOTS": str(self.inbox),
                "SFX_LIBRARY_ROOT": str(self.library),
                "SFX_PROJECTS_ROOT": str(self.projects),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp_dir.cleanup()

    def import_asset(self, provider: str = "pixabay"):
        source = self.inbox / "rain.wav"
        write_minimal_wav(source)
        source_url = (
            "https://pixabay.com/sound-effects/rain-1234/"
            if provider == "pixabay"
            else "https://mixkit.co/free-sound-effects/rain/"
        )
        result = sfx_mcp.import_asset_tool(
            {
                "source_path": str(source),
                "provider": provider,
                "source_url": source_url,
                "title": "Gentle rain",
                "tags": ["Rain", "ambience"],
            }
        )
        return result["structuredContent"]["asset"]

    def test_policy_exposes_only_no_attribution_providers(self):
        policy = sfx_mcp.policy_tool({})["structuredContent"]
        self.assertEqual(policy["policy"], "no-attribution-only")
        self.assertEqual(set(policy["providers"]), {"pixabay", "mixkit"})
        self.assertTrue(all(not provider["attribution_required"] for provider in policy["providers"].values()))

    def test_import_records_hash_and_license(self):
        asset = self.import_asset()
        self.assertEqual(asset["provider"], "pixabay")
        self.assertFalse(asset["attribution_required"])
        self.assertTrue(asset["commercial_use"])
        self.assertEqual(len(asset["sha256"]), 64)
        catalog = json.loads((self.library / "catalog.json").read_text())
        self.assertEqual(catalog["policy"], "no-attribution-only")
        self.assertEqual(catalog["assets"][0]["id"], asset["id"])

    def test_import_rejects_unknown_and_attribution_sources(self):
        source = self.inbox / "bird.wav"
        write_minimal_wav(source)
        with self.assertRaises(sfx_mcp.McpError):
            sfx_mcp.import_asset_tool(
                {
                    "source_path": str(source),
                    "provider": "freesound",
                    "source_url": "https://freesound.org/s/1234/",
                    "title": "Bird",
                }
            )

    def test_import_rejects_path_outside_inbox(self):
        source = self.root / "outside.wav"
        write_minimal_wav(source)
        with self.assertRaises(sfx_mcp.McpError):
            sfx_mcp.import_asset_tool(
                {
                    "source_path": str(source),
                    "provider": "pixabay",
                    "source_url": "https://pixabay.com/sound-effects/bird-1234/",
                    "title": "Bird",
                }
            )

    def test_import_is_idempotent_by_hash(self):
        first = self.import_asset()
        second = self.import_asset()
        self.assertEqual(first["id"], second["id"])
        catalog = json.loads((self.library / "catalog.json").read_text())
        self.assertEqual(len(catalog["assets"]), 1)

    def test_stage_asset_copies_audio_and_provenance(self):
        asset = self.import_asset("mixkit")
        result = sfx_mcp.stage_asset_tool(
            {"asset_id": asset["id"], "project_id": "genesis-1", "filename": "rain-bed.wav"}
        )["structuredContent"]
        destination = Path(result["path"])
        self.assertTrue(destination.is_file())
        provenance = json.loads(Path(result["provenance_path"]).read_text())
        self.assertEqual(provenance["policy"], "no-attribution-only")
        self.assertFalse(provenance["assets"]["rain-bed.wav"]["attribution_required"])

    def test_stage_asset_refuses_to_overwrite_different_audio(self):
        asset = self.import_asset()
        destination_dir = self.projects / "genesis-1" / "input" / "sound-effects"
        destination_dir.mkdir(parents=True)
        conflicting = destination_dir / "rain-bed.wav"
        conflicting.write_bytes(b"RIFF" + (8).to_bytes(4, "little") + b"WAVEother")
        with self.assertRaises(sfx_mcp.McpError):
            sfx_mcp.stage_asset_tool(
                {"asset_id": asset["id"], "project_id": "genesis-1", "filename": "rain-bed.wav"}
            )
        self.assertEqual(conflicting.read_bytes()[-5:], b"other")

    def test_tools_mark_mutations_as_writes(self):
        tools = {
            item["name"]: item
            for item in sfx_mcp.handle_request({"id": 1, "method": "tools/list"})["result"]["tools"]
        }
        self.assertFalse(tools["sfx_import_asset"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["sfx_stage_asset"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["sfx_list_assets"]["annotations"]["readOnlyHint"])


if __name__ == "__main__":
    unittest.main()
