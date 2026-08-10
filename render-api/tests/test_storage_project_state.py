import tempfile
import sys
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage


class ProjectStateStorageTest(TestCase):
    def test_project_state_and_list_projects_use_persistent_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_root = root / "projects"
            with mock.patch.object(storage, "ROOT", root), mock.patch.object(
                storage,
                "PROJECTS_ROOT",
                projects_root,
            ):
                storage.save_project_state(
                    "p_saved",
                    {
                        "title": "Saved listing",
                        "images": [{"name": "front.jpg"}],
                        "removedImages": [],
                        "updatedAt": 1000.0,
                    },
                    project_name="Saved listing",
                )
                storage.save_project_version(
                    "p_saved",
                    {
                        "title": "Saved listing",
                        "images": [{"name": "front.jpg"}],
                        "removedImages": [{"name": "old.jpg"}],
                        "updatedAt": 1000.0,
                    },
                    label="update",
                )
                storage.save_project_version(
                    "p_saved",
                    {
                        "title": "Saved listing",
                        "images": [{"name": "front.jpg"}],
                        "removedImages": [{"name": "old.jpg"}],
                        "updatedAt": 1000.0,
                    },
                    label="restore",
                )
                image_dir = storage.p_input("p_saved") / "images"
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / "front.jpg").write_bytes(b"image")

                loaded = storage.read_project_state("p_saved")
                versions = storage.list_project_versions("p_saved")
                projects = storage.list_projects()

        self.assertEqual(loaded["title"], "Saved listing")
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["projectId"], "p_saved")
        self.assertEqual(projects[0]["title"], "Saved listing")
        self.assertEqual(projects[0]["imageCount"], 1)
        self.assertEqual(projects[0]["versionCount"], 1)
        self.assertEqual(versions[0]["label"], "update")
        self.assertEqual(versions[0]["imageCount"], 1)
        self.assertEqual(versions[0]["removedImageCount"], 1)
