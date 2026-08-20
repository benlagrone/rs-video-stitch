"""Simple background worker that consumes render jobs."""
from __future__ import annotations

import datetime as dt
import time
import traceback
from threading import Event
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Artifact, Job
from app.renderer import render_project
from app.bible_workflow import (
    animate_bible_scene,
    generate_bible_title_card,
    prepare_bible_project,
    regenerate_bible_scene_stills,
)
from app.storage import ROOT as STORAGE_ROOT, job_log_path, p_input

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


def _sleep(stop_event: Event | None, seconds: float) -> bool:
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def loop(stop_event: Event | None = None) -> None:
    while True:
        if stop_event and stop_event.is_set():
            break
        with SessionLocal() as session:
            job = (
                session.execute(
                    select(Job).where(Job.status == "QUEUED").order_by(Job.created_at)
                )
                .scalars()
                .first()
            )
            if job is None:
                if _sleep(stop_event, POLL_INTERVAL):
                    break
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
                is_bible_video = payload.get("workflow") == "bible-video"
                is_scene_animation = payload.get("workflow") == "scene-animation"
                is_scene_stills = payload.get("workflow") == "bible-scene-stills"
                is_bible_title_card = payload.get("workflow") == "bible-title-card"
                if is_scene_animation:
                    final_path = animate_bible_scene(
                        job.project_id,
                        int(payload.get("sceneIndex") or 0),
                        str(payload.get("prompt") or ""),
                        str(payload.get("cameraBehavior") or "locked"),
                        progress=progress,
                        log=log,
                    )
                elif is_scene_stills:
                    final_path = regenerate_bible_scene_stills(
                        job.project_id,
                        [int(index) for index in (payload.get("sceneIndexes") or [])] or None,
                        progress=progress,
                        log=log,
                    )
                elif is_bible_title_card:
                    regenerate_image = bool(payload.get("regenerateImage", True))
                    render_video = bool(payload.get("renderVideo", False))
                    title_card_path = p_input(job.project_id) / "leader" / "bible-title-card.png"
                    if regenerate_image:
                        title_card_path = generate_bible_title_card(
                            job.project_id,
                            str(payload.get("visualStyle") or "") or None,
                            progress=lambda stage, value: progress(stage, value * (0.3 if render_video else 0.95)),
                            log=log,
                        )
                    if render_video:
                        if not title_card_path.exists():
                            raise FileNotFoundError("Generate and approve a Bible title card before rebuilding the video")
                        final_path = render_project(
                            job.project_id,
                            STORAGE_ROOT,
                            options,
                            output_name,
                            log=log,
                            progress=lambda stage, value: progress(stage, (0.3 if regenerate_image else 0.02) + value * (0.69 if regenerate_image else 0.97)),
                        )
                    else:
                        final_path = title_card_path
                else:
                    if is_bible_video:
                        prepare_bible_project(
                            job.project_id,
                            payload,
                            progress=progress,
                            log=log,
                        )
                    def render_progress(stage: str, value: float) -> None:
                        progress(stage, 0.5 + value * 0.49 if is_bible_video else value)

                    final_path = render_project(
                        job.project_id,
                        STORAGE_ROOT,
                        options,
                        output_name,
                        log=log,
                        progress=render_progress,
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
                    kind=(
                        "motion" if is_scene_animation
                        else "still-image" if is_scene_stills
                        else "title-card" if is_bible_title_card and not payload.get("renderVideo", False)
                        else "video"
                    ),
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
        if _sleep(stop_event, 0.3):
            break


if __name__ == "__main__":
    loop()
