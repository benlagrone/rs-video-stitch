"""Same-origin photo enhancement: read the house, then correct approved photos with Grok."""
from __future__ import annotations

import base64
import mimetypes
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.grok_image_client import GrokImageError, complete_json, edit_image
from app.grok_video_client import GrokVideoError
from app.storage import ensure_dirs, list_asset_files, project_asset_path, read_project_state, save_project_state
from app.virtual_staging import (
    VirtualStagingError,
    edit_prompt,
    is_staged_filename,
    plan_listing,
    staged_filename,
)


router = APIRouter()
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class StagingPlanRequest(BaseModel):
    filenames: list[str] = Field(default_factory=list)
    propertyAddress: str = ""
    area: str = ""


class StagingEditRequest(BaseModel):
    confirmed: bool = False
    plan: dict[str, Any]
    filenames: list[str] = Field(default_factory=list)


@router.post("/v1/projects/{pid}/photo-enhancement/plan")
def create_staging_plan(pid: str, req: StagingPlanRequest) -> dict[str, Any]:
    filenames = _source_filenames(pid, req.filenames)
    state = read_project_state(pid) or {}
    address = req.propertyAddress.strip() or str(state.get("propertyAddress") or "")
    photos = [{"filename": name, "dataUri": _preview_data_uri(pid, name)} for name in filenames]
    try:
        plan = plan_listing(photos, property_address=address, area=req.area, complete=complete_json)
    except VirtualStagingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (GrokImageError, GrokVideoError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _store_plan(pid, plan)
    return {"projectId": pid, "plan": plan}


@router.post("/v1/projects/{pid}/photo-enhancement/edits")
def create_staged_photos(pid: str, req: StagingEditRequest) -> dict[str, Any]:
    if not req.confirmed:
        raise HTTPException(status_code=409, detail="Review the house, area, and enhancement decisions, then enhance with confirmed=true.")
    try:
        plan = _reviewed_plan(req.plan)
        selected = _selected_photos(plan, req.filenames)
    except VirtualStagingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    results = []
    for photo in selected:
        source = project_asset_path(pid, "images", photo["filename"])
        if not source.is_file():
            raise HTTPException(status_code=404, detail=f"Listing photo is missing: {photo['filename']}")
        prompt = edit_prompt(plan, photo)
        data_uri = _data_uri(source.read_bytes(), _media_type(source))
        try:
            staged = edit_image(prompt, data_uri)
        except (GrokImageError, GrokVideoError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        destination = _write_staged_copy(source, staged)
        _remember_staged_image(pid, destination.name)
        results.append({
            "source": photo["filename"],
            "filename": destination.name,
            "url": f"/v1/projects/{pid}/assets/images/{destination.name}",
            "action": photo["action"],
            "group": photo["group"],
        })
    return {"projectId": pid, "results": results}


def _source_filenames(pid: str, requested: list[str]) -> list[str]:
    available = list_asset_files(pid, "images")
    names = requested or [name for name in available if not is_staged_filename(name)]
    if not names:
        raise HTTPException(status_code=422, detail="Add listing photos before planning photo enhancement.")
    missing = [name for name in names if name not in available]
    if missing:
        raise HTTPException(status_code=404, detail=f"Listing photo is missing: {missing[0]}")
    staged = [name for name in names if is_staged_filename(name)]
    if staged:
        raise HTTPException(status_code=422, detail="Plan from the original listing photos.")
    return names


def _reviewed_plan(plan: dict[str, Any]) -> dict[str, Any]:
    photos = plan.get("photos")
    if not isinstance(photos, list) or not photos:
        raise VirtualStagingError("The reviewed plan has no photos")
    from app.virtual_staging import parse_staging_plan

    filenames = [
        str(item.get("filename") or "").strip()
        for item in photos
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    ]
    return parse_staging_plan(
        plan,
        filenames,
        property_address=str(plan.get("property") or ""),
        area=str(plan.get("area") or ""),
    )


def _selected_photos(plan: dict[str, Any], filenames: list[str]) -> list[dict[str, Any]]:
    wanted = set(filenames)
    chosen = []
    for photo in plan["photos"]:
        if wanted and photo["filename"] not in wanted:
            continue
        if photo["action"] == "leave":
            continue
        chosen.append(photo)
    if not chosen:
        raise VirtualStagingError("None of the selected photos are marked to enhance or polish")
    return chosen


def _preview_data_uri(pid: str, filename: str) -> str:
    path = project_asset_path(pid, "images", filename)
    preview = _ffmpeg_preview(path)
    return _data_uri(preview, "image/jpeg")


def _ffmpeg_preview(path: Path) -> bytes:
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Listing photo is missing: {path.name}")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vf", "scale='min(1280,iw)':-2",
        "-frames:v", "1",
        "-q:v", "5",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="ffmpeg is required to read listing photos") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=422, detail=f"Could not read {path.name}: {detail}") from exc
    if not completed.stdout:
        raise HTTPException(status_code=422, detail=f"Could not read {path.name}")
    return completed.stdout


def _write_staged_copy(source: Path, staged: bytes) -> Path:
    if not staged:
        raise HTTPException(status_code=502, detail="xAI returned an empty enhanced photo")
    extension = ".png" if staged.startswith(b"\x89PNG") else ".jpg"
    destination = source.with_name(staged_filename(source.name, extension))
    if destination.resolve() == source.resolve():
        raise HTTPException(status_code=500, detail="Refusing to replace the original listing photo")
    original = source.read_bytes()
    destination.write_bytes(staged)
    if source.read_bytes() != original:
        raise HTTPException(status_code=500, detail="The original listing photo changed during enhancement")
    return destination


def _remember_staged_image(pid: str, filename: str) -> None:
    state = read_project_state(pid) or {}
    images = state.get("images")
    if isinstance(images, list):
        names = {item.get("name") if isinstance(item, dict) else item for item in images}
        if filename not in names:
            images.append({"name": filename})
            save_project_state(pid, state)


def _store_plan(pid: str, plan: dict[str, Any]) -> None:
    ensure_dirs(pid)
    state = read_project_state(pid) or {}
    state["photoEnhancement"] = plan
    save_project_state(pid, state)


def _data_uri(data: bytes, media_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _media_type(path: Path) -> str:
    media_type = MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if media_type not in MEDIA_TYPES.values():
        raise HTTPException(status_code=422, detail=f"{path.name} must be a PNG, JPEG, or WebP photo")
    return media_type
