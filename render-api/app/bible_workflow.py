"""Server-side Bible passage to storyboard media workflow."""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.art_styles import resolve_art_style
from app.motion_provider import COMFYUI_MODEL_API_URL, generate_motion_clip
from app.storage import ensure_dirs, p_input, save_project_state, save_scenes

BIBLE_TEXT_API_URL = os.getenv("BIBLE_TEXT_API_URL", "https://bible-api.com")
BIBLE_TEXT_TIMEOUT_SECONDS = float(os.getenv("BIBLE_TEXT_TIMEOUT_SECONDS", "30"))
STABLE_DIFFUSION_API_URL = os.getenv(
    "STABLE_DIFFUSION_API_URL",
    "http://100.100.97.30:7861/sdapi/v1/txt2img",
)
STABLE_DIFFUSION_TIMEOUT_SECONDS = float(os.getenv("STABLE_DIFFUSION_TIMEOUT_SECONDS", "600"))
STABLE_DIFFUSION_CHECKPOINT = os.getenv(
    "STABLE_DIFFUSION_CHECKPOINT",
    "Stable-diffusion/absolutereality_v181.safetensors",
)
MEDIASTUDIO_RUNTIME_HOST = os.getenv("MEDIASTUDIO_RUNTIME_HOST", "")
SEXTANT_ORCHESTRATOR_URL = os.getenv("SEXTANT_ORCHESTRATOR_URL", "")

Progress = Callable[[str, float], None]
Log = Callable[[str], None]


def capability_health(*, session=requests) -> dict[str, Any]:
    checks = {
        "image": STABLE_DIFFUSION_API_URL.rsplit("/sdapi/", 1)[0] + "/sdapi/v1/options",
        "motion": f"{COMFYUI_MODEL_API_URL.rstrip('/')}/system_stats",
    }
    result: dict[str, Any] = {}
    for name, url in checks.items():
        try:
            response = session.get(url, timeout=2)
            result[name] = {"ok": response.ok, "status": response.status_code}
        except requests.RequestException as exc:
            result[name] = {"ok": False, "detail": type(exc).__name__}
    result["mediastudio"] = {"ok": True}
    if SEXTANT_ORCHESTRATOR_URL:
        try:
            response = session.get(f"{SEXTANT_ORCHESTRATOR_URL.rstrip('/')}/healthz", timeout=2)
            result["sextant"] = {"ok": response.ok, "status": response.status_code}
        except requests.RequestException as exc:
            result["sextant"] = {"ok": False, "detail": type(exc).__name__}
    else:
        result["sextant"] = {
            "ok": MEDIASTUDIO_RUNTIME_HOST.strip().lower() == "fortress.sextant",
            "owner": "fortress.sextant:mediastudio-video-orchestrator",
        }
    return result


