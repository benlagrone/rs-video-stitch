"""Simple background worker that consumes render jobs."""
from __future__ import annotations

import datetime as dt
import os
import time
import traceback
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Artifact, Job
from app.renderer import render_project
from app.storage import job_log_path

STORAGE_ROOT = Path(os.getenv("RENDER_STORAGE", "/data"))
POLL_INTERVAL = 1.0


def _timestamp() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def _open_log(job_id: str):
    path = job_log_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = path.open("a", encoding="utf-8")

    def write(message: str) -> None:
        log_file.write(f"[{_timestamp()}] {message}\n")
        log_file.flush()

    return log_file, write


def _update_job(session, job: Job, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    session.add(job)
    session.commit()


def loop() -> None:
    while True:
        with SessionLocal() as session:
            job = (
                session.execute(
                    select(Job).where(Job.status == "QUEUED").order_by(Job.created_at)
                )
                .scalars()
                .first()
            )
            if job is None:
                time.sleep(POLL_INTERVAL)
                continue

            job_id = job.id
            _update_job(session, job, status="RUNNING", stage="VALIDATE", progress=0.02)
            payload = job.payload or {}
            options = payload.get("renderOptions", {})
            output_name = payload.get("outputName", "video.mp4")

            log_file, log = _open_log(job_id)
            log(f"Starting job for project {job.project_id}")

            def progress(stage: str, value: float) -> None:
                _update_job(session, job, stage=stage, progress=min(1.0, value))

            try:
                final_path = render_project(
                    job.project_id,
                    STORAGE_ROOT,
                    options,
                    output_name,
                    log=log,
                    progress=progress,
                )
                size = final_path.stat().st_size if final_path.exists() else 0
                if final_path.exists():
                    try:
                        rel_path = final_path.relative_to(STORAGE_ROOT)
                    except ValueError:
                        rel_path = final_path
                else:
                    rel_path = final_path
                artifact = Artifact(
                    project_id=job.project_id,
                    job_id=job.id,
                    path=str(rel_path),
                    kind="video",
                    size=size,
                )
                session.add(artifact)
                _update_job(session, job, status="SUCCEEDED", stage="FINALIZE", progress=1.0)
                log(f"Job completed: {final_path}")
            except Exception as exc:  # noqa: BLE001
                log("Job failed")
                log(traceback.format_exc())
                _update_job(
                    session,
                    job,
                    status="FAILED",
                    stage="ERROR",
                    progress=1.0,
                    error=str(exc)[:2000],
                )
            finally:
                log_file.close()
        time.sleep(0.3)


if __name__ == "__main__":
    loop()
