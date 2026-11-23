"""Transcript-backed lead cards, transitions, and descriptions for stitched footage.

Saving or rendering source footage runs this once per clip list. A later save
keeps the written copy until the files or their order change.
"""
from __future__ import annotations

import fcntl
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from app.storage import (
    ensure_dirs,
    p_input,
    p_work,
    project_asset_path,
    read_project_state,
    save_project_state,
)

Progress = Callable[[str, float], None]
Log = Callable[[str], None]


class FootageCopyError(RuntimeError):
    """The footage transcript could not be turned into project text."""


_STOP_WORDS = {
    "a", "an", "the", "for", "in", "and", "of", "with", "near", "to", "from", "at",
    "on", "is", "are", "be", "this", "that", "very", "also", "has", "it", "its",
    "you", "your", "we", "our", "or", "not", "so", "let", "me", "can", "have",
    "do", "need", "just", "into", "over", "about",
}


def ordered_footage(state: dict) -> list[dict[str, Any]]:
    clips = []
    for item in state.get("sourceFootage") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("filename") or "").strip()
        if name:
            clips.append(item)
    return clips


def footage_names(state: dict) -> list[str]:
    return [Path(str(item.get("name") or item.get("filename"))).name for item in ordered_footage(state)]


def footage_signature(pid: str, names: list[str]) -> str:
    parts = []
    for name in names:
        path = project_asset_path(pid, "footage", name)
        if not path.is_file():
            raise FootageCopyError(f"Footage file is missing: {name}")
        stat = path.stat()
        parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def copy_is_current(state: dict, signature: str) -> bool:
    copy = state.get("footageCopy") if isinstance(state.get("footageCopy"), dict) else {}
    return copy.get("status") == "ready" and copy.get("signature") == signature


def _existing_copy_matches_clips(state: dict, names: list[str]) -> bool:
    """Keep text that was already written for this exact clip list."""
    if state.get("footageCopy"):
        return False
    if not str(state.get("script") or "").strip():
        return False
    if not any(str(line).strip() for line in (state.get("introLines") or [])):
        return False
    titles = {
        Path(str(item.get("name") or item.get("filename") or "")).name: str(item.get("title") or "").strip()
        for item in ordered_footage(state)
    }
    if not all(titles.get(name) for name in names):
        return False
    transitions = (state.get("hyperframes") or {}).get("transitionTitles") or {}
    return len(names) < 2 or bool(transitions)


