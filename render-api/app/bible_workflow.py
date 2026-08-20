"""Server-side Bible passage to storyboard media workflow."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.art_styles import resolve_art_style
from app.motion_provider import COMFYUI_MODEL_API_URL, extract_last_frame, generate_motion_clip
from app.storage import ensure_dirs, p_input, read_project_state, save_project_state, save_scenes

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
OLLAMA_PROMPT_MODEL = os.getenv("OLLAMA_PROMPT_MODEL", "mistral:latest")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
BIBLE_CHANNEL_NAME = os.getenv("BIBLE_CHANNEL_NAME", "Animals")
BIBLE_CHANNEL_ID = os.getenv("BIBLE_CHANNEL_ID", "UCU1T3KZjLceczyfHr2aqpeQ")
BIBLE_CHANNEL_ICON = os.getenv("BIBLE_CHANNEL_ICON", "animal-safari-kids.png")
BIBLE_CAPTION_STYLE = {
    "fontFamily": "EB Garamond",
    "fontSize": 48,
    "fill": "#ffffff",
    "outline": "#000000",
    "position": "bottom-left",
}
GOD_CHARACTER_DESIGN = {
    "id": "god-masculine-elder",
    "version": 2,
    "locked": True,
    "gender": "masculine",
    "age": "mature-to-elderly",
    "summary": "Scenery-first; if God is shown, masculine and mature, never feminine, young, or duplicated",
    "positiveAnchor": (
        "When God is visibly represented, He is always the same unmistakably masculine, mature-to-elderly "
        "adult male divine figure: a mature weathered masculine face, broad brow, deep-set eyes, long "
        "silver-white hair, full silver-white beard, dignified strong build, modest flowing ancient Near "
        "Eastern robes, and an authoritative paternal bearing. Preserve the same masculine sex, mature age, "
        "face, hair, beard, build, garments, and divine visual motifs in every appearance. God may be shown "
        "in human form when the passage and art direction call for it."
    ),
    "negativeAnchor": (
        "God as a woman, feminine God, female deity representing God, goddess representing God, feminine face "
        "or body for God, youthful God, young man representing God, adolescent God, boy deity, child God, "
        "clean-shaven youthful God, gender change for God, age regression for God, duplicate God figure, "
        "two Gods, twin deity figures, multiple old bearded men representing God"
    ),
}

GOD_CHARACTER_ANCHOR = GOD_CHARACTER_DESIGN["positiveAnchor"]
GOD_CHARACTER_NEGATIVE = GOD_CHARACTER_DESIGN["negativeAnchor"]

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


def _god_portrayal_instruction(reference: str, verse: str) -> str:
    context = f"{reference} {verse}".lower()
    if re.match(r"^genesis\s+1(?::|\b)", reference.strip(), flags=re.IGNORECASE):
        return (
            "Genesis 1 scenery-first composition: make the physical creation event described by this verse the clear "
            "subject—cosmic space, light, waters, sky, land, vegetation, celestial bodies, animals, or the humans named "
            "in the verse. Do not depict God as a human figure in this creation scene. Convey divine agency through the "
            "visible transformation, ordered movement, light, scale, wind, and atmosphere. No portrait of elderly men, "
            "no pair of men, and no duplicated divine figure."
        )
    visibly_embodied = any(
        cue in context
        for cue in (
            "appeared unto", "appeared to", "the lord appeared", "stood before", "walked in", "ancient of days",
            "saw the lord", "face to face", "upon the throne", "seated on the throne",
        )
    )
    if visibly_embodied:
        return (
            f"A visible depiction of God is supported in this scene. Show exactly one divine figure, never a pair or "
            f"duplicate. {GOD_CHARACTER_ANCHOR}"
        )
    return (
        "Make the verse's place, event, people, and visible action the primary subject. Do not add a human figure merely "
        "because the text names God. If a visible depiction of God is artistically appropriate, show exactly one figure, "
        f"never a pair or duplicate, and apply this design: {GOD_CHARACTER_ANCHOR}"
    )


def _scene_negative_prompt(reference: str) -> str:
    negative = "duplicate deity, two Gods, twin divine figures, multiple old bearded men, repeated character portrait"
    genesis_one = re.match(r"^genesis\s+1(?::(\d+))?\b", reference.strip(), flags=re.IGNORECASE)
    if genesis_one:
        negative += ", anthropomorphic God, human deity, portrait of God, elderly deity, two elderly men"
        verse_number = int(genesis_one.group(1) or 0)
        if not verse_number or verse_number <= 25 or verse_number == 30:
            negative += (
                ", person, people, man, woman, male figure, female figure, human, humanoid, face, portrait, "
                "robed figure, angel, goddess, deity, crowd, pair of figures, architecture, columns, arches, temple, church"
            )
    return negative


def _scene_prompt(reference: str, verse: str, visual_style: str, opening_state: str = "") -> str:
    style = resolve_art_style(visual_style)
    opening = f" Opening frame: {opening_state}." if opening_state else ""
    is_genesis_one = bool(re.match(r"^genesis\s+1(?::|\b)", reference.strip(), flags=re.IGNORECASE))
    setting_policy = (
        "Pure creation-era cosmic or natural scenery with no civilization. For verses before humanity is created, "
        "show no person, face, humanoid, angel, robed figure, deity portrait, architecture, columns, arches, or buildings. "
        "Use the selected art style only for palette, gold accents, geometry, texture, and brushwork; ignore any style "
        "defaults that call for human or symbolic figures."
        if is_genesis_one
        else "Ancient Near Eastern setting appropriate to the passage, natural human anatomy."
    )
    return (
        f"Biblically and historically grounded visual interpretation of {reference}: {verse}. "
        f"{opening} "
        f"Art direction: {style['name']}. {style['prompt']}. "
        f"Composition policy: {_god_portrayal_instruction(reference, verse)} "
        f"{setting_policy} "
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
        "Composition policy for the entire plan: "
        f"{_god_portrayal_instruction(canonical, ' '.join(verse['text'] for verse in verses))} "
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
        if item["continuity"].lower() in {"none", "n/a", "not applicable"}:
            item["continuity"] = (
                "Preserve the same palette, light direction, geography, spatial composition, and evolving forms "
                "from the opening frame through the ending frame"
            )
        if normalized:
            item["startState"] = normalized[-1]["endState"]
            if str(scene.get("continuity") or "").strip().lower() in {"none", "n/a", "not applicable"}:
                item["continuity"] += f"; carry forward {normalized[-1]['continuity']}"
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


def scene_animation_context(project_id: str, scene_index: int) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
    scenes_path = p_input(project_id) / "scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Project {project_id} has no scenes.json")
    try:
        document = json.loads(scenes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Project {project_id} has invalid scene data") from exc

    scenes = document.get("scenes") or []
    if scene_index < 1 or scene_index > len(scenes):
        raise IndexError(f"Scene {scene_index} does not exist")
    scene = scenes[scene_index - 1]
    image_name = str((scene.get("images") or [""])[0]).strip()
    if not image_name:
        raise ValueError(f"Scene {scene_index} has no still image")
    still_path = p_input(project_id) / "images" / Path(image_name).name
    if not still_path.exists():
        raise FileNotFoundError(f"Scene {scene_index} still image is missing: {image_name}")
    clip_name = f"scene_{scene_index:03d}.mp4"
    clip_path = p_input(project_id) / "motion" / clip_name
    return document, scene, still_path, clip_path


def _safe_fallback_animation_prompt(scene: dict[str, Any]) -> str:
    context = " ".join(
        str(value or "")
        for value in (
            scene.get("VO"),
            scene.get("description"),
            ((scene.get("timeline") or [{}])[0]).get("prompt"),
        )
    ).lower()
    actions = []
    if any(term in context for term in ("water", "sea", "river", "ocean")):
        actions.append("Existing water ripples outward and its reflections travel continuously across the surface")
    if any(term in context for term in ("light", "sun", "day", "heaven", "created", "beginning")):
        actions.append("available light advances gradually across the existing landscape")
    if any(term in context for term in ("plant", "tree", "grass", "herb", "flower", "vine")):
        actions.append("existing leaves and stems respond naturally to a steady breeze")
    if any(term in context for term in ("animal", "bird", "fish", "creature", "cattle")):
        actions.append("the existing creatures continue one restrained natural movement")
    if any(term in context for term in ("man", "woman", "people", "person", "adam", "eve")):
        actions.append("the existing people breathe, shift their weight, and direct their gaze toward the visible action")
    if not actions:
        actions.append("the existing subjects and environmental light develop through one restrained natural action")
    title = re.sub(r"\s+", " ", str(scene.get("title") or "This scene")).strip()
    verse = re.sub(r"\s+", " ", str(scene.get("VO") or scene.get("description") or "")).strip()
    return (
        f"{title}: animate the specific scripture beat, {verse} {actions[0]}. "
        "Use the remaining visible environmental motion only where it already exists in the still. "
        "The camera makes a slow, steady forward move with gentle parallax, keeping every "
        "visible subject, garment, face, structure, decorative element, palette, and light direction consistent. Motion "
        "continues throughout the five-second shot and settles into a clear final composition that can flow directly into "
        "the following scene without introducing anything new. "
        f"Composition policy: {_god_portrayal_instruction(title, verse)}"
    )


def _scene_animation_writer_prompt(
    document: dict[str, Any],
    scene: dict[str, Any],
    scene_index: int,
    visual_style: str,
) -> str:
    scenes = document.get("scenes") or []
    previous_scene = scenes[scene_index - 2] if scene_index > 1 else {}
    next_scene = scenes[scene_index] if scene_index < len(scenes) else {}

    def value(item: dict[str, Any], key: str) -> str:
        return re.sub(r"\s+", " ", str(item.get(key) or "")).strip()

    timeline = scene.get("timeline") or [{}]
    still_description = re.sub(r"\s+", " ", str(timeline[0].get("prompt") or "")).strip()
    still_description = still_description.split("Locked God character design:", 1)[0].strip()
    existing_prompt = value(scene, "motionPrompt").split("Locked God character design:", 1)[0].strip()
    return (
        "Write one production-ready image-to-video animation prompt for exactly the current Bible scene below. "
        "Make this scene unmistakably different from adjacent scenes. Ground the action in this verse and in objects or "
        "people already visible in the still; do not reuse generic water, breeze, lighting, or camera language unless the "
        "current verse and still specifically support it. Describe a concrete opening state, one continuous visible action "
        "with purposeful subject movement, a specific camera move, and an ending state that can flow into the next scene. "
        "Preserve faces, bodies, garments, architecture, palette, composition, and light direction. Do not add new people "
        "or objects, cut to another shot, morph anatomy, or render text. Use 60 to 90 words in one paragraph. Return only "
        "the animation prompt, without a heading, quotation marks, analysis, the scripture text verbatim, or the locked "
        "character-policy wording. The renderer enforces character design separately: do not describe God's age, gender, "
        "hair, beard, face, body, garments, or character-policy traits in this prompt.\n\n"
        f"Passage: {value(document.get('info') or {}, 'passage') or value(document.get('info') or {}, 'name')}\n"
        f"Current scene: {scene_index} of {len(scenes)}\n"
        f"Reference: {value(scene, 'title')}\n"
        f"Verse meaning and event: {value(scene, 'VO') or value(scene, 'description')}\n"
        f"Still-image description: {still_description}\n"
        f"Existing prompt to replace, not copy: {existing_prompt or 'none'}\n"
        f"Planned start: {value(scene, 'startState') or 'infer only from the existing still'}\n"
        f"Planned action: {value(scene, 'action') or 'derive one verse-specific visible action'}\n"
        f"Planned ending: {value(scene, 'endState') or 'settle into a state compatible with the next scene'}\n"
        f"Planned camera: {value(scene, 'camera') or 'choose a scene-specific camera move'}\n"
        f"Continuity requirements: {value(scene, 'continuity') or 'preserve everything visible in the still'}\n"
        f"Planned transition: {value(scene, 'transition') or 'end in visual continuity with the next scene'}\n"
        f"Previous scene ending: {value(previous_scene, 'endState') or value(previous_scene, 'VO') or 'opening scene'}\n"
        f"Next scene event: {value(next_scene, 'VO') or value(next_scene, 'description') or 'final scene'}\n"
        f"Visual style: {resolve_art_style(visual_style)['name']}"
    )


def generate_scene_animation_prompt(project_id: str, scene_index: int, *, session=requests) -> str:
    document, scene, _, _ = scene_animation_context(project_id, scene_index)
    state = read_project_state(project_id) or {}
    visual_style = str(state.get("visualStyle") or (document.get("info") or {}).get("visualStyle") or "cinematic natural light")
    response = session.post(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        json={
            "model": OLLAMA_PROMPT_MODEL,
            "prompt": _scene_animation_writer_prompt(document, scene, scene_index, visual_style),
            "stream": False,
            "options": {"temperature": 0.35, "num_predict": 160},
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    generated = str(
        payload.get("response")
        or ((payload.get("message") or {}).get("content") if isinstance(payload.get("message"), dict) else "")
    ).strip()
    generated = re.sub(r"^```(?:text)?\s*|\s*```$", "", generated, flags=re.IGNORECASE).strip().strip('"')
    generated = re.sub(r"^(?:animation prompt|prompt)\s*:\s*", "", generated, flags=re.IGNORECASE)
    generated = re.sub(r"\s+", " ", generated).strip()
    if not generated:
        raise RuntimeError("Fortress animation prompt writer returned an empty response")
    if generated[-1] not in ".!?":
        complete_sentences = re.match(r"^(.+[.!?])(?:\s+[^.!?]*)?$", generated)
        if complete_sentences:
            generated = complete_sentences.group(1).strip()
    title = re.sub(r"\s+", " ", str(scene.get("title") or f"Scene {scene_index}")).strip()
    return f"Scene {scene_index} — {title}. {generated}"


def animate_bible_scene(
    project_id: str,
    scene_index: int,
    prompt: str = "",
    *,
    progress: Progress,
    log: Log,
) -> Path:
    document, scene, still_path, clip_path = scene_animation_context(project_id, scene_index)
    resolved_prompt = re.sub(r"\s+", " ", prompt).strip()
    if not resolved_prompt:
        progress("WRITING_MOTION_PROMPT", 0.12)
        log(f"Generating a continuity-safe animation prompt for scene {scene_index}")
        resolved_prompt = generate_scene_animation_prompt(project_id, scene_index)
    provider_prompt = (
        f"{resolved_prompt} Composition policy: "
        f"{_god_portrayal_instruction(str(scene.get('title') or ''), str(scene.get('VO') or scene.get('description') or ''))}"
    )

    progress("MOTION_GENERATION", 0.25)
    log(f"Animating scene {scene_index} from {still_path.name}")
    generate_motion_clip(
        still_path,
        clip_path,
        prompt=provider_prompt,
        negative_prompt=(
            "static tableau, frozen pose, slideshow, no movement, scene cut, jump cut, jitter, flicker, "
            f"face morph, anatomy distortion, identity change, clothing change, text, watermark, "
            f"{_scene_negative_prompt(str(scene.get('title') or ''))}, {GOD_CHARACTER_NEGATIVE}"
        ),
    )
    timeline = scene.setdefault("timeline", [{"image": still_path.name}])
    if not timeline:
        timeline.append({"image": still_path.name})
    timeline[0]["video"] = clip_path.name
    scene["motionPrompt"] = resolved_prompt
    scene["animationUpdatedAt"] = time.time()
    project_name = str((document.get("info") or {}).get("name") or project_id)
    save_scenes(project_id, json.dumps(document, indent=2), project_name=project_name)

    state = read_project_state(project_id) or {}
    state["hasMotionScenes"] = True
    state["updatedAt"] = time.time()
    save_project_state(project_id, state, project_name=str(state.get("title") or project_name))
    progress("MOTION_READY", 0.95)
    return clip_path


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


def _generate_still(prompt: str, destination: Path, *, negative_extra: str = "", session=requests) -> None:
    negative_prompt = (
        "text, watermark, logo, modern clothing, modern architecture, deformed anatomy, extra limbs, "
        f"duplicate people, face morph, blur, low detail, {GOD_CHARACTER_NEGATIVE}"
    )
    if negative_extra.strip():
        negative_prompt = f"{negative_prompt}, {negative_extra.strip()}"
    response = session.post(
        STABLE_DIFFUSION_API_URL,
        json={
            "prompt": prompt,
            "negative_prompt": negative_prompt,
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


def _title_card_prompt(canonical: str, scenes: list[dict[str, Any]], visual_style: str) -> str:
    style = resolve_art_style(visual_style)
    subject = " ".join(str(scene.get("VO") or scene.get("description") or "") for scene in scenes[:6])
    subject = re.sub(r"\s+", " ", subject).strip()[:1200]
    if re.match(r"^genesis\s+1(?:\D|$)", canonical.strip(), flags=re.IGNORECASE):
        visual_subject = (
            "Primordial creation before human civilization: vast dark waters beneath a deep celestial expanse, "
            "the first warm radiance separating light from darkness, newly forming land and vegetation only at the far edges. "
            "There are no people, animals, buildings, towers, churches, castles, cities, boats, or constructed objects."
        )
    else:
        visual_subject = f"Use only people, places, objects, and events directly supported by this passage: {subject}"
    style_requirement = ""
    if visual_style == "medieval-illuminated-manuscript":
        style_requirement = (
            "The result must unmistakably look like a medieval illuminated manuscript: flat painted perspective, "
            "gold-leaf accents, jewel pigments, and botanical marginalia around the perimeter, with no architecture. "
        )
    return (
        f"Create a dedicated 16:9 Bible video title-card background for {canonical}. "
        f"Primary visual subject: {visual_subject} Art direction: {style['name']}. {style['prompt']}. "
        f"{style_requirement}"
        f"Composition policy: {_god_portrayal_instruction(canonical, subject)} "
        "Compose a reverent, visually specific interpretation of the passage with its principal subject and setting. "
        "Keep the middle third open, calm, and lower contrast for a separately rendered title; place meaningful imagery around the perimeter. "
        "No words, letters, captions, logos, brokerage branding, real-estate marks, branded frames, badges, watermarks, or modern objects."
    )


def _title_card_negative_prompt(canonical: str, visual_style: str = "") -> str:
    if re.match(r"^genesis\s+1(?:\D|$)", canonical.strip(), flags=re.IGNORECASE):
        negative = (
            "church, cathedral, chapel, castle, tower, house, building, city, village, bridge, boat, ship, road, "
            "person, people, human, animal, central monument, busy center"
        )
        if visual_style != "medieval-illuminated-manuscript":
            negative += ", decorative title frame"
        return negative
    return "brokerage logo, real estate sign, decorative title frame, busy center"


def generate_bible_title_card(
    project_id: str,
    visual_style: str | None = None,
    *,
    progress: Progress,
    log: Log,
) -> Path:
    scenes_path = p_input(project_id) / "scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Project {project_id} has no scenes.json")
    document = json.loads(scenes_path.read_text(encoding="utf-8"))
    scenes = document.get("scenes") or []
    if not scenes:
        raise ValueError(f"Project {project_id} has no Bible scenes")
    state = read_project_state(project_id) or {}
    canonical = str(state.get("passage") or (document.get("info") or {}).get("passage") or state.get("title") or project_id)
    visual_style = str(visual_style or state.get("visualStyle") or "cinematic-natural-light")
    destination = p_input(project_id) / "leader" / "bible-title-card.png"
    progress("TITLE_CARD_GENERATION", 0.2)
    log(f"Generating passage-specific {visual_style} title card for {canonical}")
    _generate_still(
        _title_card_prompt(canonical, scenes, visual_style),
        destination,
        negative_extra=_title_card_negative_prompt(canonical, visual_style),
    )
    state["titleCardImageName"] = destination.name
    state["titleCardUpdatedAt"] = time.time()
    state["visualStyle"] = visual_style
    state["characterDesign"] = {"god": dict(GOD_CHARACTER_DESIGN)}
    render_options = dict(state.get("renderOptions") or {})
    render_options.update({
        "introEnabled": True,
        "introTitle": canonical,
        "introBackgroundImage": destination.name,
        "introLeaderEnabled": False,
        "thumbnailEnabled": True,
        "logoEnabled": True,
        "logoImage": BIBLE_CHANNEL_ICON,
        "logoCorner": "bottom-right",
        "logoMargin": 28,
        "scriptureCaptionEnabled": True,
        "titleStyle": dict(render_options.get("titleStyle") or BIBLE_CAPTION_STYLE),
    })
    state["renderOptions"] = render_options
    save_project_state(project_id, state, project_name=str(state.get("title") or canonical))
    progress("TITLE_CARD_READY", 0.95)
    return destination


def regenerate_bible_scene_stills(
    project_id: str,
    scene_indexes: list[int] | None = None,
    *,
    progress: Progress,
    log: Log,
) -> Path:
    scenes_path = p_input(project_id) / "scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Project {project_id} has no scenes.json")
    document = json.loads(scenes_path.read_text(encoding="utf-8"))
    scenes = document.get("scenes") or []
    if not scenes:
        raise ValueError(f"Project {project_id} has no Bible scenes")
    indexes = scene_indexes or list(range(1, len(scenes) + 1))
    if any(index < 1 or index > len(scenes) for index in indexes):
        raise IndexError("One or more Bible scene indexes do not exist")

    state = read_project_state(project_id) or {}
    visual_style = str(state.get("visualStyle") or (document.get("info") or {}).get("visualStyle") or "cinematic-natural-light")
    last_path: Path | None = None
    for position, scene_index in enumerate(indexes, start=1):
        scene = scenes[scene_index - 1]
        reference = str(scene.get("title") or f"Scene {scene_index}")
        verse = str(scene.get("VO") or scene.get("description") or "")
        timeline = scene.setdefault("timeline", [{}])
        if not timeline:
            timeline.append({})
        prompt = _scene_prompt(reference, verse, visual_style, str(scene.get("startState") or ""))
        image_name = str((scene.get("images") or [f"scene_{scene_index:03d}.png"])[0])
        destination = p_input(project_id) / "images" / Path(image_name).name
        if destination.exists():
            backup_dir = destination.parent / "history"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_name = f"{destination.stem}-{int(time.time())}{destination.suffix}"
            shutil.copy2(destination, backup_dir / backup_name)
            scene.setdefault("imageHistory", []).append(f"history/{backup_name}")
        log(f"Regenerating scenery-first still {position}/{len(indexes)} for {reference}")
        _generate_still(prompt, destination, negative_extra=_scene_negative_prompt(reference))
        timeline[0]["image"] = destination.name
        timeline[0]["prompt"] = prompt
        timeline[0].pop("video", None)
        scene["imageUpdatedAt"] = time.time()
        last_path = destination
        progress("IMAGE_REGENERATION", 0.05 + (position / len(indexes)) * 0.9)

    project_name = str((document.get("info") or {}).get("name") or state.get("title") or project_id)
    save_scenes(project_id, json.dumps(document, indent=2), project_name=project_name)
    state["hasMotionScenes"] = any(
        bool(((scene.get("timeline") or [{}])[0]).get("video")) for scene in scenes
    )
    state["characterDesign"] = {"god": dict(GOD_CHARACTER_DESIGN)}
    state["updatedAt"] = time.time()
    save_project_state(project_id, state, project_name=project_name)
    return last_path or scenes_path


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
    leader_dir = p_input(project_id) / "leader"
    motion_dir = p_input(project_id) / "motion"
    image_dir.mkdir(parents=True, exist_ok=True)
    leader_dir.mkdir(parents=True, exist_ok=True)
    if payload.get("mode") == "motion":
        motion_dir.mkdir(parents=True, exist_ok=True)

    title_card_name = "bible-title-card.png"
    title_card_path = leader_dir / title_card_name
    log(f"Generating passage-specific title card for {canonical}")
    _generate_still(
        _title_card_prompt(canonical, scenes, str(payload.get("visualStyle") or "cinematic-natural-light")),
        title_card_path,
        negative_extra=_title_card_negative_prompt(canonical, str(payload.get("visualStyle") or "cinematic-natural-light")),
    )
    render_options = payload.setdefault("renderOptions", {})
    render_options.update({
        "introEnabled": True,
        "introTitle": canonical,
        "introBackgroundImage": title_card_name,
        "introLeaderEnabled": False,
        "thumbnailEnabled": True,
        "logoEnabled": True,
        "logoImage": BIBLE_CHANNEL_ICON,
        "logoCorner": "bottom-right",
        "logoMargin": 28,
        "scriptureCaptionEnabled": True,
        "titleStyle": dict(render_options.get("titleStyle") or BIBLE_CAPTION_STYLE),
    })

    previous_motion_path: Path | None = None
    for index, scene in enumerate(scenes, start=1):
        timeline = scene["timeline"][0]
        still_path = image_dir / scene["images"][0]
        if payload.get("mode") == "motion" and previous_motion_path is not None:
            log(f"Carrying final frame from scene {index - 1} into scene {index}")
            extract_last_frame(previous_motion_path, still_path)
        else:
            log(f"Generating still {index}/{len(scenes)} for {scene['title']}")
            _generate_still(
                timeline["prompt"],
                still_path,
                negative_extra=_scene_negative_prompt(str(scene.get("title") or "")),
            )
        if payload.get("mode") == "motion":
            clip_name = f"scene_{index:03d}.mp4"
            clip_path = motion_dir / clip_name
            log(f"Generating motion clip {index}/{len(scenes)} for {scene['title']}")
            generate_motion_clip(
                still_path,
                clip_path,
                prompt=(
                    f"{scene['motionPrompt']} Composition policy: "
                    f"{_god_portrayal_instruction(str(scene.get('title') or ''), str(scene.get('VO') or ''))}"
                ),
                negative_prompt=(
                    "static tableau, frozen pose, slideshow, no movement, scene cut, jump cut, jitter, flicker, "
                    f"face morph, anatomy distortion, identity change, clothing change, text, watermark, "
                    f"{_scene_negative_prompt(str(scene.get('title') or ''))}, {GOD_CHARACTER_NEGATIVE}"
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
            "characterDesignVersion": GOD_CHARACTER_DESIGN["version"],
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
            "youtubeProfile": "animals",
            "youtubeChannelName": BIBLE_CHANNEL_NAME,
            "youtubeChannelId": BIBLE_CHANNEL_ID,
            "titleCardImageName": title_card_name,
            "characterDesign": {"god": dict(GOD_CHARACTER_DESIGN)},
            "renderOptions": render_options,
        },
        project_name=spec["info"]["name"],
    )