def _safe_reference(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not re.search(r"[A-Za-z]", cleaned) or not re.search(r"\d", cleaned):
        raise ValueError("Passage must include a Bible book and chapter, such as Genesis 3:1-6")
    return cleaned


def fetch_passage(passage: str, translation: str, *, session=requests) -> dict[str, Any]:
    reference = _safe_reference(passage)
    url = f"{BIBLE_TEXT_API_URL.rstrip('/')}/{quote(reference, safe='')}"
    response = session.get(
        url,
        params={"translation": translation.lower()},
        timeout=BIBLE_TEXT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    verses = payload.get("verses") or []
    if not verses:
        raise RuntimeError(f"No verses were returned for {reference}")
    return payload


def _verse_reference(verse: dict[str, Any], fallback: str) -> str:
    book = str(verse.get("book_name") or verse.get("book_id") or "").strip()
    chapter = verse.get("chapter")
    number = verse.get("verse")
    return f"{book} {chapter}:{number}" if book and chapter and number else fallback


def _scene_prompt(reference: str, verse: str, visual_style: str) -> str:
    style = resolve_art_style(visual_style)
    return (
        f"Biblically and historically grounded visual interpretation of {reference}: {verse}. "
        f"Art direction: {style['name']}. {style['prompt']}. "
        "Ancient Near Eastern setting appropriate to the passage, natural human anatomy, "
        "modest composition, expressive but restrained emotion, cinematic 16:9 framing, coherent lighting, "
        "no text, no lettering, no watermark, no modern objects."
    )


def build_storyboard(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    passage_data = fetch_passage(payload["passage"], payload.get("translation") or "kjv")
    canonical = str(passage_data.get("reference") or payload["passage"]).strip()
    scenes = []
    for index, verse in enumerate(passage_data.get("verses") or [], start=1):
        text = re.sub(r"\s+", " ", str(verse.get("text") or "")).strip()
        reference = _verse_reference(verse, canonical)
        scenes.append(
            {
                "title": reference,
                "description": text,
                "VO": text,
                "images": [f"scene_{index:03d}.png"],
                "duration": max(4.0, min(18.0, len(text.split()) / 2.3)),
                "timeline": [
                    {
                        "image": f"scene_{index:03d}.png",
                        "header": reference,
                        "prompt": _scene_prompt(reference, text, payload["visualStyle"]),
                    }
                ],
            }
        )
    return canonical, scenes


def _generate_still(prompt: str, destination: Path, *, session=requests) -> None:
    response = session.post(
        STABLE_DIFFUSION_API_URL,
        json={
            "prompt": prompt,
            "negative_prompt": (
                "text, watermark, logo, modern clothing, modern architecture, deformed anatomy, extra limbs, "
                "duplicate people, face morph, blur, low detail"
            ),
            "width": 1024,
            "height": 576,
            "steps": 24,
            "cfg_scale": 7,
            "sampler_name": "DPM++ 2M Karras",
            "override_settings": {"sd_model_checkpoint": STABLE_DIFFUSION_CHECKPOINT},
            "override_settings_restore_afterwards": True,
        },
        timeout=STABLE_DIFFUSION_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    images = response.json().get("images") or []
    if not images:
        raise RuntimeError("Stable Diffusion returned no image")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(images[0]))


def prepare_bible_project(
    project_id: str,
    payload: dict[str, Any],
    *,
    progress: Progress,
    log: Log,
) -> None:
    progress("SCRIPTURE_LOOKUP", 0.04)
    canonical, scenes = build_storyboard(payload)
    ensure_dirs(project_id)
    image_dir = p_input(project_id) / "images"
    motion_dir = p_input(project_id) / "motion"
    image_dir.mkdir(parents=True, exist_ok=True)
    if payload.get("mode") == "motion":
        motion_dir.mkdir(parents=True, exist_ok=True)

    for index, scene in enumerate(scenes, start=1):
        timeline = scene["timeline"][0]
        still_path = image_dir / scene["images"][0]
        log(f"Generating still {index}/{len(scenes)} for {scene['title']}")
        _generate_still(timeline["prompt"], still_path)
        if payload.get("mode") == "motion":
            clip_name = f"scene_{index:03d}.mp4"
            log(f"Generating motion clip {index}/{len(scenes)} for {scene['title']}")
            generate_motion_clip(
                still_path,
                motion_dir / clip_name,
                prompt=(
                    f"{timeline['prompt']} Locked cinematic shot. Preserve subjects, faces, anatomy, clothing, "
                    "objects, composition, and art style. Add restrained natural motion; keep the camera stable."
                ),
                negative_prompt="scene cut, pan, zoom, jitter, flicker, face morph, anatomy distortion, text, watermark",
            )
            timeline["video"] = clip_name
        progress("MOTION_GENERATION" if payload.get("mode") == "motion" else "IMAGE_GENERATION", 0.05 + (index / len(scenes)) * 0.45)

    spec = {
        "info": {
            "name": f"{canonical} ({payload.get('translation', 'kjv').upper()})",
            "source": "bible-studio",
            "passage": canonical,
            "mode": payload.get("mode", "still"),
        },
        "vid": {
            "voice": payload.get("voice"),
            "lang": payload.get("language", "en-US"),
            "api": payload.get("ttsApi", "voice-gateway"),
        },
        "scenes": scenes,
    }
    save_scenes(project_id, __import__("json").dumps(spec, indent=2), project_name=spec["info"]["name"])
    save_project_state(
        project_id,
        {
            "schemaVersion": 1,
            "workflow": "bible-video",
            "title": spec["info"]["name"],
            "projectId": project_id,
            "passage": canonical,
            "translation": payload.get("translation", "kjv"),
            "mode": payload.get("mode", "still"),
            "visualStyle": payload.get("visualStyle"),
            "voice": payload.get("voice"),
            "language": payload.get("language", "en-US"),
            "ttsApi": payload.get("ttsApi", "voice-gateway"),
            "outputName": payload.get("outputName", "video.mp4"),
            "youtubeTitle": spec["info"]["name"],
            "youtubeDescription": f"A narrated visual presentation of {canonical}.",
            "youtubeTags": f"Bible, Scripture, {canonical.split()[0]}",
            "youtubePrivacy": "private",
        },
        project_name=spec["info"]["name"],
    )
