"""Server-side Bible passage to storyboard media workflow."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.art_styles import resolve_art_style
from app.motion_provider import COMFYUI_MODEL_API_URL, extract_last_frame, generate_motion_clip, ltx_local_status
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
DEFAULT_THEME_INTERPRETATION = (
    "Follow the scripture text closely. Make each visible subject, action, setting, and transition arise from the "
    "passage itself; do not impose an unrelated allegory, era, plot, or character role."
)

Progress = Callable[[str, float], None]
Log = Callable[[str], None]


def _theme_instruction(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned or DEFAULT_THEME_INTERPRETATION


def _theme_prompt(value: str | None) -> str:
    return (
        f"Theme and interpretation direction: {_theme_instruction(value)} "
        "Mix this direction with the selected art style and scripture. It may refine mood, symbolism, setting, "
        "recurring visual motifs, costume, and permitted portrayal details, but it must not contradict the supplied "
        "scripture or override locked portrayal constraints."
    )


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
    result["motionLocal"] = ltx_local_status(session=session)
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
        verse_number = int(genesis_one.group(1) or 0)
        if verse_number in {26, 27, 28, 29, 31}:
            return (
                "deity figure, divine portrait, old man, elderly person, gray hair, white beard, same-sex pair, "
                "identical twins, duplicate person, third figure, extra figure, crowd, child, wedding, bride, groom, "
                "formal gown, modern dress, architecture, building, house, palace, church, temple, arches, columns, "
                "city, village, road, paved path"
            )
        negative += (
            ", anthropomorphic God, human deity, portrait of God, elderly deity, two elderly men, architecture, "
            "building, house, palace, church, temple, arches, columns, city, village, road, paved path"
        )
        if not verse_number or verse_number <= 25 or verse_number == 30:
            negative += (
                ", person, people, man, woman, male figure, female figure, human, humanoid, face, portrait, "
                "robed figure, angel, goddess, deity, crowd, pair of figures"
            )
    return negative


def _genesis_one_visual_subject(reference: str) -> str:
    """Describe only what should be visible, without deity words that provoke portraits."""
    match = re.match(r"^genesis\s+1(?::(\d+))?\b", reference.strip(), flags=re.IGNORECASE)
    verse_number = int(match.group(1) or 0) if match else 0
    subjects = {
        1: "A vast primordial cosmos and newly forming earth beneath immense heavens",
        2: "A formless dark ocean under a deep empty sky, with wind tracing broad ripples across the water",
        3: "The first radiant light breaking across primordial darkness and illuminating the ocean",
        4: "A sharp boundary forming between luminous day and deep darkness across the same horizon",
        5: "The first complete transition from glowing evening into bright morning over the young earth",
        6: "A broad vault of sky opening between lower seas and immense waters suspended above",
        7: "The waters separating into a calm ocean below and luminous cloud-borne waters above",
        8: "The newly ordered sky stretching from horizon to horizon above the primordial sea",
        9: "Ocean waters drawing together while the first dry ridges rise visibly from beneath them",
        10: "Newly exposed earth and gathered seas settling into distinct coastlines",
        11: "Fresh grass, seed-bearing herbs, and fruit trees rapidly spreading across bare land",
        12: "A flourishing landscape of mature grasses, herbs, and fruit trees heavy with seed and fruit",
        13: "The planted earth resting through evening and awakening into a green third morning",
        14: "Sun, moon, and stars taking ordered positions in the vast sky above the earth",
        15: "Celestial lights casting their first organized illumination across land and sea",
        16: "The brilliant sun ruling the day while the moon and stars govern the night sky",
        17: "Sun, moon, and constellations fixed in a harmonious celestial expanse above the world",
        18: "Daylight and night dividing cleanly as celestial lights follow their appointed courses",
        19: "A richly colored evening yielding to the clear dawn of the fourth morning",
        20: "Schools of fish filling clear seas while great flocks of birds sweep across the open sky",
        21: "Great sea creatures moving through deep water among abundant fish, with birds above",
        22: "Sea life multiplying through the waters and bird flocks expanding across the sky",
        23: "Birds settling at evening above teeming seas before the fifth morning",
        24: "Wild animals, livestock, and small ground creatures emerging across varied habitats",
        25: "A balanced living landscape populated by distinct wild animals, livestock, and ground creatures",
        26: (
            "Exactly two full-body people outdoors, Adam and Eve: (one visibly masculine young adult man:1.4) with "
            "short dark hair and (one visibly feminine young adult woman:1.4) with long dark hair, both in simple "
            "ancient undyed linen garments, standing separately amid plants and animals"
        ),
        27: (
            "Exactly two full-body people, Adam and Eve: (one clearly masculine young adult man:1.4) and (one clearly "
            "feminine young adult woman:1.4), equal in dignity, standing separately outdoors in a wild garden landscape"
        ),
        28: (
            "Adam and Eve together, exactly (one young adult man:1.4) and (one young adult woman:1.4), beginning their "
            "stewardship outdoors amid fertile land, birds, fish, and animals"
        ),
        29: (
            "Adam and Eve together, exactly (one young adult man:1.4) and (one young adult woman:1.4), gathering from "
            "abundant seed-bearing plants and fruit trees in a wild outdoor landscape"
        ),
        30: "Land animals, birds, and small creatures feeding peacefully among abundant green plants",
        31: (
            "A panoramic view of the complete living world with Adam and Eve, exactly one young adult man and one "
            "young adult woman, visible together as small figures within the vast wild landscape"
        ),
    }
    return subjects.get(verse_number, "The physical creation of the cosmos unfolding through distinct natural forms")


def _scene_prompt(
    reference: str,
    verse: str,
    visual_style: str,
    opening_state: str = "",
    theme_interpretation: str = "",
) -> str:
    style = resolve_art_style(visual_style)
    opening = f" Opening frame: {opening_state}." if opening_state else ""
    is_genesis_one = bool(re.match(r"^genesis\s+1(?::|\b)", reference.strip(), flags=re.IGNORECASE))
    style_name = str(style["name"])
    if is_genesis_one and re.search(r"\b(iconography|portraiture|character)\b", style_name, flags=re.IGNORECASE):
        style_name = re.sub(r"\bIconography\b", "inspired visual treatment", style_name, flags=re.IGNORECASE)
        style_name = re.sub(r"\bPortraiture\b", "inspired visual treatment", style_name, flags=re.IGNORECASE)
        style_name = re.sub(r"\bCharacter\b", "visual", style_name, flags=re.IGNORECASE)
    style_direction = str(style["prompt"])
    if is_genesis_one and re.search(r"\b(figures?|portraits?|characters?|saints?|icons?)\b", style_direction, flags=re.IGNORECASE):
        style_direction = (
            f"Use the {style_name} palette, flat spatial design, gold-leaf surface treatment, linework, and "
            "intricate geometric patterning on cosmic and natural forms only"
        )
    if is_genesis_one:
        return (
            f"Environment-led visual interpretation of {reference}. "
            f"Primary visible subject and action: {_genesis_one_visual_subject(reference)}."
            f"{opening} "
            f"Art treatment: {style_name}. {style_direction}. "
            f"{_theme_prompt(theme_interpretation)} "
            "Creation-era cosmic and natural setting with no civilization; make the physical transformation, scale, "
            "atmosphere, and living world fill the frame. Modest composition, cinematic 16:9 framing, coherent "
            "lighting, no text, no lettering, no watermark, no modern objects."
        )
    setting_policy = (
        "Ancient Near Eastern setting appropriate to the passage, natural human anatomy."
    )
    return (
        f"Biblically and historically grounded visual interpretation of {reference}: {verse}. "
        f"{opening} "
        f"Art direction: {style['name']}. {style_direction}. "
        f"{_theme_prompt(theme_interpretation)} "
        f"Composition policy: {_god_portrayal_instruction(reference, verse)} "
        f"{setting_policy} "
        "modest composition, expressive but restrained emotion, cinematic 16:9 framing, coherent lighting, "
        "no text, no lettering, no watermark, no modern objects."
    )


def _motion_plan_prompt(
    canonical: str,
    verses: list[dict[str, str]],
    visual_style: str,
    theme_interpretation: str = "",
) -> str:
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
        f"{_theme_prompt(theme_interpretation)} "
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
    theme_interpretation: str = "",
    *,
    session=requests,
) -> list[dict[str, str]]:
    response = session.post(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": _motion_plan_prompt(canonical, verses, visual_style, theme_interpretation),
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


def _motion_prompt(scene: dict[str, Any], visual_style: str, theme_interpretation: str = "") -> str:
    style = resolve_art_style(visual_style)
    return (
        f"One continuous cinematic shot in {style['name']} style. Begin exactly with: {scene['startState']}. "
        f"The visible action is: {scene['action']}. End exactly with: {scene['endState']}. "
        f"Camera movement: {scene['camera']}. Preserve throughout: {scene['continuity']}. "
        f"Connection to the next shot: {scene['transition']}. {style['prompt']}. "
        f"{_theme_prompt(theme_interpretation)} "
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
        actions.append(
            "existing dust and small localized highlights drift and shimmer gently without changing the scene's overall exposure"
        )
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
        "Keep the camera locked to the original composition while subject and environmental motion develop, keeping every "
        "visible subject, garment, face, structure, decorative element, palette, and light direction consistent. Motion "
        "continues throughout the five-second shot and settles into a clear final composition that can flow directly into "
        "the following scene without introducing anything new. "
        f"Composition policy: {_god_portrayal_instruction(title, verse)}"
    )


LOCKED_CAMERA_CONFLICT = re.compile(
    r"\bcamera\s+(?:then\s+)?(?:slowly\s+|smoothly\s+|gracefully\s+|subtly\s+)*"
    r"(?:pans?|glides?|moves?|pushes?|pulls?|advances?|tracks?|dollies?|zooms?|tilts?|orbits?)\b",
    flags=re.IGNORECASE,
)

LOCKED_SCENE_TRANSFORMATION_CONFLICT = re.compile(
    r"\b(?:break(?:s|ing)?\s+apart|reform(?:s|ing|ed)?|newly\s+form(?:s|ing|ed)?|"
    r"transform(?:s|ing|ed|ation)?|fill(?:s|ing|ed)?\s+the\s+(?:entire\s+)?(?:frame|void|scene)|"
    r"ignit(?:e|es|ing|ed)|explod(?:e|es|ing|ed)|global\s+(?:light|color|exposure)\s+(?:change|shift))\b",
    flags=re.IGNORECASE,
)

LOCKED_COMPOSITION_POLICY = (
    " Keep frame edges, crop, scale, horizon, object positions, palette, and global exposure unchanged. "
    "Animate only small localized details already visible in the still. Do not add, remove, split, reform, or transform "
    "objects; do not change the overall lighting or color; do not move the camera."
)


def _enforce_camera_behavior_prompt(prompt: str, scene: dict[str, Any], camera_behavior: str) -> str:
    normalized = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if camera_behavior == "locked":
        if LOCKED_CAMERA_CONFLICT.search(normalized) or LOCKED_SCENE_TRANSFORMATION_CONFLICT.search(normalized):
            normalized = _safe_fallback_animation_prompt(scene)
        if LOCKED_COMPOSITION_POLICY.strip() not in normalized:
            normalized = f"{normalized.rstrip()} {LOCKED_COMPOSITION_POLICY.strip()}"
    return normalized


def _scene_animation_writer_prompt(
    document: dict[str, Any],
    scene: dict[str, Any],
    scene_index: int,
    visual_style: str,
    theme_interpretation: str = "",
    camera_behavior: str = "locked",
) -> str:
    scenes = document.get("scenes") or []
    previous_scene = scenes[scene_index - 2] if scene_index > 1 else {}
    next_scene = scenes[scene_index] if scene_index < len(scenes) else {}

    def value(item: dict[str, Any], key: str) -> str:
        return re.sub(r"\s+", " ", str(item.get(key) or "")).strip()

    timeline = scene.get("timeline") or [{}]
    image_generation = timeline[0].get("imageGeneration") or {}
    still_description = re.sub(
        r"\s+", " ", str(image_generation.get("prompt") or timeline[0].get("prompt") or "")
    ).strip()
    still_description = still_description.split("Locked God character design:", 1)[0].strip()
    existing_prompt = value(scene, "motionPrompt").split("Locked God character design:", 1)[0].strip()
    camera_instruction = {
        "locked": (
            "LOCKED composition. Do not describe any camera movement. Keep frame edges, crop, scale, horizon, and "
            "composition fixed; all motion must occur within the existing scene. Ignore any stored planned camera move."
        ),
        "slow-push": "One smooth subtle slow push only, without shake, direction change, or crop jump.",
        "pan-left": "One smooth restrained pan left only, without shake, zoom, or direction change.",
        "pan-right": "One smooth restrained pan right only, without shake, zoom, or direction change.",
    }.get(camera_behavior, "LOCKED composition with no camera movement.")
    return (
        "Write one production-ready image-to-video animation prompt for exactly the current Bible scene below. "
        "Make this scene unmistakably different from adjacent scenes. Ground the action in this verse and in objects or "
        "people already visible in the still; do not reuse generic water, breeze, lighting, or camera language unless the "
        "current verse and still specifically support it. Describe a concrete opening state, one continuous visible action "
        "with purposeful subject movement, and an ending state that can flow into the next scene. Obey the selected camera "
        "behavior exactly; never invent handheld shake, zoom, crop, reframing, or a second camera move. "
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
        f"Selected camera behavior: {camera_instruction}\n"
        f"Continuity requirements: {value(scene, 'continuity') or 'preserve everything visible in the still'}\n"
        f"Planned transition: {value(scene, 'transition') or 'end in visual continuity with the next scene'}\n"
        f"Previous scene ending: {value(previous_scene, 'endState') or value(previous_scene, 'VO') or 'opening scene'}\n"
        f"Next scene event: {value(next_scene, 'VO') or value(next_scene, 'description') or 'final scene'}\n"
        f"Visual style: {resolve_art_style(visual_style)['name']}\n"
        f"{_theme_prompt(theme_interpretation)}"
    )


def generate_scene_animation_prompt(
    project_id: str,
    scene_index: int,
    *,
    camera_behavior: str = "locked",
    session=requests,
) -> str:
    document, scene, _, _ = scene_animation_context(project_id, scene_index)
    state = read_project_state(project_id) or {}
    visual_style = str(state.get("visualStyle") or (document.get("info") or {}).get("visualStyle") or "cinematic natural light")
    theme_interpretation = str(
        state.get("themeInterpretation") or (document.get("info") or {}).get("themeInterpretation") or ""
    )
    response = session.post(
        f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        json={
            "model": OLLAMA_PROMPT_MODEL,
            "prompt": _scene_animation_writer_prompt(
                document, scene, scene_index, visual_style, theme_interpretation, camera_behavior
            ),
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
    generated = _enforce_camera_behavior_prompt(generated, scene, camera_behavior)
    title = re.sub(r"\s+", " ", str(scene.get("title") or f"Scene {scene_index}")).strip()
    return f"Scene {scene_index} — {title}. {generated}"


MOTION_REGION_EFFECTS = {"drift", "rise", "surge", "roll", "fracture", "radiate", "pulse"}
MOTION_REGION_DIRECTIONS = {"left", "right", "up", "down", "outward", "clockwise", "counterclockwise", "pulse"}
MOTION_REGION_METHODS = {"object-vector", "generative-region"}
MOTION_VECTOR_EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out"}


def _default_vector(direction: str, strength: float) -> dict[str, float]:
    distance = round(0.05 + (strength * 0.15), 3)
    return {
        "left": {"dx": -distance, "dy": 0.0},
        "right": {"dx": distance, "dy": 0.0},
        "up": {"dx": 0.0, "dy": -distance},
        "down": {"dx": 0.0, "dy": distance},
    }.get(direction, {"dx": 0.0, "dy": 0.0})


def _sanitize_motion_plan(plan: dict[str, Any], *, source: str = "edited") -> dict[str, Any]:
    regions = []
    for position, item in enumerate(plan.get("regions") or []):
        if not isinstance(item, dict) or len(regions) >= 5:
            continue
        raw_box = item.get("box") if isinstance(item.get("box"), dict) else {}
        try:
            x = min(0.95, max(0.0, float(raw_box.get("x", 0.1))))
            y = min(0.95, max(0.0, float(raw_box.get("y", 0.1))))
            width = min(1.0 - x, max(0.08, float(raw_box.get("width", 0.35))))
            height = min(1.0 - y, max(0.08, float(raw_box.get("height", 0.35))))
            strength = min(1.0, max(0.1, float(item.get("strength", 0.55))))
        except (TypeError, ValueError):
            continue
        effect = str(item.get("effect") or "drift").lower()
        direction = str(item.get("direction") or "right").lower()
        method = str(item.get("method") or item.get("renderMode") or "generative-region").lower()
        vector = item.get("vector") if isinstance(item.get("vector"), dict) else {}
        try:
            dx = min(0.5, max(-0.5, float(vector.get("dx", 0.0))))
            dy = min(0.5, max(-0.5, float(vector.get("dy", 0.0))))
        except (TypeError, ValueError):
            dx, dy = 0.0, 0.0
        if method == "object-vector" and dx == 0.0 and dy == 0.0:
            default_vector = _default_vector(direction, strength)
            dx, dy = default_vector["dx"], default_vector["dy"]
        easing = str(item.get("easing") or "ease-in-out").lower()
        regions.append({
            "id": re.sub(r"[^a-z0-9-]+", "-", str(item.get("id") or f"region-{position + 1}").lower()).strip("-")[:40],
            "label": re.sub(r"\s+", " ", str(item.get("label") or f"Region {position + 1}")).strip()[:80],
            "action": re.sub(r"\s+", " ", str(item.get("action") or "moves continuously within the existing scene")).strip()[:240],
            "effect": effect if effect in MOTION_REGION_EFFECTS else "drift",
            "direction": direction if direction in MOTION_REGION_DIRECTIONS else "right",
            "strength": round(strength, 2),
            "method": method if method in MOTION_REGION_METHODS else "generative-region",
            "vector": {"dx": round(dx, 3), "dy": round(dy, 3)},
            "easing": easing if easing in MOTION_VECTOR_EASINGS else "ease-in-out",
            "box": {"x": round(x, 3), "y": round(y, 3), "width": round(width, 3), "height": round(height, 3)},
            "enabled": item.get("enabled") is not False,
        })
    if not regions:
        raise ValueError("A motion plan must contain at least one usable region")
    return {
        "version": 2,
        "source": source,
        "summary": re.sub(r"\s+", " ", str(plan.get("summary") or "Animate selected scene elements while holding the composition fixed.")).strip()[:320],
        "lockedBackground": plan.get("lockedBackground") is not False,
        "allowFullFrameGeneration": plan.get("allowFullFrameGeneration") is True,
        "fallbackMode": "still",
        "regions": regions,
        "updatedAt": time.time(),
    }


def _fallback_motion_plan(
    scene: dict[str, Any],
    scene_index: int,
    motion_instruction: str = "",
) -> dict[str, Any]:
    timeline = (scene.get("timeline") or [{}])[0]
    image_generation = timeline.get("imageGeneration") or {}
    context = " ".join([
        str(scene.get("title") or ""), str(scene.get("VO") or ""), str(scene.get("action") or ""),
        str(image_generation.get("prompt") or timeline.get("prompt") or ""),
        motion_instruction,
    ]).lower()
    explicit_direction = next(
        (direction for direction in ("down", "up", "left", "right") if direction in motion_instruction.lower()),
        "",
    )
    explicit_subject = next(
        (subject for subject in ("planet", "moon", "sun", "boat", "bird", "animal", "figure", "person") if subject in motion_instruction.lower()),
        "",
    )
    if explicit_subject and explicit_direction:
        raw = {
            "summary": f"Move the existing {explicit_subject} {explicit_direction} along one exact vector while the rest of the frame remains fixed.",
            "lockedBackground": True,
            "allowFullFrameGeneration": False,
            "regions": [{
                "id": explicit_subject,
                "label": f"Existing {explicit_subject}",
                "action": motion_instruction,
                "effect": "drift",
                "direction": explicit_direction,
                "strength": 1.0,
                "method": "object-vector",
                "vector": _default_vector(explicit_direction, 1.0),
                "easing": "ease-in-out",
                "box": {"x": 0.04, "y": 0.03, "width": 0.42, "height": 0.52},
            }],
        }
    elif any(word in context for word in ("cataclysm", "planet", "cosmos", "collision", "fractur", "fire", "molten")):
        raw = {
            "summary": "Continue the existing cosmic cataclysm through distinct environmental actions while the planet and frame remain stable.",
            "lockedBackground": True,
            "regions": [
                {"id": "fire-arc", "label": "Existing fire arc", "action": "The existing fiery arc races forward and sheds sparks along its current path.", "effect": "surge", "direction": "clockwise", "strength": 0.82, "box": {"x": 0.64, "y": 0.04, "width": 0.34, "height": 0.92}},
                {"id": "forming-terrain", "label": "Forming terrain", "action": "Existing ridges lift and fracture as glowing seams spread through the ground.", "effect": "fracture", "direction": "up", "strength": 0.55, "box": {"x": 0.18, "y": 0.56, "width": 0.58, "height": 0.36}},
                {"id": "dust-front", "label": "Dust and debris", "action": "Dust and small debris roll outward across the foreground without obscuring the scene.", "effect": "roll", "direction": "left", "strength": 0.46, "box": {"x": 0.04, "y": 0.66, "width": 0.78, "height": 0.28}},
            ],
        }
    elif any(word in context for word in ("water", "sea", "ocean", "river", "wave")):
        raw = {
            "summary": "Move the existing water and atmosphere in separate continuous layers while preserving the horizon.",
            "lockedBackground": True,
            "regions": [
                {"id": "water", "label": "Existing water", "action": "Broad ripples travel across the existing water surface.", "effect": "roll", "direction": "outward", "strength": 0.58, "box": {"x": 0.0, "y": 0.5, "width": 1.0, "height": 0.5}},
                {"id": "atmosphere", "label": "Clouds and mist", "action": "Existing clouds and mist drift steadily across the sky.", "effect": "drift", "direction": "right", "strength": 0.34, "box": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.5}},
            ],
        }
    else:
        raw = {
            "summary": "Animate distinct existing environmental layers with restrained continuous motion and a fixed composition.",
            "lockedBackground": True,
            "regions": [
                {"id": "primary-action", "label": "Primary visible action", "action": str(scene.get("action") or scene.get("VO") or "The primary visible element moves continuously.")[:240], "effect": "surge", "direction": "right", "strength": 0.55, "box": {"x": 0.18, "y": 0.2, "width": 0.64, "height": 0.56}},
                {"id": "atmosphere", "label": "Atmosphere", "action": "Existing atmospheric details drift subtly behind the main action.", "effect": "drift", "direction": "left", "strength": 0.28, "box": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 0.42}},
            ],
        }
    return _sanitize_motion_plan(raw, source="source-prompt+scripture-fallback")


def generate_scene_motion_plan(
    project_id: str,
    scene_index: int,
    motion_instruction: str = "",
    *,
    session=requests,
) -> dict[str, Any]:
    document, scene, _, _ = scene_animation_context(project_id, scene_index)
    state = read_project_state(project_id) or {}
    timeline = (scene.get("timeline") or [{}])[0]
    source_prompt = str((timeline.get("imageGeneration") or {}).get("prompt") or timeline.get("prompt") or "")
    instruction = (
        "Return only valid JSON for an image-to-video motion plan. Use the scripture event and the exact source-image "
        "prompt and requested animation to identify 1 to 5 DISTINCT EXISTING visual regions that should move. Give "
        "normalized 0..1 bounding boxes around the actual subjects. Prefer scenery and physical events over faces or whole "
        "people. Never invent an object absent from the source prompt. Keep the background, horizon, frame edges, scale, "
        "and camera locked. Choose method object-vector for an intact object that translates through the frame, and "
        "generative-region only for deformable water, fire, smoke, light, terrain, or atmosphere. Each region needs id, "
        "label, action, method, effect (drift|rise|surge|roll|fracture|radiate|pulse), "
        "direction (left|right|up|down|outward|clockwise|counterclockwise|pulse), strength 0.1..1, enabled true, and "
        "box {x,y,width,height}. Object-vector regions also need vector {dx,dy} as normalized frame displacement and "
        "easing linear|ease-in|ease-out|ease-in-out. Add a concise summary, lockedBackground true, "
        "allowFullFrameGeneration false, and fallbackMode still. Never choose full-frame generation merely because a "
        "regional method is difficult. JSON shape: "
        "{\"summary\":\"...\",\"lockedBackground\":true,\"allowFullFrameGeneration\":false,\"regions\":[{...}]}.\n\n"
        f"Reference: {scene.get('title')}\nScripture event: {scene.get('VO') or scene.get('description')}\n"
        f"Source-image prompt: {source_prompt}\nTheme: {_theme_instruction(state.get('themeInterpretation'))}\n"
        f"Existing scene action: {scene.get('action') or ''}\nRequested animation: {motion_instruction}"
    )
    try:
        response = session.post(
            f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate",
            json={"model": OLLAMA_PROMPT_MODEL, "prompt": instruction, "stream": False, "format": "json", "options": {"temperature": 0.2, "num_predict": 850}},
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        content = str(payload.get("response") or ((payload.get("message") or {}).get("content") if isinstance(payload.get("message"), dict) else "")).strip()
        plan = _sanitize_motion_plan(json.loads(content), source="source-image-prompt+scripture")
    except (requests.RequestException, json.JSONDecodeError, TypeError, ValueError):
        plan = _fallback_motion_plan(scene, scene_index, motion_instruction)
    scene["motionPlan"] = plan
    save_scenes(project_id, json.dumps(document, indent=2), project_name=str((document.get("info") or {}).get("name") or project_id))
    return plan


def save_scene_motion_plan(project_id: str, scene_index: int, plan: dict[str, Any]) -> dict[str, Any]:
    document, scene, _, _ = scene_animation_context(project_id, scene_index)
    sanitized = _sanitize_motion_plan(plan, source="edited")
    scene["motionPlan"] = sanitized
    save_scenes(project_id, json.dumps(document, indent=2), project_name=str((document.get("info") or {}).get("name") or project_id))
    return sanitized


def _motion_provenance(scene: dict[str, Any], still_path: Path, scene_index: int, motion_prompt: str) -> dict[str, Any]:
    timeline = (scene.get("timeline") or [{}])[0]
    image_generation = timeline.get("imageGeneration") or {}
    image_prompt = re.sub(
        r"\s+", " ", str(image_generation.get("prompt") or timeline.get("prompt") or "")
    ).strip()
    image_negative = re.sub(r"\s+", " ", str(image_generation.get("negativePrompt") or "")).strip()
    image_seed = image_generation.get("seed")
    fingerprint_source = still_path.read_bytes() if still_path.exists() else f"{still_path.name}|{image_prompt}".encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
    motion_attempt = max(1, int(scene.get("motionAttempt") or 1))
    seed_material = f"{image_seed or fingerprint}|{scene_index}|{motion_prompt}|attempt:{motion_attempt}".encode("utf-8")
    motion_seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big") % (2**63 - 1) or 1
    # A visual-style name does not prove that the generated pixels contain a
    # decorative border. Freezing the perimeter of a full-bleed image creates
    # an obvious moving inset rectangle, so frame protection is opt-in metadata
    # set only after the generated image has been explicitly classified.
    protect_style_frame = image_generation.get("decorativeFrameProtection") is True
    return {
        "sourceImagePrompt": image_prompt,
        "sourceImageNegativePrompt": image_negative,
        "sourceImageSeed": image_seed,
        "sourceImageModel": image_generation.get("model") or STABLE_DIFFUSION_CHECKPOINT,
        "sourceImageFingerprint": fingerprint,
        "motionSeed": motion_seed,
        "motionAttempt": motion_attempt,
        "cameraBehavior": "locked",
        "decorativeFrameProtection": {
            "enabled": protect_style_frame,
            "outerWidthPercent": 12,
            "outerHeightPercent": 14,
            "featherPixels": 4,
        },
    }


def _motion_provider_prompt(resolved_prompt: str, scene: dict[str, Any], provenance: dict[str, Any]) -> str:
    reference = str(scene.get("title") or "")
    verse = str(scene.get("VO") or scene.get("description") or "")
    source_prompt = str(provenance.get("sourceImagePrompt") or "").strip()
    visual_anchor = (
        "The supplied source image is the authoritative first frame. Preserve its exact subjects, count, identities, "
        "anatomy, environment, composition, palette, materials, lighting, and art treatment; animate it without "
        "restaging or introducing new elements."
    )
    if source_prompt:
        visual_anchor += f" Locked source-image description: {source_prompt}"
    camera_behavior = str(provenance.get("cameraBehavior") or "locked")
    camera_direction = {
        "locked": (
            "Camera behavior is LOCKED: keep the source frame edges, horizon, scale, crop, and composition fixed. "
            "No handheld movement, shake, pan, tilt, dolly, zoom, crop, or reframing. Motion must come from subjects "
            "and environmental elements already visible in the image."
        ),
        "slow-push": "Camera behavior is a single smooth, subtle slow push with no shake, crop jump, or direction change.",
        "pan-left": "Camera behavior is one smooth restrained pan left with no shake, zoom, crop jump, or direction change.",
        "pan-right": "Camera behavior is one smooth restrained pan right with no shake, zoom, crop jump, or direction change.",
    }.get(camera_behavior, "Camera behavior is LOCKED with no camera movement or reframing.")
    return (
        f"{visual_anchor} {camera_direction} Motion direction: {resolved_prompt} Composition policy: "
        f"{_god_portrayal_instruction(reference, verse)}"
    )


def _motion_negative_prompt(scene: dict[str, Any], provenance: dict[str, Any]) -> str:
    source_negative = str(provenance.get("sourceImageNegativePrompt") or "").strip()
    parts = [
        "static tableau, frozen pose, slideshow, no movement, scene cut, jump cut, jitter, flicker, camera shake, "
        "handheld wobble, sudden zoom, accidental crop, framing drift, uncontrolled pan, reframing",
        "face morph, anatomy distortion, identity change, clothing change, text, watermark",
        source_negative,
        _scene_negative_prompt(str(scene.get("title") or "")),
        GOD_CHARACTER_NEGATIVE,
    ]
    return ", ".join(part for part in parts if part)


def animate_bible_scene(
    project_id: str,
    scene_index: int,
    prompt: str = "",
    camera_behavior: str = "locked",
    motion_plan: dict[str, Any] | None = None,
    *,
    progress: Progress,
    log: Log,
) -> Path:
    document, scene, still_path, clip_path = scene_animation_context(project_id, scene_index)
    resolved_prompt = re.sub(r"\s+", " ", prompt).strip()
    if not resolved_prompt:
        progress("WRITING_MOTION_PROMPT", 0.08)
        log(f"Generating a continuity-safe animation prompt for scene {scene_index}")
        resolved_prompt = generate_scene_animation_prompt(
            project_id,
            scene_index,
            camera_behavior=camera_behavior,
        )
    if motion_plan:
        scene["motionPlan"] = _sanitize_motion_plan(motion_plan, source="edited")
    elif not scene.get("motionPlan"):
        progress("PLANNING_MOTION_REGIONS", 0.12)
        log(f"Planning controlled motion regions for scene {scene_index}")
        scene["motionPlan"] = generate_scene_motion_plan(project_id, scene_index, resolved_prompt)
        document, scene, still_path, clip_path = scene_animation_context(project_id, scene_index)
    resolved_prompt = _enforce_camera_behavior_prompt(resolved_prompt, scene, camera_behavior)
    scene["motionPrompt"] = resolved_prompt
    prior_generation = ((scene.get("timeline") or [{}])[0].get("motionGeneration") or {})
    prior_attempt = scene.get("motionAttempt")
    if prior_attempt is None:
        prior_attempt = prior_generation.get("motionAttempt")
    if prior_attempt is None and (scene.get("timeline") or [{}])[0].get("video"):
        prior_attempt = 1
    scene["motionAttempt"] = max(0, int(prior_attempt or 0)) + 1
    provenance = _motion_provenance(scene, still_path, scene_index, resolved_prompt)
    provenance["cameraBehavior"] = camera_behavior
    provider_prompt = _motion_provider_prompt(resolved_prompt, scene, provenance)

    progress("MOTION_GENERATION", 0.25)
    log(f"Animating scene {scene_index} from {still_path.name}")
    candidate_path = clip_path.with_name(f"{clip_path.stem}.candidate-{uuid.uuid4().hex[:8]}{clip_path.suffix}")
    try:
        quality = generate_motion_clip(
            still_path,
            candidate_path,
            prompt=provider_prompt,
            negative_prompt=_motion_negative_prompt(scene, provenance),
            seed=int(provenance["motionSeed"]),
            protect_style_frame=bool((provenance.get("decorativeFrameProtection") or {}).get("enabled")),
            camera_behavior=camera_behavior,
            motion_plan=scene.get("motionPlan") or {},
        )
        candidate_path.replace(clip_path)
    except Exception as exc:
        candidate_path.unlink(missing_ok=True)
        scene["animationQuality"] = {
            "status": "rejected",
            "cameraBehavior": camera_behavior,
            "reason": str(exc)[:1000],
            "updatedAt": time.time(),
        }
        project_name = str((document.get("info") or {}).get("name") or project_id)
        save_scenes(project_id, json.dumps(document, indent=2), project_name=project_name)
        raise
    provenance["quality"] = quality
    timeline = scene.setdefault("timeline", [{"image": still_path.name}])
    if not timeline:
        timeline.append({"image": still_path.name})
    timeline[0]["video"] = clip_path.name
    timeline[0]["motionGeneration"] = provenance
    scene["motionPrompt"] = resolved_prompt
    scene["animationQuality"] = {
        "status": "accepted",
        "cameraBehavior": camera_behavior,
        **quality,
        "updatedAt": time.time(),
    }
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
        plan_motion_sequence(
            canonical,
            verses,
            payload["visualStyle"],
            str(payload.get("themeInterpretation") or ""),
        )
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
                    "prompt": _scene_prompt(
                        reference,
                        text,
                        payload["visualStyle"],
                        plan.get("startState", ""),
                        str(payload.get("themeInterpretation") or ""),
                    ),
                }
            ],
        }
        if plan:
            scene.update(plan)
            scene["motionPrompt"] = _motion_prompt(
                scene, payload["visualStyle"], str(payload.get("themeInterpretation") or "")
            )
        scenes.append(scene)
    return canonical, scenes


def _generate_still(prompt: str, destination: Path, *, negative_extra: str = "", session=requests) -> dict[str, Any]:
    negative_prompt = (
        "text, watermark, logo, modern clothing, modern architecture, deformed anatomy, extra limbs, "
        f"duplicate people, face morph, blur, low detail, {GOD_CHARACTER_NEGATIVE}"
    )
    if negative_extra.strip():
        negative_prompt = f"{negative_prompt}, {negative_extra.strip()}"
    seed = random.randint(1, 2**63 - 1)
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
            "seed": seed,
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
    return {
        "prompt": prompt,
        "negativePrompt": negative_prompt,
        "seed": seed,
        "model": STABLE_DIFFUSION_CHECKPOINT,
        "sampler": "DPM++ 2M Karras",
        "steps": 24,
        "cfgScale": 7,
        "width": 1024,
        "height": 576,
    }


def _title_card_prompt(
    canonical: str,
    scenes: list[dict[str, Any]],
    visual_style: str,
    theme_interpretation: str = "",
) -> str:
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
        f"{_theme_prompt(theme_interpretation)} "
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
    theme_interpretation = str(state.get("themeInterpretation") or "")
    destination = p_input(project_id) / "leader" / "bible-title-card.png"
    progress("TITLE_CARD_GENERATION", 0.2)
    log(f"Generating passage-specific {visual_style} title card for {canonical}")
    _generate_still(
        _title_card_prompt(canonical, scenes, visual_style, theme_interpretation),
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
    theme_interpretation = str(
        state.get("themeInterpretation") or (document.get("info") or {}).get("themeInterpretation") or ""
    )
    last_path: Path | None = None
    for position, scene_index in enumerate(indexes, start=1):
        scene = scenes[scene_index - 1]
        reference = str(scene.get("title") or f"Scene {scene_index}")
        verse = str(scene.get("VO") or scene.get("description") or "")
        timeline = scene.setdefault("timeline", [{}])
        if not timeline:
            timeline.append({})
        prompt = _scene_prompt(
            reference,
            verse,
            visual_style,
            str(scene.get("startState") or ""),
            theme_interpretation,
        )
        image_name = str((scene.get("images") or [f"scene_{scene_index:03d}.png"])[0])
        destination = p_input(project_id) / "images" / Path(image_name).name
        if destination.exists():
            backup_dir = destination.parent / "history"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_name = f"{destination.stem}-{int(time.time())}{destination.suffix}"
            shutil.copy2(destination, backup_dir / backup_name)
            scene.setdefault("imageHistory", []).append(f"history/{backup_name}")
        log(f"Regenerating scenery-first still {position}/{len(indexes)} for {reference}")
        generation = _generate_still(prompt, destination, negative_extra=_scene_negative_prompt(reference))
        timeline[0]["image"] = destination.name
        timeline[0]["prompt"] = prompt
        timeline[0]["imageGeneration"] = generation
        timeline[0].pop("video", None)
        timeline[0].pop("motionGeneration", None)
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
        _title_card_prompt(
            canonical,
            scenes,
            str(payload.get("visualStyle") or "cinematic-natural-light"),
            str(payload.get("themeInterpretation") or ""),
        ),
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
            generation = _generate_still(
                timeline["prompt"],
                still_path,
                negative_extra=_scene_negative_prompt(str(scene.get("title") or "")),
            )
            timeline["imageGeneration"] = generation
        if payload.get("mode") == "motion":
            clip_name = f"scene_{index:03d}.mp4"
            clip_path = motion_dir / clip_name
            log(f"Generating motion clip {index}/{len(scenes)} for {scene['title']}")
            scene["motionPrompt"] = _enforce_camera_behavior_prompt(scene["motionPrompt"], scene, "locked")
            scene["motionAttempt"] = 1
            provenance = _motion_provenance(scene, still_path, index, scene["motionPrompt"])
            quality = generate_motion_clip(
                still_path,
                clip_path,
                prompt=_motion_provider_prompt(scene["motionPrompt"], scene, provenance),
                negative_prompt=_motion_negative_prompt(scene, provenance),
                seed=int(provenance["motionSeed"]),
                protect_style_frame=bool((provenance.get("decorativeFrameProtection") or {}).get("enabled")),
                camera_behavior="locked",
            )
            provenance["quality"] = quality
            timeline["video"] = clip_name
            timeline["motionGeneration"] = provenance
            scene["animationQuality"] = {"status": "accepted", **quality, "updatedAt": time.time()}
            previous_motion_path = clip_path
        progress("MOTION_GENERATION" if payload.get("mode") == "motion" else "IMAGE_GENERATION", 0.05 + (index / len(scenes)) * 0.45)

    spec = {
        "info": {
            "name": f"{canonical} ({payload.get('translation', 'kjv').upper()})",
            "source": "bible-studio",
            "passage": canonical,
            "mode": payload.get("mode", "still"),
            "characterDesignVersion": GOD_CHARACTER_DESIGN["version"],
            "themeInterpretation": str(payload.get("themeInterpretation") or ""),
            "resolvedThemeInterpretation": _theme_instruction(str(payload.get("themeInterpretation") or "")),
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
            "themeInterpretation": str(payload.get("themeInterpretation") or ""),
            "resolvedThemeInterpretation": _theme_instruction(str(payload.get("themeInterpretation") or "")),
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
