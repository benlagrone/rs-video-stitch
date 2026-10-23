"""Simple background worker that consumes render jobs."""
from __future__ import annotations

import datetime as dt
import time
import traceback
import uuid
from threading import Event, Thread
from sqlalchemy import select
from app.db import SessionLocal, init_db
from app.models import Artifact, Job
from app.renderer import render_project
from app.bible_workflow import (
    animate_bible_scene,
    generate_bible_title_card,
    prepare_bible_project,
    repair_and_animate_bible_scene,
    regenerate_bible_scene_stills,
)
from app.storage import ROOT as STORAGE_ROOT, job_log_path, p_input

POLL_INTERVAL = 1.0
JOB_LEASE_SECONDS = 120
JOB_HEARTBEAT_SECONDS = 15
LEGACY_RECOVERY_MAX_AGE_SECONDS = 6 * 60 * 60


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


def _lease_expiry(now: dt.datetime | None = None) -> dt.datetime:
    return (now or dt.datetime.utcnow()) + dt.timedelta(seconds=JOB_LEASE_SECONDS)


def recover_orphaned_jobs(session, worker_id: str, now: dt.datetime | None = None) -> list[str]:
    """Return only jobs whose prior worker has stopped heartbeating to the queue."""
    now = now or dt.datetime.utcnow()
    recovered = session.execute(
        select(Job).where(
            Job.status == "RUNNING",
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at < now,
        )
    ).scalars().all()
    for job in recovered:
        _update_job(
            session,
            job,
            status="QUEUED",
            stage="RECOVERING",
            progress=0.0,
            error=None,
            worker_id=None,
            lease_expires_at=None,
        )
        log_file, log = _open_log(job.id)
        log(f"Recovered orphaned job after worker lease expired; requeued by {worker_id}")
        log_file.close()
    return [job.id for job in recovered]


def recover_unleased_jobs_on_startup(
    session,
    worker_id: str,
    now: dt.datetime | None = None,
) -> list[str]:
    """Recover only recent jobs left by pre-lease workers during an upgrade."""
    now = now or dt.datetime.utcnow()
    cutoff = now - dt.timedelta(seconds=LEGACY_RECOVERY_MAX_AGE_SECONDS)
    recovered = session.execute(
        select(Job).where(
            Job.status == "RUNNING",
            Job.lease_expires_at.is_(None),
            Job.updated_at >= cutoff,
        )
    ).scalars().all()
    for job in recovered:
        _update_job(
            session,
            job,
            status="QUEUED",
            stage="RECOVERING",
            progress=0.0,
            error=None,
            worker_id=None,
        )
        log_file, log = _open_log(job.id)
        log(f"Recovered unleased job during worker startup; requeued by {worker_id}")
        log_file.close()
    return [job.id for job in recovered]


def _heartbeat(job_id: str, worker_id: str, stop_event: Event) -> None:
    while not stop_event.wait(JOB_HEARTBEAT_SECONDS):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "RUNNING" or job.worker_id != worker_id:
                return
            _update_job(session, job, lease_expires_at=_lease_expiry())


def loop(stop_event: Event | None = None) -> None:
    init_db()
    worker_id = f"worker-{uuid.uuid4().hex}"
    with SessionLocal() as session:
        recover_unleased_jobs_on_startup(session, worker_id)
    while True:
        if stop_event and stop_event.is_set():
            break
        with SessionLocal() as session:
            recover_orphaned_jobs(session, worker_id)
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
            _update_job(
                session,
                job,
                status="RUNNING",
                stage="VALIDATE",
                progress=0.02,
                worker_id=worker_id,
                lease_expires_at=_lease_expiry(),
            )
            payload = job.payload or {}
            options = payload.get("renderOptions", {})
            output_name = payload.get("outputName", "video.mp4")

            log_file, log = _open_log(job_id)
            log(f"Starting job for project {job.project_id}")
            heartbeat_stop = Event()
            heartbeat = Thread(target=_heartbeat, args=(job_id, worker_id, heartbeat_stop), daemon=True)
            heartbeat.start()

            def progress(stage: str, value: float) -> None:
                _update_job(session, job, stage=stage, progress=min(1.0, value))

            try:
                is_bible_video = payload.get("workflow") == "bible-video"
                is_scene_animation = payload.get("workflow") == "scene-animation"
                is_scene_animation_repair = payload.get("workflow") == "scene-animation-repair"
                is_scene_stills = payload.get("workflow") == "bible-scene-stills"
                is_bible_title_card = payload.get("workflow") == "bible-title-card"
                if is_scene_animation_repair:
                    final_path = repair_and_animate_bible_scene(
                        job.project_id,
                        int(payload.get("sceneIndex") or 0),
                        str(payload.get("prompt") or ""),
                        str(payload.get("cameraBehavior") or "locked"),
                        dict(payload.get("motionPlan") or {}),
                        progress=progress,
                        log=log,
                    )
                elif is_scene_animation:
                    final_path = animate_bible_scene(
                        job.project_id,
                        int(payload.get("sceneIndex") or 0),
                        str(payload.get("prompt") or ""),
                        str(payload.get("cameraBehavior") or "locked"),
                        dict(payload.get("motionPlan") or {}),
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
                        "motion" if is_scene_animation or is_scene_animation_repair
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
                heartbeat_stop.set()
                heartbeat.join(timeout=JOB_HEARTBEAT_SECONDS + 1)
                log_file.close()
        if _sleep(stop_event, 0.3):
            break


if __name__ == "__main__":
    loop()
