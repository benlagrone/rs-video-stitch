"""Server-side Bible passage to storyboard media workflow."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.art_styles import resolve_art_style
from app.motion_provider import COMFYUI_MODEL_API_URL, extract_last_frame, generate_motion_clip
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
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://fortress.lan:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mixtral:latest")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))

Progress = Callable[[str, float], None]
Log = Callable[[str], None]


def capability_health(*, session=requests) -> dict[str, Any]:
    checks = {
        "image": STABLE_DIFFUSION_API_URL.rsplit("/sdapi/", 1)[0] + "/sdapi/v1/options",
        "motion": f"{COMFYUI_MODEL_API_URL.rstrip('/')}/system_stats",
        "planning": f"{OLLAMA_BASE_URL.rstrip('/')}/api/tags",
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


def _scene_prompt(reference: str, verse: str, visual_style: str, opening_state: str = "") -> str:
    style = resolve_art_style(visual_style)
    opening = f" Opening frame: {opening_state}." if opening_state else ""
    return (
        f"Biblically and historically grounded visual interpretation of {reference}: {verse}. "
        f"{opening} "
        f"Art direction: {style['name']}. {style['prompt']}. "
        "Ancient Near Eastern setting appropriate to the passage, natural human anatomy, "
        "modest composition, expressive but restrained emotion, cinematic 16:9 framing, coherent lighting, "
        "no text, no lettering, no watermark, no modern objects."
    )


def _motion_plan_prompt(canonical: str, verses: list[dict[str, str]], visual_style: str) -> str:
    numbered_verses = "\n".join(
        f"{index}. {verse['reference']} — {verse['text']}"
        for index, verse in enumerate(verses, start=1)
    )
    return (
        "You are the continuity director for one uninterrupted, cinematic Bible sequence. "
        "Plan exactly one shot per supplied verse. Every shot must contain a concrete, visible action, not a pose, "
        "tableau, mood, symbol, or generic camera drift. The endState of shot N must be the literal startState of "
        "shot N+1. Keep recurring people, faces, age, clothing, geography, architecture, light direction, weather, "
        "props, and screen direction consistent unless the scripture requires a visible transformation. "
        "When time or place changes, describe an on-camera transition that carries the viewer into the new state. "
        "Do not alter, summarize, or add to the scripture. Avoid text, lettering, modern objects, scene cuts inside a shot, "
        "and abstract theological imagery. Make each action achievable in about five seconds.\n\n"
        f"Passage: {canonical}\nVisual style: {visual_style}\nVerses:\n{numbered_verses}\n\n"
        "Return only valid JSON in this exact shape: "
        '{"scenes":[{"reference":"...","startState":"...","action":"...","endState":"...",'
        '"camera":"...","continuity":"...","transition":"..."}]}. '
        f"Return exactly {len(verses)} scene objects in the supplied order. Every field must be a specific visual description."
    )


def _parse_motion_plan(raw: str, expected_count: int) -> list[dict[str, str]]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Fortress motion planner returned invalid JSON: {exc}") from exc
    scenes = payload.get("scenes") if isinstance(payload, dict) else None
    if not isinstance(scenes, list) or len(scenes) != expected_count:
        raise RuntimeError(f"Fortress motion planner must return exactly {expected_count} scenes")
    required = ("startState", "action", "endState", "camera", "continuity", "transition")
    normalized = []
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Fortress motion planner scene {index} is not an object")
        item = {key: re.sub(r"\s+", " ", str(scene.get(key) or "")).strip() for key in required}
        missing = [key for key, value in item.items() if not value]
        if missing:
            raise RuntimeError(f"Fortress motion planner scene {index} is missing {', '.join(missing)}")
        if normalized:
            item["startState"] = normalized[-1]["endState"]
        normalized.append(item)
    return normalized


def plan_motion_sequence(
    canonical: str,
    verses: list[dict[str, str]],
    visual_style: str,
    *,
    session=requests,
) -> list[dict[str, str]]:
    response = session.post(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": _motion_plan_prompt(canonical, verses, visual_style),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.25},
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    raw = str(payload.get("response") or ((payload.get("message") or {}).get("content") if isinstance(payload.get("message"), dict) else "")).strip()
    if not raw:
        raise RuntimeError("Fortress motion planner returned an empty response")
    return _parse_motion_plan(raw, len(verses))


def _motion_prompt(scene: dict[str, Any], visual_style: str) -> str:
    style = resolve_art_style(visual_style)
    return (
        f"One continuous cinematic shot in {style['name']} style. Begin exactly with: {scene['startState']}. "
        f"The visible action is: {scene['action']}. End exactly with: {scene['endState']}. "
        f"Camera movement: {scene['camera']}. Preserve throughout: {scene['continuity']}. "
        f"Connection to the next shot: {scene['transition']}. {style['prompt']}. "
        "Natural body mechanics, purposeful movement throughout the shot, coherent lighting, no cuts."
    )


def build_storyboard(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    passage_data = fetch_passage(payload["passage"], payload.get("translation") or "kjv")
    canonical = str(passage_data.get("reference") or payload["passage"]).strip()
    verses = []
    for verse in passage_data.get("verses") or []:
        text = re.sub(r"\s+", " ", str(verse.get("text") or "")).strip()
        verses.append({"reference": _verse_reference(verse, canonical), "text": text})
    motion_plan = (
        plan_motion_sequence(canonical, verses, payload["visualStyle"])
        if payload.get("mode") == "motion"
        else []
    )
    scenes = []
    for index, verse in enumerate(verses, start=1):
        reference = verse["reference"]
        text = verse["text"]
        plan = motion_plan[index - 1] if motion_plan else {}
        scene = {
            "title": reference,
            "description": text,
            "VO": text,
            "images": [f"scene_{index:03d}.png"],
            "duration": max(4.0, min(18.0, len(text.split()) / 2.3)),
            "timeline": [
                {
                    "image": f"scene_{index:03d}.png",
                    "header": reference,
                    "prompt": _scene_prompt(reference, text, payload["visualStyle"], plan.get("startState", "")),
                }
            ],
        }
        if plan:
            scene.update(plan)
            scene["motionPrompt"] = _motion_prompt(scene, payload["visualStyle"])
        scenes.append(scene)
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

    previous_motion_path: Path | None = None
    for index, scene in enumerate(scenes, start=1):
        timeline = scene["timeline"][0]
        still_path = image_dir / scene["images"][0]
        if payload.get("mode") == "motion" and previous_motion_path is not None:
            log(f"Carrying final frame from scene {index - 1} into scene {index}")
            extract_last_frame(previous_motion_path, still_path)
        else:
            log(f"Generating still {index}/{len(scenes)} for {scene['title']}")
            _generate_still(timeline["prompt"], still_path)
        if payload.get("mode") == "motion":
            clip_name = f"scene_{index:03d}.mp4"
            clip_path = motion_dir / clip_name
            log(f"Generating motion clip {index}/{len(scenes)} for {scene['title']}")
            generate_motion_clip(
                still_path,
                clip_path,
                prompt=scene["motionPrompt"],
                negative_prompt=(
                    "static tableau, frozen pose, slideshow, no movement, scene cut, jump cut, jitter, flicker, "
                    "face morph, anatomy distortion, identity change, clothing change, text, watermark"
                ),
            )
            timeline["video"] = clip_name
            previous_motion_path = clip_path
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
