import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import api
from app.db import Base
from app.models import Artifact, Job, Project
from app.schemas import TikTokHighlightPlanRequest, TikTokHighlightRenderRequest


class TikTokHighlightApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.master_state = self.input / "project_state.json"
        self.master_state.write_text('{"outputName":"youtube.mp4","youtubeUpload":{"videoId":"keep-me"}}')
        self.original_state = self.master_state.read_bytes()
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        self.db.add(Project(id="property", last_output_name="youtube.mp4"))
        self.db.commit()
        for name, value in (("p_input", self.input), ("p_output", self.output)):
            patcher = patch.object(api, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(api, "ensure_dirs")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plan = {"sourceFilename": "youtube.mp4", "durationSeconds": 45, "hook": "Property tour"}

    def request(self):
        return TikTokHighlightRenderRequest(plan=self.plan)

    def test_duration_request_is_limited_to_thirty_through_sixty_seconds(self):
        for duration in (29, 61):
            with self.assertRaises(ValidationError):
                TikTokHighlightPlanRequest(filename="youtube.mp4", durationSeconds=duration)

    async def test_plan_edits_survive_separately_from_main_project_state(self):
        with patch.object(api, "plan_highlight", return_value=self.plan):
            result = await api.tiktok_highlight_plan("property", TikTokHighlightPlanRequest(filename="youtube.mp4"), self.db)
        self.assertEqual(result["plan"], self.plan)
        edited = {**self.plan, "hook": "An editable hook"}
        with patch.object(api, "validate_plan", return_value=edited):
            await api.tiktok_highlight_save("property", TikTokHighlightRenderRequest(plan=edited), self.db)
        self.assertEqual((await api.tiktok_highlight_state("property", self.db))["plan"], edited)
        self.assertEqual(self.master_state.read_bytes(), self.original_state)

    async def test_render_queues_frozen_plan_and_preserves_youtube_output(self):
        with patch.object(api, "validate_plan", return_value=self.plan):
            first = await api.tiktok_highlight_render("property", self.request(), self.db)
            repeated = await api.tiktok_highlight_render("property", self.request(), self.db)
        self.assertEqual(first, repeated)
        self.assertEqual(self.db.query(Job).count(), 1)
        job = self.db.get(Job, first["jobId"])
        self.assertEqual(job.payload, {"workflow": "tiktok-highlight", "plan": self.plan})
        self.assertEqual(job.status, "QUEUED")
        self.assertEqual(self.db.get(Project, "property").last_output_name, "youtube.mp4")
        self.assertEqual(self.master_state.read_bytes(), self.original_state)

    async def test_active_render_prevents_replacing_its_draft(self):
        with patch.object(api, "validate_plan", return_value=self.plan):
            await api.tiktok_highlight_render("property", self.request(), self.db)
        with self.assertRaises(HTTPException) as error:
            await api.tiktok_highlight_save("property", self.request(), self.db)
        self.assertEqual(error.exception.status_code, 409)

    async def test_finished_artifact_and_caption_sidecar_are_read_back_from_job(self):
        with patch.object(api, "validate_plan", return_value=self.plan):
            result = await api.tiktok_highlight_render("property", self.request(), self.db)
        job = self.db.get(Job, result["jobId"])
        job.status, job.stage, job.progress = "SUCCEEDED", "FINALIZE", 1.0
        self.db.add(Artifact(project_id="property", job_id=job.id, kind="video", path="projects/property/output/tiktok_highlight_45.mp4", size=100))
        self.db.commit()
        (self.output / "tiktok_highlight_45.srt").write_text("captions")
        saved = await api.tiktok_highlight_state("property", self.db)
        self.assertEqual(saved["filename"], "tiktok_highlight_45.mp4")
        self.assertEqual(saved["captionFilename"], "tiktok_highlight_45.srt")
        self.assertEqual(saved["status"], "SUCCEEDED")

    async def test_missing_source_becomes_clear_validation_error_without_queueing(self):
        with patch.object(api, "plan_highlight", side_effect=FileNotFoundError("Source video missing")):
            with self.assertRaises(HTTPException) as error:
                await api.tiktok_highlight_plan("property", TikTokHighlightPlanRequest(), self.db)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.db.query(Job).count(), 0)
