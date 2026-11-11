"""Queue a reviewed Grok video request built from a 3D motion-previs shot."""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.grok_video_client import GrokVideoError, download_video, submit_video, wait_for_video
from app.grok_video_request import GrokVideoRequestError, build_grok_video_request, motion_keyframe_times
from app.models import Job, Project
from app.storage import p_output, project_asset_path


router = APIRouter()
ALLOWED_ASSET_DIRS = {"images", "footage", "leader"}
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class ImageRef(BaseModel):
    subdir: str
    filename: str
    role: str = "appearance"


class GrokVideoPlanRequest(BaseModel):
    shotId: str = ""
    shot: Optional[Dict[str, Any]] = None
    background: Optional[ImageRef] = None
    appearance: List[ImageRef] = Field(default_factory=list)
    plate: Optional[ImageRef] = None
    endPlate: Optional[ImageRef] = None
    destination: str = "grok"
    useBlockout: bool = True
    pinBlockout: bool = False
    blockoutVideo: str = ""
    resolution: str = "720p"
    aspectRatio: str = "16:9"


class GrokVideoJobRequest(GrokVideoPlanRequest):
    confirmed: bool = False
    approvedPrompt: str = ""


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def previs_library() -> Path:
    configured = os.getenv("MEDIASTUDIO_PREVIS_LIBRARY", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "capabilities" / "motion-previs" / "library"


def load_shot(shot_id: str) -> tuple[dict[str, Any], Path | None]:
    if not shot_id or "/" in shot_id or shot_id.startswith("."):
        raise GrokVideoRequestError("Choose a motion-previs shot id")
    library = previs_library().resolve()
    contract = (library / shot_id / "shot.yaml").resolve()
    try:
        contract.relative_to(library)
    except ValueError as exc:
        raise GrokVideoRequestError("The motion shot must stay inside the previs library") from exc
    if not contract.is_file():
        raise GrokVideoRequestError(f"Motion shot '{shot_id}' has no shot.yaml")
    shot = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(shot, dict):
        raise GrokVideoRequestError(f"Motion shot '{shot_id}' is not a shot contract")
    proxy = _proxy_video(library, shot_id)
    return shot, proxy


def build_project_request(project_id: str, request: GrokVideoPlanRequest) -> dict[str, Any]:
    destination = request.destination.strip().lower()
    if destination not in {"grok", "phronesis"}:
        raise GrokVideoRequestError("destination must be grok or phronesis")
    shot, proxy = _resolve_shot(request)
    if proxy is None and request.blockoutVideo:
        rendered = project_asset_path(project_id, "motion", request.blockoutVideo)
        if rendered.is_file():
            proxy = rendered
    background_ref = request.background or request.plate
    if destination == "phronesis" and background_ref is None:
        raise GrokVideoRequestError("Phronesis video needs a background still")
    if destination == "phronesis" and request.pinBlockout:
        raise GrokVideoRequestError(
            "Phronesis animates the background still. Pinning the 3D blockout would replace that room."
        )
    plate = _load_image(project_id, background_ref) if background_ref else None
    end_plate = _load_image(project_id, request.endPlate) if request.endPlate else None
    appearance = [_load_image(project_id, item) for item in request.appearance]
    send_blockout = destination == "grok" and (request.useBlockout or request.pinBlockout)
    blockout = []
    if request.pinBlockout and proxy is None:
        raise GrokVideoRequestError("This motion shot has no rendered 3D blockout video to pin")
    if proxy is not None and send_blockout:
        duration = float(shot.get("duration_seconds") or 0)
        times = [0.0, *motion_keyframe_times(shot), max(0.0, duration - 0.05)]
        blockout = _sample_blockout(proxy, times)
    built = build_grok_video_request(
        shot,
        plate=plate,
        end_plate=end_plate,
        appearance=appearance,
        blockout_frames=blockout,
        pin_blockout=request.pinBlockout,
        resolution=request.resolution,
        aspect_ratio=request.aspectRatio,
    )
    built["plan"]["destination"] = destination
    built["plan"]["layers"] = {
        "background": None if background_ref is None else background_ref.filename,
        "motion": f"sextant-blender:{(shot.get('id') or request.shotId or 'custom')}",
    }
    if destination == "phronesis":
        built["plan"]["warnings"].append(
            "Phronesis receives the background still and the 3D shot's timing. "
            "Sextant Blender is the 3D layer; Phronesis does not render the people itself."
        )
    elif request.useBlockout and proxy is None and not request.pinBlockout:
        built["plan"]["warnings"].append(
            "This shot has no rendered 3D blockout, so the request uses the timed shot plan only."
        )
    return built


def run_grok_video_job(project_id: str, payload: dict[str, Any], *, progress, log) -> Path:
    request = GrokVideoPlanRequest.model_validate(payload.get("request") or {})
    approved = str(payload.get("approvedPrompt") or "")
    progress("GROK_PLAN", 0.1)
    built = build_project_request(project_id, request)
    prompt = built["plan"]["prompt"]
    if prompt != approved:
        raise GrokVideoError("The motion plan changed after it was reviewed. Review the new plan before generating.")
    if built["plan"].get("destination") == "phronesis":
        return _run_phronesis_video(project_id, request, prompt, progress, log)
    log(f"Submitting reviewed Grok request for shot {built['plan'].get('shotId') or 'custom'}")
    progress("GROK_SUBMIT", 0.25)
    request_id = submit_video(built["body"])
    log(f"xAI request {request_id} is generating")
    progress("GROK_WAIT", 0.45)
    result = wait_for_video(request_id)
    video = (result.get("video") or {}) if isinstance(result, dict) else {}
    url = str(video.get("url") or "")
    if not url:
        raise GrokVideoError("xAI finished without a video URL")
    destination = p_output(project_id) / "grok-video" / f"{request_id}.mp4"
    progress("GROK_DOWNLOAD", 0.85)
    download_video(url, destination)
    plan_path = destination.with_suffix(".plan.json")
    plan_path.write_text(
        __import__("json").dumps(built["plan"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log(f"Saved Grok video {destination.name}")
    return destination


@router.post("/v1/projects/{pid}/grok-video/plan")
async def grok_video_plan(pid: str, req: GrokVideoPlanRequest) -> dict[str, Any]:
    try:
        built = build_project_request(pid, req)
    except GrokVideoRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"projectId": pid, "plan": built["plan"]}


@router.post("/v1/projects/{pid}/grok-video/jobs", status_code=202)
async def grok_video_job(
    pid: str,
    req: GrokVideoJobRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        built = build_project_request(pid, req)
    except GrokVideoRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not req.confirmed or req.approvedPrompt != built["plan"]["prompt"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Review the motion plan and submit it again with confirmed=true and the approved prompt.",
                "plan": built["plan"],
            },
        )
    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
        db.add(project)
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    db.add(
        Job(
            id=job_id,
            project_id=pid,
            status="QUEUED",
            payload={
                "workflow": "phronesis-video" if built["plan"].get("destination") == "phronesis" else "grok-video",
                "request": req.model_dump(mode="json"),
                "approvedPrompt": req.approvedPrompt,
            },
            progress=0.0,
            stage="QUEUED",
        )
    )
    db.commit()
    return {"projectId": pid, "jobId": job_id, "status": "QUEUED", "shotId": built["plan"].get("shotId")}


def _resolve_shot(request: GrokVideoPlanRequest) -> tuple[dict[str, Any], Path | None]:
    if request.shot is not None:
        if not isinstance(request.shot, dict):
            raise GrokVideoRequestError("A supplied motion shot must be an object")
        return request.shot, None
    return load_shot(request.shotId.strip())


def _run_phronesis_video(project_id: str, request: GrokVideoPlanRequest, prompt: str, progress, log) -> Path:
    from app.motion_provider import generate_motion_clip

    background = request.background or request.plate
    if background is None:
        raise GrokVideoError("Phronesis video needs a background still")
    image_path = project_asset_path(project_id, background.subdir, background.filename)
    if not image_path.is_file():
        raise GrokVideoError(f"Background still is missing: {background.filename}")
    destination = p_output(project_id) / "phronesis-video" / f"{uuid.uuid4().hex}.mp4"
    progress("PHRONESIS_MOTION", 0.4)
    log(f"Sending the background still and 3D motion plan to Phronesis for {request.shotId or 'custom shot'}")
    generate_motion_clip(
        image_path,
        destination,
        prompt=prompt,
        negative_prompt=(
            "replacing the background room, extra people, new furniture, camera move, "
            "clay render, blockout materials, morphing, flicker"
        ),
        camera_behavior="locked",
    )
    log(f"Saved Phronesis video {destination.name}")
    return destination


def _proxy_video(library: Path, shot_id: str) -> Path | None:
    guides = library / shot_id / "guides"
    if not guides.is_dir():
        return None
    matches = sorted(guides.glob("*rgb-proxy*.mp4"))
    return matches[0] if matches else None


def _load_image(project_id: str, ref: ImageRef) -> tuple[bytes, str, str]:
    if ref.subdir not in ALLOWED_ASSET_DIRS:
        raise GrokVideoRequestError(f"Images must come from {', '.join(sorted(ALLOWED_ASSET_DIRS))}")
    path = project_asset_path(project_id, ref.subdir, ref.filename)
    if not path.is_file():
        raise GrokVideoRequestError(f"Project image is missing: {ref.filename}")
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise GrokVideoRequestError(f"{ref.filename} must be a PNG, JPEG, or WebP image")
    label = ref.role.strip() or path.name
    return path.read_bytes(), media_type, f"{label} ({path.name})"


def _sample_blockout(video: Path, times: list[float]) -> list[tuple[float, bytes, str]]:
    frames = []
    seen: set[float] = set()
    with tempfile.TemporaryDirectory(prefix="grok-blockout-") as temp_dir:
        for timestamp in times:
            rounded = round(float(timestamp), 3)
            if rounded in seen or rounded < 0:
                continue
            seen.add(rounded)
            destination = Path(temp_dir) / f"frame-{len(frames)}.jpg"
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{rounded:.3f}", "-i", str(video),
                "-frames:v", "1", "-q:v", "3", str(destination),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise GrokVideoRequestError(f"Unable to sample the 3D blockout at {rounded:.3f}s: {exc}") from exc
            if destination.is_file() and destination.stat().st_size:
                frames.append((rounded, destination.read_bytes(), "image/jpeg"))
    if not frames:
        raise GrokVideoRequestError("The 3D blockout did not yield any cue frames")
    return frames
