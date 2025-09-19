"""FastAPI entrypoint for the render API."""
from __future__ import annotations

import io
import json
import os
import uuid
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.models import Job, Project
from app.schemas import ProjectSpec, RenderRequest
from app.storage import ensure_dirs, job_log_path, list_outputs, p_input, p_output, save_scenes

ALLOW_ORIGINS = (
    os.getenv("ALLOW_ORIGINS", "").split(",")
    if os.getenv("ALLOW_ORIGINS")
    else ["*"]
)

app = FastAPI(title="Render API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> dict:
    return {"ok": True}


@app.put("/v1/projects/{pid}/scenes")
async def upsert_scenes(
    pid: str,
    spec: ProjectSpec,
    db: Session = Depends(get_db),
) -> dict:
    ensure_dirs(pid)
    payload = json.dumps(spec.model_dump(mode="json", by_alias=True), indent=2)
    save_scenes(pid, payload)

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
    db.add(project)
    db.commit()

    return {"projectId": pid, "ok": True}


@app.post("/v1/projects/{pid}/assets")
async def upload_assets(
    pid: str,
    files: list[UploadFile] = File(...),
    subdir: str = Form("images"),
) -> dict:
    allowed = {"images", "voiceovers"}
    if subdir not in allowed:
        raise HTTPException(status_code=400, detail="Invalid subdir")

    ensure_dirs(pid)
    dest = p_input(pid) / subdir
    dest.mkdir(parents=True, exist_ok=True)

    count = 0
    for upload in files:
        data = await upload.read()
        if not upload.filename:
            continue
        target = dest / upload.filename
        target.write_bytes(data)
        count += 1

    return {"projectId": pid, "count": count, "subdir": subdir}


@app.post("/v1/projects/{pid}/render")
async def render(
    pid: str,
    req: RenderRequest,
    db: Session = Depends(get_db),
) -> dict:
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    payload = req.model_dump(mode="json", by_alias=True)

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
    project.last_output_name = req.outputName
    db.add(project)

    job = Job(
        id=job_id,
        project_id=pid,
        status="QUEUED",
        payload=payload,
        progress=0.0,
        stage="QUEUED",
    )
    db.add(job)
    db.commit()

    return {"jobId": job_id}


def _tail_logs(job_id: str, limit_bytes: int = 4096) -> str:
    path = job_log_path(job_id)
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) <= limit_bytes:
        return data.decode("utf-8", errors="ignore")
    return data[-limit_bytes:].decode("utf-8", errors="ignore")


@app.get("/v1/jobs/{job_id}")
async def job_status(
    job_id: str,
    db: Session = Depends(get_db),
) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "jobId": job.id,
        "projectId": job.project_id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "etaSeconds": None,
        "error": job.error,
        "logs": _tail_logs(job_id),
    }


@app.get("/v1/projects/{pid}/outputs")
async def outputs(pid: str) -> dict:
    return {"projectId": pid, "files": list_outputs(pid)}


@app.get("/v1/projects/{pid}/outputs/video")
async def download_video(
    pid: str,
    filename: Optional[str] = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    project = db.get(Project, pid)
    preferred = filename or (project.last_output_name if project else None) or "video.mp4"
    target = p_output(pid) / preferred
    if not target.exists():
        raise HTTPException(status_code=404, detail="video not found")

    file_like = open(target, "rb")
    headers = {"Content-Disposition": f"attachment; filename=\"{preferred}\""}
    return StreamingResponse(file_like, media_type="video/mp4", headers=headers)
