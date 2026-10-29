"""Prompt-to-3D scene planning and Blender previsualization for Bible scenes."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import requests

from app.gpu_admission import GpuAdmissionError, governed_post
from app.storage import p_input, read_project_state, save_scenes


OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://fortress.lan:11434")
OLLAMA_PROMPT_MODEL = os.getenv("OLLAMA_PROMPT_MODEL", "mistral:latest")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
BLENDER_EXECUTABLE = os.getenv("BLENDER_EXECUTABLE", "blender")
BLENDER_RENDER_SCRIPT = Path(__file__).with_name("blender_scene_renderer.py")

Progress = Callable[[str, float], None]
Log = Callable[[str], None]


def _clean_text(value: Any, limit: int = 3000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _scene_context(project_id: str, scene_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    path = p_input(project_id) / "scenes.json"
    if not path.exists():
        raise FileNotFoundError(f"Project {project_id} has no scenes.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    scenes = document.get("scenes") or []
    if scene_index < 1 or scene_index > len(scenes):
        raise IndexError(f"Scene {scene_index} does not exist")
    return document, scenes[scene_index - 1]


def _fallback_genesis_1_1_brief(scene: dict[str, Any], image_prompt: str, motion_prompt: str) -> dict[str, Any]:
    return {
        "version": 1,
        "reference": _clean_text(scene.get("title"), 120),
        "durationSeconds": 5.0625,
        "frameRate": 16,
        "camera": {
            "behavior": "locked",
            "position": [0.0, -9.0, 3.2],
            "target": [0.0, 0.0, 1.7],
            "lensMm": 48,
        },
        "environment": {
            "kind": "primordial-cosmos",
            "description": "A dark primordial cosmos above an unfinished rocky world with a distant luminous horizon.",
            "palette": ["#030712", "#172554", "#7c2d12", "#f59e0b"],
        },
        "objects": [
            {
                "id": "forming-earth",
                "label": "Forming earth",
                "primitive": "sphere",
                "scale": [0.95, 0.95, 0.95],
                "material": "rocky-planet",
                "startPosition": [1.0, 0.15, 2.85],
                "endPosition": [0.5, 0.15, 1.55],
                "rotationDegrees": [0.0, 0.0, 42.0],
                "action": "The single forming planet begins high in frame and descends smoothly into the lower scene.",
            },
            {
                "id": "unfinished-ground",
                "label": "Unfinished earth surface",
                "primitive": "plane",
                "scale": [8.0, 8.0, 1.0],
                "material": "dark-rock",
                "startPosition": [0.0, 1.8, 0.0],
                "endPosition": [0.0, 1.8, 0.0],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "The ground remains fixed while subtle warm light travels across its surface.",
            },
        ],
        "actionBeats": [
            {"seconds": "0.0-0.8", "action": "Hold the primordial environment and establish the planet high in frame."},
            {"seconds": "0.8-4.2", "action": "Move the one planet down and slightly inward on a smooth continuous vector."},
            {"seconds": "4.2-5.0625", "action": "Settle the planet into its final lower position while the horizon glow strengthens."},
        ],
        "continuityRules": [
            "Exactly one planet is visible.",
            "The camera, horizon, frame edges, and ground remain fixed.",
            "The planet never duplicates, collides with a second world, or becomes a moving crop.",
        ],
        "imagePrompt": image_prompt,
        "motionPrompt": motion_prompt,
        "videoPrompt": (
            "Fixed wide camera in a primordial cosmos. Exactly one forming rocky planet begins high in frame and moves "
            "smoothly downward and slightly inward, settling above the unfinished earth. The surface and horizon remain "
            "stable; only the planet trajectory and restrained atmospheric light move. Preserve a single coherent planet "
            "with natural parallax, continuous rotation, no collision, no duplicate world, no crop animation, and no cut."
        ),
    }


def _fallback_genesis_1_2_brief(scene: dict[str, Any], image_prompt: str, motion_prompt: str) -> dict[str, Any]:
    return {
        "version": 1,
        "reference": _clean_text(scene.get("title"), 120),
        "durationSeconds": 5.0625,
        "frameRate": 16,
        "camera": {
            "behavior": "locked",
            "position": [0.0, -10.0, 4.0],
            "target": [0.0, 1.8, 2.0],
            "lensMm": 45,
        },
        "environment": {
            "kind": "primordial-ocean",
            "description": "An endless dark ocean beneath one enormous unfinished world and a low misty horizon.",
            "palette": ["#020617", "#0f172a", "#475569", "#f8fafc"],
        },
        "objects": [
            {
                "id": "unfinished-earth",
                "label": "One enormous unfinished world",
                "primitive": "sphere",
                "scale": [3.7, 3.7, 3.7],
                "material": "rocky-planet",
                "startPosition": [0.0, 4.6, 6.1],
                "endPosition": [0.0, 4.6, 6.1],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "The single world remains fixed in frame while its surface rotates slowly.",
            },
            {
                "id": "primordial-ocean",
                "label": "Primordial ocean",
                "primitive": "plane",
                "scale": [8.0, 10.0, 1.0],
                "material": "water",
                "startPosition": [0.0, 2.2, 0.0],
                "endPosition": [0.0, 1.7, 0.0],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "The ocean surface advances subtly toward the fixed camera.",
            },
            {
                "id": "near-wave",
                "label": "Near wave crest",
                "primitive": "torus",
                "scale": [4.6, 1.3, 0.16],
                "material": "water",
                "startPosition": [0.0, 1.8, 0.34],
                "endPosition": [0.0, -0.8, 0.42],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "A broad wave crest travels toward the foreground without changing the horizon.",
            },
            {
                "id": "far-wave",
                "label": "Far wave crest",
                "primitive": "torus",
                "scale": [5.2, 1.45, 0.12],
                "material": "water",
                "startPosition": [0.0, 3.8, 0.22],
                "endPosition": [0.0, 1.2, 0.30],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "A second wave crest follows the first toward the foreground.",
            },
            {
                "id": "spirit-light",
                "label": "Diffuse Spirit light over the water",
                "primitive": "torus",
                "scale": [2.6, 0.52, 0.08],
                "material": "gold",
                "startPosition": [-1.6, 0.7, 0.56],
                "endPosition": [1.6, 0.7, 0.56],
                "rotationDegrees": [0.0, 0.0, 0.0],
                "action": "A diffuse luminous path moves left to right over existing wave tops without becoming a figure or orb.",
            },
        ],
        "actionBeats": [
            {"seconds": "0.0-0.8", "action": "Hold the fixed world and establish the dark ocean."},
            {"seconds": "0.8-4.2", "action": "Advance two broad wave crests while the diffuse light crosses their surface."},
            {"seconds": "4.2-5.0625", "action": "Let the light settle at right-center while the waves continue forward."},
        ],
        "continuityRules": [
            "Exactly one world is visible and it remains fixed in frame.",
            "The camera, horizon, frame edges, and global exposure remain fixed.",
            "The Spirit is represented only by diffuse light moving over existing water, never by a person, orb, or solid object.",
            "Wave motion travels toward the foreground without a cut, collision, duplicate object, or moving crop.",
        ],
        "imagePrompt": image_prompt,
        "motionPrompt": motion_prompt,
        "videoPrompt": (
            "Fixed wide camera over a dark primordial ocean beneath one enormous unfinished world. Two broad wave crests "
            "travel continuously toward the foreground while a diffuse silver-gold light moves left to right across their "
            "surface, parting low mist and brightening nearby ripples. Keep the single world, horizon, palette, composition, "
            "and camera fixed; no person, orb, duplicate world, collision, crop animation, or cut."
        ),
    }


def _generic_fallback_brief(scene: dict[str, Any], image_prompt: str, motion_prompt: str) -> dict[str, Any]:
    return {
        "version": 1,
        "reference": _clean_text(scene.get("title"), 120),
        "durationSeconds": 5.0625,
        "frameRate": 16,
        "camera": {"behavior": "locked", "position": [0.0, -8.5, 3.0], "target": [0.0, 0.0, 1.5], "lensMm": 50},
        "environment": {"kind": "scenic-stage", "description": image_prompt, "palette": ["#111827", "#334155", "#d97706"]},
        "objects": [
            {
                "id": "primary-subject", "label": "Primary subject", "primitive": "sphere",
                "scale": [1.0, 1.0, 1.0], "material": "stone",
                "startPosition": [-0.4, 0.0, 1.8], "endPosition": [0.4, 0.0, 1.8],
                "rotationDegrees": [0.0, 0.0, 30.0], "action": motion_prompt or "The primary subject moves continuously.",
            },
            {
                "id": "ground", "label": "Ground", "primitive": "plane", "scale": [7.0, 7.0, 1.0],
                "material": "dark-rock", "startPosition": [0.0, 1.5, 0.0], "endPosition": [0.0, 1.5, 0.0],
                "rotationDegrees": [0.0, 0.0, 0.0], "action": "The environment remains fixed.",
            },
        ],
        "actionBeats": [{"seconds": "0.0-5.0625", "action": motion_prompt or "Execute one continuous visible action."}],
        "continuityRules": ["Keep one coherent subject.", "Keep the camera and background fixed.", "Do not introduce new objects."],
        "imagePrompt": image_prompt,
        "motionPrompt": motion_prompt,
        "videoPrompt": motion_prompt or "One coherent subject performs one continuous action with a fixed camera.",
    }


def _sanitize_brief(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    result = dict(fallback)
    result["videoPrompt"] = _clean_text(raw.get("videoPrompt") or fallback["videoPrompt"], 2400)
    result["actionBeats"] = list(raw.get("actionBeats") or fallback["actionBeats"])[:8]
    result["continuityRules"] = [
        _clean_text(rule, 220) for rule in (raw.get("continuityRules") or fallback["continuityRules"])
    ][:12]
    objects = []
    allowed_primitives = {"sphere", "plane", "cube", "cone", "torus"}
    for index, item in enumerate(list(raw.get("objects") or fallback["objects"])[:8]):
        base = (fallback["objects"] + fallback["objects"])[min(index, len(fallback["objects"]) - 1)]
        primitive = str(item.get("primitive") or base["primitive"]).lower()
        if primitive not in allowed_primitives:
            primitive = base["primitive"]
        def vector(name: str, default: list[float]) -> list[float]:
            value = item.get(name)
            if not isinstance(value, list) or len(value) != 3:
                return list(default)
            return [max(-12.0, min(12.0, float(part))) for part in value]
        objects.append({
            "id": re.sub(r"[^a-z0-9-]+", "-", str(item.get("id") or f"object-{index + 1}").lower()).strip("-")[:64],
            "label": _clean_text(item.get("label") or base["label"], 100),
            "primitive": primitive,
            "scale": [max(0.05, abs(part)) for part in vector("scale", base["scale"])],
            "material": re.sub(r"[^a-z0-9-]+", "-", str(item.get("material") or base["material"]).lower()).strip("-")[:64],
            "startPosition": vector("startPosition", base["startPosition"]),
            "endPosition": vector("endPosition", base["endPosition"]),
            "rotationDegrees": vector("rotationDegrees", base["rotationDegrees"]),
            "action": _clean_text(item.get("action") or base["action"], 320),
        })
    result["objects"] = objects or fallback["objects"]
    return result


def generate_scene_3d_brief(
    project_id: str,
    scene_index: int,
    motion_instruction: str = "",
    motion_plan: dict[str, Any] | None = None,
    *,
    session=requests,
) -> dict[str, Any]:
    _, scene = _scene_context(project_id, scene_index)
    timeline = (scene.get("timeline") or [{}])[0]
    image_prompt = _clean_text((timeline.get("imageGeneration") or {}).get("prompt") or timeline.get("prompt"))
    motion_prompt = _clean_text(motion_instruction or scene.get("motionPrompt") or scene.get("action"))
    reference = _clean_text(scene.get("title"), 120)
    is_genesis_one_one = bool(re.match(r"^genesis\s+1:1\b", reference, flags=re.IGNORECASE))
    is_genesis_one_two = bool(re.match(r"^genesis\s+1:2\b", reference, flags=re.IGNORECASE))
    if is_genesis_one_one:
        fallback = _fallback_genesis_1_1_brief(scene, image_prompt, motion_prompt)
    elif is_genesis_one_two:
        fallback = _fallback_genesis_1_2_brief(scene, image_prompt, motion_prompt)
    else:
        fallback = _generic_fallback_brief(scene, image_prompt, motion_prompt)
    prompt = (
        "Return only valid JSON for a Blender previsualization brief. Translate the approved image prompt and motion "
        "prompt into a fixed-camera 5.0625-second 3D shot. Use only these primitives: sphere, plane, cube, cone, torus. "
        "Use at most 8 objects and never invent a duplicate subject. Each object requires id, label, primitive, scale "
        "[x,y,z], material, startPosition [x,y,z], endPosition [x,y,z], rotationDegrees [x,y,z], and action. Also return "
        "actionBeats, continuityRules, and one concrete videoPrompt describing object trajectories. Preserve the exact "
        "object counts and vectors in the motion plan; keep a locked camera. JSON shape: "
        "{\"objects\":[...],\"actionBeats\":[...],\"continuityRules\":[...],\"videoPrompt\":\"...\"}.\n\n"
        f"Reference: {reference}\nScripture: {_clean_text(scene.get('VO') or scene.get('description'))}\n"
        f"Approved image prompt: {image_prompt}\nApproved motion prompt: {motion_prompt}\n"
        f"Structured motion plan: {json.dumps(motion_plan or scene.get('motionPlan') or {}, separators=(',', ':'))[:5000]}"
    )
    try:
        response = governed_post(
            session,
            f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
            workload_class="ollama",
            vram_required_mb=8192,
            priority=4,
            json={
                "model": OLLAMA_PROMPT_MODEL,
                "prompt": prompt,
                "stream": False,
                "keep_alive": 0,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 1400},
            },
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        content = str(payload.get("response") or "").strip()
        brief = _sanitize_brief(json.loads(content), fallback)
        if is_genesis_one_one or is_genesis_one_two:
            brief["objects"] = fallback["objects"]
            brief["actionBeats"] = fallback["actionBeats"]
            brief["continuityRules"] = fallback["continuityRules"]
            brief["videoPrompt"] = fallback["videoPrompt"]
        brief["source"] = "ollama-image+motion-prompts"
    except (GpuAdmissionError, requests.RequestException, json.JSONDecodeError, TypeError, ValueError):
        brief = fallback
        brief["source"] = "deterministic-fallback"
    brief["imagePrompt"] = image_prompt
    brief["motionPrompt"] = motion_prompt
    brief["motionPlan"] = motion_plan or scene.get("motionPlan") or {}
    return brief


def render_scene_previsualization(
    project_id: str,
    scene_index: int,
    motion_instruction: str = "",
    motion_plan: dict[str, Any] | None = None,
    *,
    progress: Progress,
    log: Log,
) -> Path:
    document, scene = _scene_context(project_id, scene_index)
    progress("PLANNING_3D_SCENE", 0.12)
    brief = generate_scene_3d_brief(project_id, scene_index, motion_instruction, motion_plan)
    work_dir = p_input(project_id).parent / "work" / "previs" / f"scene_{scene_index:03d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    brief_path = work_dir / "shot-brief.json"
    blend_path = work_dir / f"scene_{scene_index:03d}-guide.blend"
    video_name = f"scene_{scene_index:03d}-3d-guide.mp4"
    depth_video_name = f"scene_{scene_index:03d}-3d-depth.mp4"
    poster_name = f"scene_{scene_index:03d}-3d-guide.png"
    video_path = p_input(project_id) / "motion" / video_name
    depth_video_path = p_input(project_id) / "motion" / depth_video_name
    poster_path = p_input(project_id) / "images" / poster_name
    brief_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")

    executable = shutil.which(BLENDER_EXECUTABLE)
    if not executable:
        raise RuntimeError("Blender is not installed in the MediaStudio worker")
    progress("RENDERING_3D_GUIDE", 0.28)
    log(f"Rendering Blender guide for scene {scene_index} from approved image and motion prompts")
    command = [
        executable, "--background", "--python", str(BLENDER_RENDER_SCRIPT), "--",
        "--brief", str(brief_path), "--video", str(video_path),
        "--depth-video", str(depth_video_path), "--poster", str(poster_path),
        "--blend", str(blend_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30 * 60)
    if result.returncode != 0:
        raise RuntimeError(f"Blender previsualization failed: {(result.stderr or result.stdout)[-1200:]}")
    if not video_path.exists() or video_path.stat().st_size == 0:
        raise RuntimeError("Blender completed without a 3D guide video")
    if not depth_video_path.exists() or depth_video_path.stat().st_size == 0:
        raise RuntimeError("Blender completed without a 3D depth-control video")

    scene["previsualization"] = {
        "status": "ready",
        "brief": brief,
        "briefPath": f"work/previs/scene_{scene_index:03d}/shot-brief.json",
        "blendPath": f"work/previs/scene_{scene_index:03d}/scene_{scene_index:03d}-guide.blend",
        "video": video_name,
        "depthVideo": depth_video_name,
        "poster": poster_name,
        "updatedAt": time.time(),
    }
    project_name = str((document.get("info") or {}).get("name") or project_id)
    save_scenes(project_id, json.dumps(document, indent=2), project_name=project_name)
    progress("GUIDE_3D_READY", 0.95)
    return video_path


def guided_video_inputs(project_id: str, scene_index: int) -> tuple[str, dict[str, Any], Path, Path]:
    _, scene = _scene_context(project_id, scene_index)
    previs = scene.get("previsualization") or {}
    brief = previs.get("brief") or {}
    if previs.get("status") != "ready" or not brief:
        raise ValueError("Build and review the 3D guide before generating a 3D-guided video")
    video_prompt = _clean_text(brief.get("videoPrompt"), 2200)
    continuity = "; ".join(_clean_text(rule, 180) for rule in (brief.get("continuityRules") or []))
    resolved_prompt = f"{video_prompt} 3D staging constraints: {continuity}".strip()
    guide_name = str(previs.get("video") or "").strip()
    guide_path = p_input(project_id) / "motion" / guide_name
    if not guide_name or not guide_path.exists():
        raise ValueError("The reviewed 3D guide video is missing; rebuild it before generating video")
    depth_name = str(previs.get("depthVideo") or "").strip()
    depth_path = p_input(project_id) / "motion" / depth_name
    if not depth_name or not depth_path.exists():
        raise ValueError("The reviewed 3D depth-control video is missing; rebuild the 3D guide before generating video")
    return (
        resolved_prompt,
        dict(brief.get("motionPlan") or scene.get("motionPlan") or {}),
        guide_path,
        depth_path,
    )
