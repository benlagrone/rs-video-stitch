import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage
from app.schemas import (
    AssetCandidate,
    ExternalAssetSelection,
    ProviderRegistryItem,
    SourceCard,
)


class SourceIntegrationContractTest(TestCase):
    def test_asset_candidate_preserves_provider_rights_and_query_context(self):
        candidate = AssetCandidate(
            provider="aic",
            provider_id="27992",
            title="A Sunday on La Grande Jatte",
            creator="Georges Seurat",
            date="1884-1886",
            image_url="https://example.test/full.jpg",
            source_url="https://example.test/source",
            license="Public Domain",
            rights_status="public_domain",
            attribution="Art Institute of Chicago",
            query={"q": "landscape", "provider": "aic"},
        )

        payload = candidate.model_dump(mode="json")

        self.assertEqual(payload["provider"], "aic")
        self.assertEqual(payload["provider_id"], "27992")
        self.assertEqual(payload["rights_status"], "public_domain")
        self.assertEqual(payload["query"]["q"], "landscape")
        self.assertEqual(payload["warnings"], [])

    def test_selection_and_source_card_have_roadmap_defaults(self):
        candidate = AssetCandidate(provider="fixture", provider_id="asset-1")
        selection = ExternalAssetSelection(filename="fixture_asset_1.jpg", candidate=candidate)
        source_card = SourceCard(provider="open_library", provider_id="OL123W")
        provider = ProviderRegistryItem(
            id="fixture",
            label="Fixture Provider",
            kind="image",
            supports_import=True,
        )

        self.assertEqual(selection.review_status, "approved")
        self.assertEqual(source_card.rights_status, "metadata_only")
        self.assertFalse(provider.requires_key)
        self.assertTrue(provider.supports_import)


class SourceIntegrationStorageTest(TestCase):
    def test_asset_provenance_and_source_cards_persist_under_project_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_root = root / "projects"
            with mock.patch.object(storage, "ROOT", root), mock.patch.object(
                storage,
                "PROJECTS_ROOT",
                projects_root,
            ):
                selection = {
                    "candidate": {
                        "provider": "aic",
                        "provider_id": "27992",
                        "source_url": "https://example.test/source",
                        "license": "Public Domain",
                        "attribution": "Art Institute of Chicago",
                        "warnings": [],
                    },
                    "review_status": "approved",
                }
                stored_selection = storage.upsert_asset_provenance(
                    "p_sources",
                    "../aic_27992.jpg",
                    selection,
                    project_name="Source project",
                )
                cards = [
                    {
                        "provider": "open_library",
                        "provider_id": "OL123W",
                        "title": "Example Work",
                        "rights_status": "metadata_only",
                    }
                ]
                storage.save_source_cards("p_sources", cards, project_name="Source project")

                provenance = storage.read_asset_provenance("p_sources")
                loaded_cards = storage.read_source_cards("p_sources")
                project_input = storage.p_input("p_sources")
                provenance_exists = (project_input / "asset_provenance.json").exists()
                source_cards_exists = (project_input / "source_cards.json").exists()

        self.assertEqual(stored_selection["filename"], "aic_27992.jpg")
        self.assertIn("aic_27992.jpg", provenance)
        self.assertEqual(provenance["aic_27992.jpg"]["candidate"]["provider"], "aic")
        self.assertEqual(loaded_cards[0]["provider"], "open_library")
        self.assertTrue(provenance_exists)
        self.assertTrue(source_cards_exists)