def clean_transition_title(value: str) -> str:
    """Keep the title words from an Ollama transition response."""
    title = str(value or "").strip()
    match = re.search(r"\{.*\}", title, flags=re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("title", "transition", "text"):
                if str(payload.get(key) or "").strip():
                    title = str(payload[key]).strip()
                    break
        elif isinstance(payload, str) and payload.strip():
            title = payload.strip()
        else:
            title = match.group(0).strip("{}")
    title = title.splitlines()[0].strip()
    title = "".join(char for char in title if unicodedata.category(char) != "So").strip()
    title = re.sub(r"^(?:title|transition)\s*:\s*", "", title, flags=re.IGNORECASE).strip()
    title = title.strip(" `\"'{}*-:;,.!_")
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        raise ValueError("empty transition title")
    return title[:80].rstrip()


def spoken_title(text: str) -> str:
    cleaned = re.sub(r"^(?:hello|hi)[,.]?\s*", "", str(text or "").strip(), flags=re.IGNORECASE)
    sentence = re.split(r"[.!?。！？]", cleaned, maxsplit=1)[0]
    words = sentence.split()[:6]
    title = " ".join(words).strip(" ,;:-")
    return title[:80] or "Next"


def unsupported_words(lines: list[str], transcript: str) -> list[str]:
    source = transcript.lower()
    tokens = set(re.findall(r"[a-z0-9']+", source))
    missing = []
    for line in lines:
        for word in re.findall(r"[a-z0-9']+", line.lower()):
            if word in _STOP_WORDS or len(word) <= 2 or word in missing:
                continue
            if word in source:
                continue
            if any(min(len(word), len(token)) >= 5 and (token.startswith(word) or word.startswith(token)) for token in tokens):
                continue
            missing.append(word)
    return missing


def render_options_for_footage(options: dict, state: dict) -> dict:
    """Use the transcript copy on a stitch, including a request that still has the old card."""
    copy = state.get("footageCopy") if isinstance(state.get("footageCopy"), dict) else {}
    if copy.get("status") != "ready":
        return options
    lines = [str(line).strip() for line in (state.get("introLines") or []) if str(line).strip()]
    if not lines:
        return options
    updated = dict(options)
    updated["introEnabled"] = True
    updated["introTitle"] = "\n".join(lines[:3])
    hyperframes = dict(updated.get("hyperframes") or {})
    saved = state.get("hyperframes") if isinstance(state.get("hyperframes"), dict) else {}
    titles = saved.get("transitionTitles")
    if isinstance(titles, dict) and titles:
        hyperframes["transitionTitles"] = titles
    for key in (
        "leadCard",
        "leadCardMode",
        "sceneTransition",
        "sceneTransitionMode",
        "leadCardRight1",
        "leadCardRight2",
        "projectId",
    ):
        if not hyperframes.get(key) and saved.get(key):
            hyperframes[key] = saved[key]
    hyperframes.setdefault("leadCardMode", "hyperframes")
    hyperframes.setdefault("sceneTransitionMode", "hyperframes")
    updated["hyperframes"] = hyperframes
    return updated


def _progress(progress: Optional[Progress], stage: str, value: float) -> None:
    if progress:
        progress(stage, value)


def _log(log: Optional[Log], message: str) -> None:
    if log:
        log(message)


def _default_transcribe(path: Path, work_dir: Path, language: str) -> str:
    from app.video_captions import transcribe_video

    cues = transcribe_video(path, work_dir, language=language or None)
    return " ".join(cue.text.strip() for cue in cues if getattr(cue, "text", "").strip()).strip()


def _default_lead_lines(title: str, transcript: str, language: str) -> list[str]:
    from fastapi import HTTPException

    from app.api import (
        LeadCardGenerateRequest,
        _lead_card_lines_are_usable,
        _lead_card_prompt,
        _ollama_generate,
        _parse_lead_card_lines,
    )

    request = LeadCardGenerateRequest(title=title, script=transcript, language=language)
    prompt = _lead_card_prompt(request, transcript)
    best: list[str] | None = None
    best_gaps = 99
    for _attempt in range(4):
        try:
            raw = _ollama_generate(prompt, temperature=0.2, num_predict=360, json_output=True)
            lines = _parse_lead_card_lines(raw)
        except HTTPException:
            continue
        if not _lead_card_lines_are_usable(lines, transcript):
            prompt += (
                "\nThe previous card was rejected. Leave every price out and use the transcript language.\n"
                f"Rejected card:\n{json.dumps(lines, ensure_ascii=False)}"
            )
            continue
        gaps = unsupported_words(lines, transcript)
        if not gaps:
            return lines
        if len(gaps) < best_gaps:
            best = lines
            best_gaps = len(gaps)
        prompt += "\nRemove these words because they are not in the transcript: " + ", ".join(gaps)
    if best:
        return best
    raise FootageCopyError("Ollama did not return a lead card from the footage transcript")


def _default_transition_title(outgoing: str, incoming: str, narration: str, language: str) -> str:
    from fastapi import HTTPException

    from app.api import (
        HyperframesTransitionTitleRequest,
        _hyperframes_transition_title_prompt,
        _ollama_generate,
    )

    request = HyperframesTransitionTitleRequest(
        outgoingTitle=(outgoing or "Previous clip")[:160],
        incomingTitle=(incoming or "Next clip")[:160],
        narration=narration[:1200],
        language=language or "en-US",
    )
    prompt = _hyperframes_transition_title_prompt(request)
    for _attempt in range(3):
        try:
            title = clean_transition_title(_ollama_generate(prompt, temperature=0.3, num_predict=32))
        except (HTTPException, ValueError):
            continue
        if re.search(r"[$€£¥]", title):
            continue
        return title
    return spoken_title(incoming or narration)


def _default_youtube(title: str, transcript: str) -> str:
    from fastapi import HTTPException

    from app.api import YouTubeDescriptionRequest, _generate_youtube_description

    try:
        description = _generate_youtube_description(
            YouTubeDescriptionRequest(title=title, script=transcript[:12000])
        )
    except HTTPException:
        return transcript
    if re.search(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", description) and not re.search(
        r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)",
        transcript,
    ):
        return transcript
    return description


