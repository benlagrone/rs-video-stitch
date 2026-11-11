"""Listing scenes: Ollama writes the script and 3D plan, then a person approves the render."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.grok_video_request import GrokVideoRequestError, motion_keyframe_times


class SuiteProductionError(ValueError):
    pass


def production_prompt(script: str, scenes: list[dict[str, Any]]) -> str:
    compact = []
    for index, scene in enumerate(scenes, start=1):
        compact.append({
            "scene": index,
            "title": scene.get("title") or f"Scene {index}",
            "narration": scene.get("VO") or scene.get("description") or "",
            "images": scene.get("images") or [],
        })
    return (
        "You are writing a MediaStudio scene package for an existing property listing.\n"
        "Use only facts in the listing script and scene narration. Do not invent prices, amenities, people, or measurements.\n"
        "Return JSON only with this shape:\n"
        "{\"scenes\":[{\"scene\":1,\"visualPrompt\":\"...\",\"motionPlan\":{"
        "\"duration_seconds\":4,\"framing\":\"...\",\"actors\":[{\"id\":\"guest\",\"role\":\"...\"}],"
        "\"action_beats\":[{\"seconds\":\"0.0-2.0\",\"action\":\"...\"}],\"collision_zones\":[\"...\"]}}]}\n"
        "Write one entry per scene, in order.\n"
        "visualPrompt describes the locked photograph and the motion to add. Do not replace the room.\n"
        "The camera stays locked. Do not fly, pan, zoom, or cut to a different room.\n"
        "motionPlan is a 3D blockout plan, not Python and not a Blender script.\n"
        "Each plan has 2 to 4 action beats inside duration_seconds.\n"
        "Actors are fictional placeholders for people or moving objects the photograph does not already show.\n\n"
        f"Listing script:\n{script.strip()}\n\n"
        f"Scenes:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def apply_production_package(scenes: list[dict[str, Any]], package: dict[str, Any]) -> list[dict[str, Any]]:
    incoming = package.get("scenes")
    if not isinstance(incoming, list) or len(incoming) != len(scenes):
        raise SuiteProductionError(f"Ollama must return exactly {len(scenes)} scene plans")
    prepared = []
    for index, scene in enumerate(scenes):
        item = incoming[index]
        if not isinstance(item, dict):
            raise SuiteProductionError(f"Scene {index + 1} plan is missing")
        plan = _motion_plan(item.get("motionPlan"), index + 1)
        prompt = str(item.get("visualPrompt") or "").strip()
        if not prompt:
            raise SuiteProductionError(f"Scene {index + 1} is missing a visual prompt")
        prepared.append({
            "scene": index + 1,
            "title": scene.get("title") or f"Scene {index + 1}",
            "visualPrompt": prompt,
            "motionPlan": plan,
            "planStatus": "draft",
            "renderStatus": "pending",
            "sceneVideoStatus": "pending",
        })
    return prepared


def parse_production_package(raw: str, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        raise SuiteProductionError("Ollama did not return a scene package")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SuiteProductionError("Ollama scene package was not valid JSON") from exc
    return apply_production_package(scenes, payload)


def attach_scene_clip(scene: dict[str, Any], filename: str, provider: str) -> dict[str, Any]:
    """Point the section's opening still at the finished scene clip for the YouTube stitch."""
    images = scene.get("images") or []
    timeline = [dict(entry) for entry in (scene.get("timeline") or []) if isinstance(entry, dict)]
    if not timeline:
        timeline = [{"image": images[0]}] if images else [{}]
    timeline[0]["video"] = filename
    timeline[0]["motionGeneration"] = {"provider": provider, "kind": "suite-scene-video"}
    updated = dict(scene)
    updated["timeline"] = timeline
    return updated


def approve_plan(scene_production: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scene_production.get("motionPlan"), dict):
        raise SuiteProductionError("Approve the 3D plan only after Ollama has written it")
    approved = dict(scene_production)
    approved["motionPlan"] = _motion_plan(scene_production["motionPlan"], int(scene_production.get("scene") or 1))
    approved["planStatus"] = "approved"
    approved["renderStatus"] = "queued"
    return approved


def _blender_binary(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.getenv("BLENDER_BIN"),
        "/usr/bin/blender",
        "/Applications/Blender.app/Contents/MacOS/Blender",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return candidates[-1]


def render_blockout(
    plan: dict[str, Any],
    destination: Path,
    *,
    blender_bin: str | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> Path:
    """Render with the trusted blockout script. The model never supplies the Python."""
    binary = _blender_binary(blender_bin)
    if not Path(binary).is_file():
        raise SuiteProductionError("Sextant Blender is not available to create this 3D render")
    script = Path(__file__).resolve().parent / "blender" / "blockout_from_plan.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    plan_path = destination.with_suffix(".plan.json")
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    command = [
        binary, "--background", "--factory-startup", "--disable-autoexec",
        "--python-exit-code", "1", "--python", str(script), "--",
        "--plan", str(plan_path), "--output", str(destination),
    ]
    execute = runner or subprocess.run
    result = execute(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not destination.is_file():
        detail = (result.stderr or result.stdout or "no output")[-500:]
        raise SuiteProductionError(f"3D blockout render failed: {detail}")
    return destination


def _motion_plan(value: Any, scene_number: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SuiteProductionError(f"Scene {scene_number} motion plan is missing")
    try:
        duration = int(round(float(value.get("duration_seconds") or 4)))
    except (TypeError, ValueError) as exc:
        raise SuiteProductionError(f"Scene {scene_number} duration is unreadable") from exc
    duration = min(8, max(4, duration))
    beats = value.get("action_beats") or []
    if not isinstance(beats, list) or not beats:
        raise SuiteProductionError(f"Scene {scene_number} needs action beats")
    plan = {
        "id": f"scene-{scene_number}",
        "title": f"Scene {scene_number}",
        "duration_seconds": duration,
        "framing": str(value.get("framing") or "locked wide shot of the photographed room").strip(),
        "actors": value.get("actors") if isinstance(value.get("actors"), list) else [],
        "action_beats": beats,
        "collision_zones": value.get("collision_zones") if isinstance(value.get("collision_zones"), list) else [],
    }
    try:
        motion_keyframe_times(plan)
    except GrokVideoRequestError as exc:
        raise SuiteProductionError(f"Scene {scene_number} motion plan cannot be timed: {exc}") from exc
    return plan
