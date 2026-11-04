"""Editable, source-preserving short property highlights rendered on Sextant."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.highlight_sources import native_source_map, split_window
from app.media_ai_policy import artifact_ai_status, write_media_provenance
from app.storage import p_input, p_output, p_work, read_project_state
from app.vertical_framing import analyze_framing, vertical_filter

FPS = 30
GRAPHICS = {"hookSeconds": 2.0, "outroSeconds": 4.0, "transitionSeconds": 0.4}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mpg", ".mpeg"}
CONTACT_DEFAULTS = {"brokerage": "LeCrown Properties", "broker": "Jie Huang", "phone": "832-798-3991", "email": ""}


def _run(command: list[str], log: Callable | None = None) -> str:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stderr[-2200:]}")
    if log:
        log(f"Completed {Path(command[0]).name}")
    return result.stdout


def _probe(path: Path) -> dict:
    return json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def _duration(path: Path) -> float:
    duration = float((_probe(path).get("format") or {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The source video has no usable duration")
    return duration


def _source_path(project_id: str, name: str) -> Path:
    if not name or "\\" in name or Path(name).name != name or Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Choose a finished project video by filename")
    if name.startswith("tiktok_highlight_"):
        raise ValueError("Choose the original finished video rather than another highlight")
    output = p_output(project_id).resolve()
    source = output / name
    if not source.is_file() or source.is_symlink() or source.resolve().parent != output:
        raise ValueError("The selected finished video is not available in this project")
    return source


def _fingerprint(source: Path) -> dict:
    stat = source.stat()
    # Nanosecond epoch timestamps exceed JavaScript's exact integer range.
    # Keep them as strings through browser edits and JSON round trips.
    return {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns)}


def _read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return result if isinstance(result, dict) else {}


def _text(value: Any, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _known_source_ranges(project_id: str, document: dict, source_duration: float, state: dict) -> list[dict]:
    """Use saved clip titles only when retained intermediates prove their timing."""
    work = p_work(project_id)
    footage = document.get("sourceFootage") or []
    scenes = document.get("scenes") or []
    entries: list[tuple[Path, str]] = []
    transitions: list[float] = []
    if footage:
        for index, item in enumerate(footage):
            proxy = work / "footage" / f"clip_{index:02d}.mp4"
            if not proxy.is_file():
                return []
            entries.append((proxy, _text(item.get("title"), 120)))
            transition = work / f"hyperframes_footage_transition_{index:02d}.mp4"
            transitions.append(_duration(transition) if transition.is_file() and index < len(footage) - 1 else 0)
    elif scenes:
        for index, item in enumerate(scenes):
            proxy = work / f"scene_{index:02d}.mp4"
            if not proxy.is_file():
                return []
            entries.append((proxy, _text(item.get("title"), 120)))
            transition = next((path for path in (
                work / f"hyperframes_scene_transition_{index:02d}.mp4",
                work / f"hyperframes_transition_{index:02d}.mp4",
            ) if path.is_file()), work / f"hyperframes_scene_transition_{index:02d}.mp4")
            transitions.append(_duration(transition) if transition.is_file() and index < len(scenes) - 1 else 0)
    if not entries:
        return []
    opts = state.get("renderOptions") or {}
    intro = 0.0
    if opts.get("introEnabled", state.get("useIntro", False)):
        candidates = [work / "hyperframes_intro.mp4", work / "intro.mp4"]
        intro_path = next((path for path in candidates if path.is_file()), None)
        if intro_path is None:
            return []
        intro = _duration(intro_path)
    ranges = []
    cursor = intro
    for index, (path, title) in enumerate(entries):
        duration = _duration(path)
        ranges.append({"start": cursor, "end": cursor + duration, "caption": title})
        cursor += duration + transitions[index]
    # A different or older output can coexist with these intermediates. Do not
    # attach a room title unless the selected master matches this render map.
    if abs(cursor - source_duration) > 1.0:
        return []
    return ranges


def _caption_copy(document: dict, state: dict, title: str) -> list[str]:
    """Listing copy is a feature overlay, never an invented speech transcript."""
    text = state.get("originalScriptText") or state.get("script") or ""
    fragments = [_text(value, 140) for value in re.split(r"[\n。！？]|(?<=[.!?])\s+", str(text))]
    return [value for value in fragments if value and value != title][:20] or [title]


def plan_highlight(project_id: str, source_name: str, duration_seconds: float = 45) -> dict:
    source = _source_path(project_id, source_name)
    target = _number(duration_seconds, "Highlight duration")
    if not 30 <= target <= 60:
        raise ValueError("Choose a highlight duration between 30 and 60 seconds")
    target = round(target * FPS) / FPS
    duration = _duration(source)
    state = read_project_state(project_id) or {}
    document = _read_json(p_input(project_id) / "scenes.json")
    title = _text(state.get("title") or (document.get("info") or {}).get("name") or source.stem, 100)
    language = str((state.get("renderOptions") or {}).get("ttsLanguage") or state.get("language") or ((document.get("vid") or document.get("video") or {}).get("lang")) or "en")
    contact = {**CONTACT_DEFAULTS, **(state.get("tiktokHighlightContact") or {})}
    count = max(4, min(8, round((target - 6) / 6)))
    content_budget = target - GRAPHICS["hookSeconds"] - GRAPHICS["outroSeconds"] - GRAPHICS["transitionSeconds"] * (count - 1)
    frames_per_clip = int(round(content_budget * FPS)) // count
    lengths = [frames_per_clip / FPS] * count
    lengths[-1] = round((content_budget - sum(lengths[:-1])) * FPS) / FPS
    if duration + 0.02 < content_budget:
        raise ValueError("The source is too short for this highlight without repeating footage; choose a shorter duration")
    known_ranges = _known_source_ranges(project_id, document, duration, state)
    copy_fragments = _caption_copy(document, state, title)
    segments: list[dict] = []
    method = "distributed-source-windows"
    if len(known_ranges) > count:
        # A short highlight can represent a longer tour without falling back
        # into its already-rendered landscape title and transition cards.
        eligible = [region for region in known_ranges if region["end"] - region["start"] >= max(lengths)]
        if len(eligible) >= count:
            selected = [eligible[round(index * (len(eligible) - 1) / (count - 1))] for index in range(count)]
            method = "verified-source-section-subset"
            for index, (region, length) in enumerate(zip(selected, lengths)):
                start = region["start"] + (region["end"] - region["start"] - length) * 0.4
                segments.append({"id": f"beat-{index + 1}", "sourceStart": round(start, 3), "sourceEnd": round(start + length, 3), "caption": region["caption"] or title})
    if known_ranges and len(known_ranges) <= count and all(region["end"] - region["start"] >= max(lengths) for region in known_ranges):
        # Show every saved section, then spend extra beats in its longest section.
        allocations = [1] * len(known_ranges)
        for _ in range(count - len(known_ranges)):
            eligible = [i for i, region in enumerate(known_ranges) if (region["end"] - region["start"]) / (allocations[i] + 1) >= max(lengths)]
            if not eligible:
                break
            best = max(eligible, key=lambda i: (known_ranges[i]["end"] - known_ranges[i]["start"]) / allocations[i])
            allocations[best] += 1
        if sum(allocations) == count:
            method = "verified-source-section-ranges"
            for region, allocated in zip(known_ranges, allocations):
                bin_size = (region["end"] - region["start"]) / allocated
                for offset in range(allocated):
                    length = lengths[len(segments)]
                    start = region["start"] + offset * bin_size + (bin_size - length) * 0.4
                    segments.append({"id": f"beat-{len(segments) + 1}", "sourceStart": round(start, 3), "sourceEnd": round(start + length, 3), "caption": region["caption"] or title})
    if not segments:
        # Trim existing lead/end cards when enough real footage remains.
        start_skip = min(3.0, duration * 0.02)
        end_skip = min(4.0, duration * 0.02)
        if duration - start_skip - end_skip < content_budget:
            start_skip = end_skip = 0.0
        bin_size = (duration - start_skip - end_skip) / count
        for index, length in enumerate(lengths):
            start = start_skip + index * bin_size + (bin_size - length) * 0.4
            # Without a verified time map, only a neutral listing title is safe
            # over an arbitrary source frame. Full project copy remains editable.
            segments.append({"id": f"beat-{index + 1}", "sourceStart": round(start, 3), "sourceEnd": round(start + length, 3), "caption": title})
    plan = {
        "version": 1, "sourceFilename": source.name, "sourceDuration": round(duration, 3),
        "sourceFingerprint": _fingerprint(source), "durationSeconds": round(target, 3),
        "language": language, "title": title,
        "hook": title.split("|", 1)[0].strip(), "contact": contact,
        "captionSource": "project-text", "captionDescription": "Editable feature captions from saved project text; not a speech transcript.",
        "suggestedCaptionCopy": copy_fragments, "selectionMethod": method, "framingMode": "smart-native",
        "graphics": dict(GRAPHICS), "segments": segments,
    }
    return validate_plan(project_id, plan)


def validate_plan(project_id: str, plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("A highlight plan is required")
    result = copy.deepcopy(plan)
    source = _source_path(project_id, str(result.get("sourceFilename") or ""))
    fingerprint = result.get("sourceFingerprint")
    if fingerprint and fingerprint != _fingerprint(source):
        raise ValueError("The source video changed. Generate a fresh highlight plan before rendering")
    result["sourceFingerprint"] = _fingerprint(source)
    actual_duration = _duration(source)
    result["sourceDuration"] = actual_duration
    if not isinstance(result.get("graphics") or {}, dict):
        raise ValueError("Highlight graphic settings must be an object")
    graphics = {**GRAPHICS, **(result.get("graphics") or {})}
    for key, expected in GRAPHICS.items():
        value = _number(graphics[key], key)
        if abs(value - expected) > 0.001:
            raise ValueError("Highlight graphic timings are fixed; edit the selected source ranges instead")
    result["graphics"] = dict(GRAPHICS)
    segments = result.get("segments")
    if not isinstance(segments, list) or not 2 <= len(segments) <= 12:
        raise ValueError("A highlight needs between 2 and 12 source sections")
    occupied: list[tuple[float, float]] = []
    body_duration = 0.0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError("Each highlight section must be an object")
        start = _number(segment.get("sourceStart"), "Section start")
        end = _number(segment.get("sourceEnd"), "Section end")
        if start < 0 or end > actual_duration + 0.025 or not 1 <= end - start <= 20:
            raise ValueError(f"Section {index + 1} must select 1–20 seconds inside the source video")
        if any(start < other_end - 0.025 and end > other_start + 0.025 for other_start, other_end in occupied):
            raise ValueError("Highlight source sections must not repeat overlapping footage")
        occupied.append((start, end))
        caption = _text(segment.get("caption"), 181)
        if not caption or len(caption) > 180:
            raise ValueError("Each section needs a feature caption of at most 180 characters")
        segment.update({"id": _text(segment.get("id") or f"beat-{index + 1}", 40), "sourceStart": round(start, 3), "sourceEnd": round(end, 3), "caption": caption})
        body_duration += end - start
    total = body_duration + GRAPHICS["hookSeconds"] + GRAPHICS["outroSeconds"] + GRAPHICS["transitionSeconds"] * (len(segments) - 1)
    expected_total = _number(result.get("durationSeconds"), "Highlight duration")
    frame_aligned_total = round(total * FPS) / FPS
    if not 30 <= frame_aligned_total <= 60 or abs(total - expected_total) > 0.12:
        raise ValueError("Selected footage plus title/transition/contact cards must total 30–60 seconds and match the highlight duration")
    result["durationSeconds"] = round(frame_aligned_total, 6)
    result["title"] = _text(result.get("title"), 120)
    result["hook"] = _text(result.get("hook"), 120)
    if not result["title"] or not result["hook"]:
        raise ValueError("Add the highlight title and opening hook")
    if not isinstance(result.get("contact") or {}, dict):
        raise ValueError("Highlight contact details must be an object")
    contact = {**CONTACT_DEFAULTS, **(result.get("contact") or {})}
    contact = {key: _text(contact.get(key), 120) for key in CONTACT_DEFAULTS}
    if not contact["broker"] or not contact["brokerage"] or not contact["phone"]:
        raise ValueError("Add the broker, brokerage, and contact phone number")
    result["contact"] = contact
    result["captionSource"] = "project-text"
    if "spokenCaptionsEnabled" in result and not isinstance(result["spokenCaptionsEnabled"], bool):
        raise ValueError("Spoken captions must be enabled or disabled")
    return result


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _caption_lines(text: str) -> str:
    if re.search(r"[\u3400-\u9fff]", text):
        return "\n".join(text[index:index + 14] for index in range(0, len(text), 14))
    return "\n".join(textwrap.wrap(text, width=29, break_long_words=True, break_on_hyphens=False))


def _caption_schedule(text: str, duration: float) -> list[tuple[float, float, str]]:
    """Keep every word while limiting each visible group to three safe lines."""
    lines = _caption_lines(text).splitlines()
    groups = ["\n".join(lines[index:index + 3]) for index in range(0, len(lines), 3)]
    if not groups:
        return []
    start, end = 0.12, max(0.2, duration - 0.08)
    group_duration = (end - start) / len(groups)
    return [(start + index * group_duration, start + (index + 1) * group_duration, group) for index, group in enumerate(groups)]


def _ass_time(value: float) -> str:
    centiseconds = int(round(value * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _write_ass(path: Path, caption: str, duration: float) -> None:
    events = "".join(
        f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Highlight,,0,0,0,,{_ass_escape(group)}\n"
        for start, end, group in _caption_schedule(caption, duration)
    )
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Highlight,Noto Sans CJK SC,52,&H00FFFFFF,&H00FFFFFF,&H0020170C,&H9020170C,-1,0,0,0,100,100,0,0,3,10,0,2,100,210,675,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + events,
        encoding="utf-8",
    )


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_clip(source: Path, destination: Path, duration: float, log=None) -> None:
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.6f}", "-vf", "scale=1080:1920,setsar=1,fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(destination)], log)


def _visual_sections(source: Path, mapping: list[dict], start: float, end: float, source_info: dict) -> list[dict]:
    """Prefer matching native assets; unwrap padding only for master fallbacks."""
    sections = []
    for piece in split_window(mapping, start, end):
        if piece["kind"] != "master":
            sections.append({**piece, "crop": None, "framing": "native-asset"})
            continue
        length = piece["sourceEnd"] - piece["sourceStart"]
        for run in analyze_framing(source, piece["sourceStart"], length, probe=source_info):
            run_start = piece["sourceStart"] + run["start"]
            sections.append({"kind": "master", "assetPath": source, "assetStart": run_start,
                             "sourceStart": run_start, "sourceEnd": run_start + run["duration"],
                             "crop": run.get("crop"), "framing": run.get("confidence", "full-frame")})
    return sections


def _render_visual_section(piece: dict, destination: Path, log=None) -> None:
    length = piece["sourceEnd"] - piece["sourceStart"]
    asset = Path(piece["assetPath"])
    scale = float(piece.get("assetTimeScale") or 1.0)
    command = ["ffmpeg", "-v", "error", "-y"]
    if piece["kind"] == "image":
        command += ["-loop", "1", "-framerate", str(FPS), "-i", str(asset)]
    else:
        command += ["-ss", f"{piece['assetStart']:.6f}", "-t", f"{length * scale:.6f}", "-i", str(asset)]
    if piece["kind"] == "video" and abs(scale - 1) > 0.000001:
        graph = f"[0:v]setpts=(PTS-STARTPTS)/{scale:.9f}[native];" + vertical_filter("native", "v", crop=piece.get("crop"))
    else:
        graph = vertical_filter("0:v", "v", crop=piece.get("crop"))
    command += ["-filter_complex", graph, "-map", "[v]", "-an", "-t", f"{length:.6f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(destination)]
    _run(command, log)


def _render_body_section(source: Path, sections: list[dict], destination: Path, ass: Path,
                         start: float, duration: float, has_audio: bool, log=None) -> None:
    """Reframe visuals independently, then retain one exact master-audio window."""
    visuals = []
    for index, section in enumerate(sections):
        visual = destination.with_name(f"{destination.stem}_visual_{index:03d}.mp4")
        _render_visual_section(section, visual, log)
        visuals.append(visual)
    if not visuals:
        raise ValueError("The selected source section has no usable visuals")
    concat = destination.with_suffix(".visuals.txt")
    concat.write_text("\n".join(f"file '{path.as_posix()}'" for path in visuals), encoding="utf-8")
    command = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
    if has_audio:
        command += ["-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-i", str(source)]
    else:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    ass_filter = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
    fade_out = max(0, duration - 0.08)
    command += ["-map", "0:v:0", "-map", "1:a:0", "-vf", f"ass='{ass_filter}'",
                "-af", f"aresample=48000,afade=t=in:d=0.04,afade=t=out:st={fade_out:.6f}:d=0.08,apad",
                "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", str(destination)]
    _run(command, log)


def render_highlight(project_id: str, plan: dict, progress=None, log=None) -> Path:
    from app.renderer import _record_media_asset
    from app.tiktok_highlight_graphics import _brand_logo, _hyperframes_root, render_highlight_graphics

    plan = validate_plan(project_id, plan)
    source = _source_path(project_id, plan["sourceFilename"])
    spoken_enabled = plan.get("spokenCaptionsEnabled", False)
    prepared_captions = None
    render_source = source
    if spoken_enabled or source.with_suffix(".captions.json").is_file():
        from app.caption_variants import prepare_derivative_captions

        prepared_captions = prepare_derivative_captions(project_id, source, enabled=spoken_enabled, language=plan.get("language"))
        if prepared_captions is not None:
            render_source = prepared_captions.clean_source
    before = {"fingerprint": _fingerprint(source), "sha256": _hash(source)}
    source_ai_media = {
        "filename": source.name,
        "sha256": before["sha256"],
        **artifact_ai_status(project_id, source.name),
    }
    graphics_media: list[dict] = []
    _record_media_asset(graphics_media, p_input(project_id), _brand_logo(_hyperframes_root()), "highlight-logo", stock_artwork=True)
    token = uuid.uuid4().hex[:10]
    work = p_work(project_id) / "tiktok_highlights" / token
    work.mkdir(parents=True, exist_ok=False)
    output = p_output(project_id)
    output.mkdir(parents=True, exist_ok=True)
    filename = f"tiktok_highlight_{source.stem[:110]}_{token}.mp4"
    destination = output / filename
    temporary_output = output / f".{filename}.partial.mp4"
    source_info = _probe(render_source)
    has_audio = any(stream.get("codec_type") == "audio" for stream in source_info.get("streams", []))
    last_progress = 0.0

    def update(stage: str, value: float) -> None:
        nonlocal last_progress
        last_progress = max(last_progress, value)
        if progress:
            progress(stage, last_progress)
    update("HIGHLIGHT_VALIDATE", 0.03)
    pieces: list[Path] = []
    captions: list[tuple[float, float, str]] = []
    spoken_windows: list[tuple[float, float, float]] = []
    native_media: list[dict] = []
    native_snapshots: dict[Path, dict] = {}
    visual_sources: list[dict] = []
    cursor = 0.0
    mapping: list[dict] = []

    def record_native(piece: dict) -> None:
        if piece["kind"] == "master":
            return
        asset = Path(piece["assetPath"])
        if asset in native_snapshots:
            return
        _record_media_asset(native_media, p_input(project_id), asset, "highlight-native-visual")
        entry = native_media[-1]
        # The native map verifies that these pixels occur in this exact master.
        # A reviewed original master therefore covers an otherwise unclassified
        # matching photo; explicit generation provenance always takes precedence.
        if entry.get("isAigc") is None and source_ai_media["isAigc"] is False:
            entry.update(status="original", isAigc=False, basis="inherited")
            entry["reasons"] = ["Native visual verified against the reviewed original source video."]
        entry["fingerprint"] = _fingerprint(asset)
        native_snapshots[asset] = {"fingerprint": entry["fingerprint"], "sha256": entry["sha256"]}

    def image_at(second: float, name: str) -> Path:
        section = split_window(mapping, second, second + 1 / FPS)[0]
        record_native(section)
        if section["kind"] == "image":
            return Path(section["assetPath"])
        path = work / f"{name}.jpg"
        asset = Path(section["assetPath"]) if section["kind"] != "master" else render_source
        offset = section.get("assetStart", second)
        crop = None
        if section["kind"] == "master":
            crop = analyze_framing(render_source, second, 1 / FPS, probe=source_info)[0].get("crop")
        command = ["ffmpeg", "-v", "error", "-y", "-ss", f"{offset:.6f}", "-i", str(asset)]
        if crop:
            command += ["-vf", f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}"]
        _run([*command, "-frames:v", "1", "-q:v", "3", str(path)], log)
        return path

    def add_graphic(kind: str, duration: float, background: Path, hook: str) -> None:
        nonlocal cursor
        update(f"HIGHLIGHT_{kind.upper()}", min(0.89, 0.07 + len(pieces) * 0.055))
        graphic = render_highlight_graphics(work, title=plan["title"], hook=hook, contact=plan["contact"], language=plan.get("language", "en"), kind=kind, duration=duration, background_image=background, log=log)
        normalized = work / f"graphic_{len(pieces):02d}.mp4"
        _normalize_clip(graphic, normalized, duration, log)
        pieces.append(normalized)
        cursor += duration

    try:
        update("HIGHLIGHT_MAP", 0.04)
        mapping = native_source_map(
            project_id, render_source, _read_json(p_input(project_id) / "scenes.json"),
            read_project_state(project_id) or {}, probe_duration=_duration,
            windows=[(segment["sourceStart"], segment["sourceEnd"]) for segment in plan["segments"]],
        )
        add_graphic("hook", GRAPHICS["hookSeconds"], image_at(plan["segments"][0]["sourceStart"], "opening"), plan["hook"])
        for index, segment in enumerate(plan["segments"]):
            duration = segment["sourceEnd"] - segment["sourceStart"]
            ass = work / f"caption_{index:02d}.ass"
            _write_ass(ass, segment["caption"], duration)
            clip = work / f"footage_{index:02d}.mp4"
            sections = _visual_sections(render_source, mapping, segment["sourceStart"], segment["sourceEnd"], source_info)
            for section in sections:
                record_native(section)
                visual_sources.append({
                    "sourceStart": section["sourceStart"], "sourceEnd": section["sourceEnd"],
                    "kind": section["kind"], "assetFilename": Path(section["assetPath"]).name,
                    "assetStart": section.get("assetStart", 0), "crop": section.get("crop"),
                    "framing": section["framing"],
                })
            spoken_windows.append((segment["sourceStart"], segment["sourceEnd"], cursor))
            _render_body_section(render_source, sections, clip, ass, segment["sourceStart"], duration, has_audio, log)
            pieces.append(clip)
            captions.extend((cursor + start, cursor + end, text) for start, end, text in _caption_schedule(segment["caption"], duration))
            cursor += duration
            update(f"HIGHLIGHT_SECTION[{index + 1}]", 0.18 + (index + 1) / len(plan["segments"]) * 0.55)
            if index < len(plan["segments"]) - 1:
                next_segment = plan["segments"][index + 1]
                add_graphic("transition", GRAPHICS["transitionSeconds"], image_at(next_segment["sourceStart"], f"transition_{index:02d}"), next_segment["caption"])
        last_segment = plan["segments"][-1]
        add_graphic("outro", GRAPHICS["outroSeconds"], image_at(max(last_segment["sourceStart"], last_segment["sourceEnd"] - 0.1), "closing"), plan["hook"])
        update("HIGHLIGHT_ASSEMBLE", 0.91)
        concat_list = work / "clips.txt"
        concat_list.write_text("\n".join(f"file '{piece.as_posix()}'" for piece in pieces), encoding="utf-8")
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-t", f"{plan['durationSeconds']:.6f}", "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(temporary_output)], log)
        actual = _duration(temporary_output)
        if abs(actual - plan["durationSeconds"]) > 0.15 or not 29.85 <= actual <= 60.15:
            raise RuntimeError(f"Highlight duration validation failed: expected {plan['durationSeconds']:.2f}s, received {actual:.2f}s")
        if _fingerprint(source) != before["fingerprint"] or _hash(source) != before["sha256"]:
            raise RuntimeError("The source video changed during highlight rendering; output was not published")
        for asset, snapshot in native_snapshots.items():
            if _fingerprint(asset) != snapshot["fingerprint"] or _hash(asset) != snapshot["sha256"]:
                raise RuntimeError("A native visual changed during highlight rendering; output was not published")
        used_media = [*graphics_media, *native_media]
        used_statuses = [source_ai_media["isAigc"], *(item["isAigc"] for item in used_media)]
        output_ai_generated = True if True in used_statuses else (False if all(value is False for value in used_statuses) else None)
        output_ai_reasons = list(dict.fromkeys([*source_ai_media["reasons"], *(reason for item in used_media for reason in item["reasons"])]))
        provenance = {"version": 1, "filename": filename, "sourceFilename": source.name, "sourceSha256": before["sha256"], "sourceFingerprint": before["fingerprint"], "sourceUnchanged": True, "durationSeconds": actual, "width": 1080, "height": 1920, "captionSource": "project-text", "captionDescription": "Feature captions from project text, not a verbatim speech transcript", "plan": plan, "createdAt": time.time()}
        provenance["sourceAiMedia"] = source_ai_media
        provenance["graphicsAiMedia"] = graphics_media
        provenance["nativeAiMedia"] = native_media
        provenance["visualSources"] = visual_sources
        provenance["framingMode"] = "smart-native" if native_media else "adaptive-master"
        if prepared_captions is not None:
            from app.caption_variants import verify_derivative_source

            verify_derivative_source(prepared_captions)
        spoken_report = None
        if spoken_enabled:
            from app.caption_variants import apply_derivative_captions

            update("CAPTION_TRANSCRIBE" if prepared_captions.source_type == "whisper" else "CAPTION_TIMING", 0.93)
            spoken_report = apply_derivative_captions(
                prepared_captions, temporary_output, destination, windows=spoken_windows,
                progress_cb=lambda fraction: update("CAPTION_BURN" if fraction >= 0.9 else ("CAPTION_TRANSCRIBE" if prepared_captions.source_type == "whisper" else "CAPTION_TIMING"), 0.93 + 0.04 * fraction), log=log,
            )
            temporary_output.unlink(missing_ok=True)
            provenance["spokenCaptions"] = {"enabled": True, "source": spoken_report["source"], "cueCount": spoken_report["cueCount"]}
        else:
            temporary_output.replace(destination)
            if destination.with_suffix(".captions.json").is_file():
                from app.caption_variants import retire_caption_sidecars

                retire_caption_sidecars(destination)
        feature_name = f"{destination.stem}.features.srt" if spoken_enabled else f"{destination.stem}.srt"
        provenance["featureCaptionFile"] = feature_name
        # Only complete, validated files become selectable project outputs.
        (output / f"{destination.stem}.highlight.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / feature_name).write_text("\n\n".join(f"{index + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}" for index, (start, end, text) in enumerate(captions)) + "\n", encoding="utf-8")
        write_media_provenance(
            destination,
            ai_generated=output_ai_generated,
            reasons=output_ai_reasons,
            inputs=used_media,
            source=source_ai_media,
        )
        update("HIGHLIGHT_FINALIZE", 0.98)
        return destination
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise


def _srt_time(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