def _default_tiktok(pid: str, state: dict, transcript: str) -> str:
    from fastapi import HTTPException

    from app.api import _load_scenes, _ollama_generate
    from app.media_ai_policy import is_lecrown_project
    from app.tiktok_caption import CaptionGenerationError, caption_context, generate_caption

    fields = {
        "title": state.get("title") or "",
        "propertyAddress": state.get("propertyAddress") or "",
        "originalScriptText": transcript,
        "script": transcript,
        "language": state.get("language") or "",
    }
    try:
        context = caption_context(
            state,
            _load_scenes(pid) or {},
            fields,
            lecrown=is_lecrown_project(pid, state),
        )
        return generate_caption(context, _ollama_generate)
    except (CaptionGenerationError, HTTPException, ValueError):
        return ""


def _update_scenes(pid: str, names: list[str], titles: list[str], state: dict) -> None:
    path = p_input(pid) / "scenes.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    existing = {
        str(item.get("filename") or ""): item
        for item in (document.get("sourceFootage") or [])
        if isinstance(item, dict)
    }
    document["sourceFootage"] = [
        {
            "filename": name,
            "title": titles[index],
            "showTitle": bool((existing.get(name) or {}).get("showTitle")),
        }
        for index, name in enumerate(names)
    ]
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    info.setdefault("name", state.get("title") or pid)
    info.setdefault("source", "render-ui")
    document["info"] = info
    document.setdefault("scenes", [])
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def prepare_footage_copy(
    pid: str,
    *,
    progress: Optional[Progress] = None,
    log: Optional[Log] = None,
    transcribe: Optional[Callable[[Path, Path, str], str]] = None,
    lead_lines: Optional[Callable[[str, str, str], list[str]]] = None,
    transition_title: Optional[Callable[[str, str, str, str], str]] = None,
    youtube_description: Optional[Callable[[str, str], str]] = None,
    tiktok_caption: Optional[Callable[[str, dict, str], str]] = None,
) -> dict[str, Any]:
    """Transcribe stitched clips and store the lead card, transitions, and descriptions."""
    ensure_dirs(pid)
    lock_path = p_work(pid) / ".footage-copy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _prepare_footage_copy_locked(
                pid,
                progress=progress,
                log=log,
                transcribe=transcribe or _default_transcribe,
                lead_lines=lead_lines or _default_lead_lines,
                transition_title=transition_title or _default_transition_title,
                youtube_description=youtube_description or _default_youtube,
                tiktok_caption=tiktok_caption or _default_tiktok,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prepare_footage_copy_locked(
    pid: str,
    *,
    progress: Optional[Progress],
    log: Optional[Log],
    transcribe: Callable[[Path, Path, str], str],
    lead_lines: Callable[[str, str, str], list[str]],
    transition_title: Callable[[str, str, str, str], str],
    youtube_description: Callable[[str, str], str],
    tiktok_caption: Callable[[str, dict, str], str],
) -> dict[str, Any]:
    state = read_project_state(pid) or {}
    names = footage_names(state)
    if not names:
        return {"active": False, "generated": False}
    signature = footage_signature(pid, names)
    if copy_is_current(state, signature):
        _log(log, "Footage copy already matches these clips")
        return {"active": True, "generated": False, "signature": signature}
    if _existing_copy_matches_clips(state, names):
        state["footageCopy"] = {
            "status": "ready",
            "signature": signature,
            "updatedAt": time.time(),
            "clips": names,
        }
        state["updatedAt"] = time.time()
        save_project_state(pid, state, project_name=str(state.get("title") or pid))
        _log(log, "Kept the transcript copy already saved for these clips")
        return {"active": True, "generated": False, "signature": signature}

    language = str(state.get("language") or "en-US")
    title = str(state.get("title") or pid)
    _log(log, f"Transcribing {len(names)} source clips")
    parts: list[str] = []
    work_root = p_work(pid) / "lead-transcripts"
    for index, name in enumerate(names):
        _progress(progress, "TRANSCRIBE", index / max(len(names), 1) * 0.55)
        source = project_asset_path(pid, "footage", name)
        text = transcribe(source, work_root / name, language).strip()
        if not text:
            raise FootageCopyError(f"The footage transcript is empty: {name}")
        parts.append(text)
        _log(log, f"Transcribed {name} ({len(text)} characters)")
    transcript = "\n\n".join(parts)

    _progress(progress, "LEAD_CARD", 0.62)
    lines = [str(line).strip() for line in lead_lines(title, transcript, language)]
    lines = [line for line in lines if line][:3]
    if len(lines) < 3:
        raise FootageCopyError("The lead card needs three lines from the transcript")
    _log(log, "Lead card: " + " | ".join(lines))

    _progress(progress, "TRANSITIONS", 0.74)
    clip_titles = []
    for part in parts:
        clip_titles.append(transition_title(part[:160], part[-160:], part[:1200], language))
    transitions = {
        str(index): transition_title(parts[index][:160], parts[index + 1][:160], parts[index + 1][:1200], language)
        for index in range(len(parts) - 1)
    }
    _log(log, "Transitions: " + json.dumps(transitions, ensure_ascii=False))

    _progress(progress, "DESCRIPTIONS", 0.86)
    description = youtube_description(lines[0], transcript).strip() or transcript
    caption = tiktok_caption(pid, {**state, "script": transcript, "originalScriptText": transcript}, transcript).strip()

    intro_lines = (lines + [""] * 6)[:6]
    hyperframes = dict(state.get("hyperframes") or {})
    hyperframes["transitionTitles"] = transitions
    hyperframes.setdefault("leadCard", "lecrown-lead-card")
    hyperframes.setdefault("sceneTransition", "lecrown-scene-transition")
    hyperframes.setdefault("leadCardMode", "hyperframes")
    hyperframes.setdefault("sceneTransitionMode", "hyperframes")
    state["introLines"] = intro_lines
    state["introTitle"] = "\n".join(lines)
    state["useIntro"] = True
    state["script"] = transcript
    state["originalScriptText"] = transcript
    state["youtubeTitle"] = lines[0]
    state["youtubeDescription"] = description
    if caption:
        state["tiktokCaption"] = caption
        state["tiktokCaptionProvenance"] = {"generatedBy": "ollama", "source": "footage-transcript"}
    previous = {str(item.get("name") or ""): item for item in ordered_footage(state)}
    state["sourceFootage"] = [
        {
            "name": name,
            "title": clip_titles[index],
            "showTitle": bool((previous.get(name) or {}).get("showTitle")),
        }
        for index, name in enumerate(names)
    ]
    state["hyperframes"] = hyperframes
    state["footageCopy"] = {
        "status": "ready",
        "signature": signature,
        "updatedAt": time.time(),
        "clips": names,
    }
    state["projectId"] = pid
    state["updatedAt"] = time.time()
    save_project_state(pid, state, project_name=str(state.get("title") or pid))
    _update_scenes(pid, names, clip_titles, state)
    summary = p_work(pid) / "footage-copy.json"
    summary.write_text(json.dumps({
        "lead": lines,
        "transitions": transitions,
        "clipTitles": clip_titles,
    }, indent=2), encoding="utf-8")
    _progress(progress, "SAVE", 0.98)
    _log(log, "Footage transcript copy saved")
    return {"active": True, "generated": True, "signature": signature, "path": str(summary)}
