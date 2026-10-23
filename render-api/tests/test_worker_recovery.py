import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job, Project
from app import worker


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'jobs.sqlite3'}", future=True)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _job(self, session, job_id, **fields):
        session.add(Project(id="project-1"))
        job = Job(id=job_id, project_id="project-1", status="RUNNING", payload={}, progress=0.24, stage="AUDIO_PREP", **fields)
        session.add(job)
        session.commit()
        return job

    def test_requeues_only_expired_leases(self):
        now = dt.datetime(2026, 10, 23, 14, 0, 0)
        with self.Session() as session, patch.object(worker, "_open_log") as open_log:
            open_log.return_value = (MagicMock(), lambda _: None)
            self._job(session, "expired", worker_id="old", lease_expires_at=now - dt.timedelta(seconds=1))
            current = Job(id="current", project_id="project-1", status="RUNNING", payload={}, progress=0.5, stage="BUILD", worker_id="active", lease_expires_at=now + dt.timedelta(seconds=60))
            session.add(current)
            session.commit()

            self.assertEqual(worker.recover_orphaned_jobs(session, "new", now), ["expired"])
            self.assertEqual(session.get(Job, "expired").status, "QUEUED")
            self.assertEqual(session.get(Job, "current").status, "RUNNING")

    def test_startup_recovers_legacy_unleased_job(self):
        with self.Session() as session, patch.object(worker, "_open_log") as open_log:
            open_log.return_value = (MagicMock(), lambda _: None)
            self._job(session, "legacy")

            self.assertEqual(worker.recover_unleased_jobs_on_startup(session, "new"), ["legacy"])
            job = session.get(Job, "legacy")
            self.assertEqual(job.status, "QUEUED")
            self.assertEqual(job.stage, "RECOVERING")


if __name__ == "__main__":
    unittest.main()
