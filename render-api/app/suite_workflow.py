"""MediaStudio routes for a reviewed 3D plan, then Phronesis or Grok, per scene."""
from __future__ import annotations

import json
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.grok_video_workflow import GrokVideoPlanRequest, ImageRef, build_project_request, run_grok_video_job
from app.models import Job, Project
from app.storage import p_input, project_asset_path, read_project_state, save_project_state
from app.suite_production import (
    SuiteProductionError,
    approve_plan,
    attach_scene_clip,
    parse_production_package,
    production_prompt,
    render_blockout,
)


router = APIRouter()


class SceneVideoRequest(BaseModel):
    destination: str = "phronesis"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def write_suite_package(project_id: str, generate) -> dict[str, Any]:
    state, scenes = _project_scenes(project_id)
    script = str(state.get("script") or "").strip()
    if not script:
        raise SuiteProductionError("Add the listing script before writing scene plans")
    raw = generate(production_prompt(script, scenes))
    package = parse_production_package(raw, scenes)
    state["suiteProduction"] = {"scenes": package}
    save_project_state(project_id, state, project_name=str(state.get("title") or project_id))
    return state["suiteProduction"]


def run_suite_blockout(project_id: str, scene_index: int, *, progress, log) -> Any:
    state, _scenes = _project_scenes(project_id)
    item = _production_scene(state, scene_index)
    if item.get("planStatus") != "approved":
        raise SuiteProductionError("Approve the 3D plan before creating the render")
    progress("BLOCKOUT", 0.2)
    destination = project_asset_path(project_id, "motion", f"scene-{scene_index:03d}-blockout.mp4")
    log(f"Creating the approved 3D blockout for scene {scene_index}")
    render_blockout(item["motionPlan"], destination)
    item["renderStatus"] = "rendered"
    item["renderFile"] = destination.name
    save_project_state(project_id, state, project_name=str(state.get("title") or project_id))
    progress("BLOCKOUT_READY", 0.95)
    return destination


def run_suite_scene_video(project_id: str, scene_index: int, destination: str, *, progress, log) -> Any:
    state, scenes = _project_scenes(project_id)
    item = _production_scene(state, scene_index)
    if item.get("renderStatus") != "rendered":
        raise SuiteProductionError("The 3D render has to be finished before making the scene video")
    image_name = (scenes[scene_index - 1].get("images") or [None])[0]
    if not image_name:
        raise SuiteProductionError(f"Scene {scene_index} has no background still")
    request = GrokVideoPlanRequest(
        shot=item["motionPlan"],
        background=ImageRef(subdir="images", filename=image_name, role="room"),
        destination=destination,
        useBlockout=destination == "grok",
        pinBlockout=False,
        blockoutVideo=str(item.get("renderFile") or ""),
    )
    built = build_project_request(project_id, request)
    final_path = run_grok_video_job(
        project_id,
        {"request": request.model_dump(mode="json"), "approvedPrompt": built["plan"]["prompt"]},
        progress=progress,
        log=log,
    )
    stitch_name = f"scene-{scene_index:03d}-scene.mp4"
    stitch_path = project_asset_path(project_id, "motion", stitch_name)
    stitch_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(final_path, stitch_path)
    _write_scene_clip(project_id, scene_index, stitch_name, destination)
    item["sceneVideoStatus"] = "ready"
    item["sceneVideoProvider"] = destination
    item["sceneVideoFile"] = stitch_name
    save_project_state(project_id, state, project_name=str(state.get("title") or project_id))
    return stitch_path


@router.post("/v1/projects/{pid}/suite-production/write")
async def write_suite_production(pid: str) -> dict[str, Any]:
    from app.api import _ollama_generate

    try:
        package = write_suite_package(pid, lambda prompt: _ollama_generate(prompt, temperature=0.3, json_output=True))
    except SuiteProductionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"projectId": pid, "suiteProduction": package}


@router.post("/v1/projects/{pid}/scenes/{scene_index}/3d-render", status_code=202)
async def approve_scene_blockout(pid: str, scene_index: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        state, _scenes = _project_scenes(pid)
        item = _production_scene(state, scene_index)
        state["suiteProduction"]["scenes"][scene_index - 1] = approve_plan(item)
        save_project_state(pid, state, project_name=str(state.get("title") or pid))
    except SuiteProductionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _queue(db, pid, {"workflow": "suite-3d-render", "sceneIndex": scene_index})


@router.post("/v1/projects/{pid}/scenes/{scene_index}/scene-video", status_code=202)
async def queue_scene_video(
    pid: str,
    scene_index: int,
    req: SceneVideoRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if req.destination not in {"grok", "phronesis"}:
        raise HTTPException(status_code=422, detail="destination must be grok or phronesis")
    try:
        state, _scenes = _project_scenes(pid)
        _production_scene(state, scene_index)
    except SuiteProductionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _queue(db, pid, {
        "workflow": "suite-scene-video",
        "sceneIndex": scene_index,
        "destination": req.destination,
    })


def _write_scene_clip(project_id: str, scene_index: int, filename: str, provider: str) -> None:
    scenes_path = p_input(project_id) / "scenes.json"
    document = json.loads(scenes_path.read_text(encoding="utf-8"))
    scenes = document.get("scenes") or []
    if scene_index < 1 or scene_index > len(scenes):
        raise SuiteProductionError(f"Scene {scene_index} is not in the project")
    scenes[scene_index - 1] = attach_scene_clip(scenes[scene_index - 1], filename, provider)
    document["scenes"] = scenes
    scenes_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _project_scenes(project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = read_project_state(project_id)
    if not isinstance(state, dict):
        raise SuiteProductionError("Project not found")
    scenes_path = p_input(project_id) / "scenes.json"
    if not scenes_path.is_file():
        raise SuiteProductionError("Project has no scene sections")
    document = json.loads(scenes_path.read_text(encoding="utf-8"))
    scenes = document.get("scenes") or []
    if not scenes:
        raise SuiteProductionError("Project has no scene sections")
    return state, scenes


def _production_scene(state: dict[str, Any], scene_index: int) -> dict[str, Any]:
    package = state.get("suiteProduction") or {}
    scenes = package.get("scenes") or []
    if scene_index < 1 or scene_index > len(scenes):
        raise SuiteProductionError(f"Scene {scene_index} has no 3D plan")
    item = scenes[scene_index - 1]
    if not isinstance(item, dict):
        raise SuiteProductionError(f"Scene {scene_index} plan is unreadable")
    return item


def _queue(db: Session, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        project = Project(id=project_id)
        db.add(project)
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    db.add(Job(id=job_id, project_id=project_id, status="QUEUED", payload=payload, progress=0.0, stage="QUEUED"))
    db.commit()
    return {"projectId": project_id, "jobId": job_id, "status": "QUEUED"}
