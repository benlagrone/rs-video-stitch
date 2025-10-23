"""Rendering pipeline built around FFmpeg."""
from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.tts import TTSConfigurationError, synthesize_azure, synthesize_xtts

DEFAULT_PRESET = os.getenv("DEFAULT_PRESET", "medium")
DEFAULT_CRF = int(os.getenv("DEFAULT_CRF", "18"))
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_MIN_SHOT = float(os.getenv("DEFAULT_MIN_SHOT", "2.5"))
DEFAULT_MAX_SHOT = float(os.getenv("DEFAULT_MAX_SHOT", "8.0"))
DEFAULT_TTS_VOICE = os.getenv("XTTS_VOICE") or os.getenv("DEFAULT_TTS_VOICE") or "p263"
DEFAULT_TTS_LANGUAGE = (
    os.getenv("XTTS_LANGUAGE")
    or os.getenv("DEFAULT_TTS_LANGUAGE")
    or "en"
)
DEFAULT_TTS_API = os.getenv("DEFAULT_TTS_API", "xtts").lower()

_FONT_ENV_VAR = "TITLE_FONT_FILE"
_DEFAULT_FONT_RELATIVE = Path("media") / "EB_Garamond" / "EBGaramond-VariableFont_wght.ttf"


@lru_cache(maxsize=1)
def _resolve_title_font() -> Path:
    """Locate the Garamond font used for scene titles."""

    env_override = os.getenv(_FONT_ENV_VAR)
    candidates: List[Path]
    if env_override:
        override_path = Path(env_override).expanduser().resolve()
        candidates = [override_path]
    else:
        base = Path(__file__).resolve()
        candidates = [
            base.parents[2] / _DEFAULT_FONT_RELATIVE,
            base.parents[1] / _DEFAULT_FONT_RELATIVE,
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        "Unable to locate Garamond font for scene titles. "
        f"Checked: {searched}. Set {_FONT_ENV_VAR} to an accessible TTF file."
    )


def _ffmpeg_escape(value: str) -> str:
    """Escape a value for safe inclusion in FFmpeg filter arguments."""
    return value.replace("\\", "\\\\").replace("'", r"\'")


@lru_cache(maxsize=1)
def _font_search_roots() -> List[Path]:
    """Return directories to scan for font files."""
    base = Path(__file__).resolve()
    candidates = [
        base.parents[2] / "media",
        base.parents[1] / "media",
    ]
    roots: List[Path] = []
    for candidate in candidates:
        if candidate.exists() and candidate not in roots:
            roots.append(candidate)
    return roots


@lru_cache(maxsize=1)
def _available_fonts() -> List[Path]:
    """Cache discovered font files for reuse."""
    fonts: List[Path] = []
    for root in _font_search_roots():
        fonts.extend(sorted(root.rglob("*.ttf")))
    return fonts


@lru_cache(maxsize=32)
def _find_font_by_family(font_family: str) -> Optional[Path]:
    """Attempt to locate a font based on a provided family name."""
    target = "".join(ch for ch in font_family.lower() if ch.isalnum())
    if not target:
        return None
    for font_path in _available_fonts():
        normalized = "".join(ch for ch in font_path.stem.lower() if ch.isalnum())
        if target in normalized:
            return font_path
    return None


def _format_alpha(alpha: float) -> str:
    clamped = max(0.0, min(1.0, alpha))
    return f"{clamped:.2f}".rstrip("0").rstrip(".") or "0"


def _normalize_color(value: Optional[str], default_alpha: Optional[float] = None) -> Optional[str]:
    """Normalize hex and named colors for FFmpeg drawtext usage."""
    if value is None:
        return None
    color = str(value).strip()
    if not color:
        return None

    if color.startswith("#"):
        hex_body = color[1:].strip()
        rgb = ""
        alpha_override: Optional[float] = None
        if len(hex_body) == 3:
            rgb = "".join(ch * 2 for ch in hex_body)
        elif len(hex_body) == 4:
            rgb = "".join(ch * 2 for ch in hex_body[:3])
            alpha_override = int(hex_body[3] * 2, 16) / 255.0
        elif len(hex_body) == 6:
            rgb = hex_body
        elif len(hex_body) == 8:
            rgb = hex_body[:6]
            alpha_override = int(hex_body[6:], 16) / 255.0
        else:
            rgb = hex_body

        rgb_value = f"#{rgb.lower()}"
        if alpha_override is not None:
            return f"{rgb_value}@{_format_alpha(alpha_override)}"
        if default_alpha is not None and "@" not in color:
            return f"{rgb_value}@{_format_alpha(default_alpha)}"
        return rgb_value

    if default_alpha is not None and "@" not in color:
        return f"{color}@{_format_alpha(default_alpha)}"
    return color


def _title_coordinates(position: Optional[str]) -> tuple[str, str]:
    margin_y = "h*0.08"
    margin_x = "w*0.08"
    default_x = "(w-text_w)/2"
    default_y = margin_y

    if not position:
        return default_x, default_y

    tokens = (
        str(position)
        .strip()
        .lower()
        .replace("_", "-")
        .split("-")
    )
    vertical = next((tok for tok in tokens if tok in {"top", "bottom", "middle", "center"}), None)
    horizontal = next((tok for tok in tokens if tok in {"left", "right", "middle", "center"}), None)

    if vertical in {"middle", "center"}:
        y_expr = "(h-text_h)/2"
    elif vertical == "bottom":
        y_expr = f"h-text_h-{margin_y}"
    else:
        y_expr = default_y

    if horizontal in {"left"}:
        x_expr = margin_x
    elif horizontal == "right":
        x_expr = f"w-text_w-{margin_x}"
    elif horizontal in {"middle", "center"}:
        x_expr = default_x
    else:
        x_expr = default_x

    return x_expr, y_expr


def _resolve_title_font_from_style(
    style: Dict[str, Any],
    project_root: Path,
    log: Optional[LogFunc],
) -> Path:
    """Resolve a font path based on optional title style hints."""
    font_file = style.get("fontFile")
    if isinstance(font_file, str) and font_file.strip():
        candidates = []
        font_path = Path(font_file.strip()).expanduser()
        if font_path.is_absolute():
            candidates.append(font_path)
        else:
            candidates.extend(
                [
                    project_root / font_path,
                    project_root / "input" / font_path,
                    Path(__file__).resolve().parents[2] / font_path,
                ]
            )
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except FileNotFoundError:
                continue
            if resolved.exists():
                return resolved
        _log(log, f"Title style fontFile '{font_file}' not found; using default font.")

    font_family = style.get("fontFamily")
    if isinstance(font_family, str) and font_family.strip():
        match = _find_font_by_family(font_family)
        if match:
            return match
        _log(log, f"Title style font '{font_family}' not found; using default font.")

    return _resolve_title_font()


def _timeline_durations(
    scene: Dict[str, Any],
    images: List[str],
    fps: int,
    audio_duration: float,
    scene_label: str,
    log: Optional[LogFunc],
) -> Optional[List[float]]:
    """Extract per-image durations from a scene timeline if possible."""
    timeline = scene.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return None

    entries: List[tuple[str, float]] = []
    for raw in timeline:
        if isinstance(raw, dict):
            image_name = raw.get("image")
            duration_value = raw.get("duration")
        else:
            image_name = getattr(raw, "image", None)
            duration_value = getattr(raw, "duration", None)

        if not image_name:
            _log(log, f"Scene {scene_label}: timeline entry missing image; falling back to defaults.")
            return None
        try:
            duration = float(duration_value)
        except (TypeError, ValueError):
            _log(log, f"Scene {scene_label}: timeline entry for {image_name} has invalid duration; falling back to defaults.")
            return None
        if duration <= 0:
            _log(log, f"Scene {scene_label}: timeline duration for {image_name} <= 0; falling back to defaults.")
            return None
        entries.append((str(image_name), duration))

    remaining = entries.copy()
    ordered: List[float] = []
    for image in images:
        match_index = next((idx for idx, (name, _) in enumerate(remaining) if name == image), None)
        if match_index is None:
            _log(log, f"Scene {scene_label}: timeline missing entry for {image}; falling back to defaults.")
            return None
        _, duration = remaining.pop(match_index)
        ordered.append(duration)

    if remaining:
        _log(log, f"Scene {scene_label}: timeline has extra entries; falling back to defaults.")
        return None

    timeline_total = sum(ordered)
    scene_duration_raw = scene.get("duration")
    try:
        explicit_duration = float(scene_duration_raw) if scene_duration_raw is not None else 0.0
    except (TypeError, ValueError):
        explicit_duration = 0.0
    target_total = max(timeline_total, explicit_duration, audio_duration)
    if ordered:
        delta = target_total - timeline_total
        if delta > 1e-3:
            ordered[-1] += delta

    min_duration = 1.0 / max(1, fps)
    for idx, duration in enumerate(ordered):
        if duration < min_duration:
            ordered[idx] = min_duration

    return ordered


LogFunc = Callable[[str], None]
ProgressFunc = Callable[[str, float], None]


def _log(log: Optional[LogFunc], message: str) -> None:
    if log:
        log(message)


def run(cmd: List[str], log: Optional[LogFunc] = None) -> None:
    """Run a subprocess and raise RuntimeError on failure."""
    _log(log, f"$ {' '.join(cmd)}")
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if process.stdout:
        _log(log, process.stdout.rstrip())
    if process.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{process.stdout}")


def ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def estimate_seconds(text: str, wpm: int = 165, floor: float = 5.0) -> float:
    words = max(1, len(text.strip().split()))
    return max(floor, (words / wpm) * 60.0)


def _resolve_project_root(pid: str, storage_root: Path) -> Path:
    """Locate the on-disk project directory, honoring meta indirection."""

    projects_root = storage_root / "projects"
    meta_path = projects_root / f"{pid}.meta.json"

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        directory = meta.get("directory")
        if directory:
            return projects_root / directory

    return projects_root / pid


def render_project(
    pid: str,
    storage_root: Path,
    opts: dict,
    output_name: str,
    log: Optional[LogFunc] = None,
    progress: Optional[ProgressFunc] = None,
) -> Path:
    """Render a project into an MP4 file."""

    def update(stage: str, value: float) -> None:
        if progress:
            progress(stage, value)

    project_root = _resolve_project_root(pid, storage_root)
    input_dir = project_root / "input"
    work_dir = project_root / "work"
    output_dir = project_root / "output"
    project_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    scenes_path = input_dir / "scenes.json"
    if not scenes_path.exists():
        raise FileNotFoundError(f"Scenes file missing: {scenes_path}")

    scenes_doc = json.loads(scenes_path.read_text("utf-8"))
    scenes = scenes_doc.get("scenes", [])
    if not scenes:
        raise ValueError("No scenes defined in scenes.json")

    video_meta = scenes_doc.get("video") or scenes_doc.get("vid") or {}

    fps = int(opts.get("fps", DEFAULT_FPS))
    min_shot = float(opts.get("minShot", DEFAULT_MIN_SHOT))
    max_shot = float(opts.get("maxShot", DEFAULT_MAX_SHOT))
    preset = opts.get("preset", DEFAULT_PRESET)
    crf = str(opts.get("crf", DEFAULT_CRF))
    raw_title_style = opts.get("titleStyle")
    if hasattr(raw_title_style, "model_dump"):
        raw_title_style = raw_title_style.model_dump()
    title_style = dict(raw_title_style) if isinstance(raw_title_style, dict) else {}
    voice_dir = opts.get("voiceDir")
    tts_api = (
        opts.get("ttsApi")
        or opts.get("tts_api")
        or video_meta.get("tts_api")
        or video_meta.get("api")
        or DEFAULT_TTS_API
    )
    tts_api = str(tts_api).lower() if tts_api else DEFAULT_TTS_API
    if tts_api not in {"xtts", "azure"}:
        _log(log, f"Unknown TTS api '{tts_api}', falling back to 'xtts'")
        tts_api = "xtts"
    def _sanitize(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().strip("'\"")

    azure_env_voice = _sanitize(os.getenv("AZURE_TTS_VOICE"))
    tts_voice_default = (
        azure_env_voice if tts_api == "azure" else DEFAULT_TTS_VOICE
    )
    tts_voice = opts.get("tts") or video_meta.get("voice") or tts_voice_default
    if tts_api == "azure":
        azure_fallback = azure_env_voice or tts_voice_default or "en-US-AriaNeural"
        if not tts_voice or "-" not in str(tts_voice):
            if tts_voice:
                _log(
                    log,
                    "Azure TTS overriding non-Azure voice %r with %r"
                    % (tts_voice, azure_fallback),
                )
            tts_voice = azure_fallback
    tts_language = (
        opts.get("ttsLanguage")
        or video_meta.get("language")
        or video_meta.get("lang")
        or DEFAULT_TTS_LANGUAGE
    )
    title_font_path = _resolve_title_font_from_style(title_style, project_root, log)
    _log(
        log,
        "Selected TTS api=%s voice=%s language=%s"
        % (tts_api, tts_voice or "<auto>", tts_language or "<default>"),
    )

    update("VALIDATE", 0.05)

    scene_files = []

    for index, scene in enumerate(scenes):
        idx = f"{index:02d}"
        images = scene.get("images") or []
        if not images:
            raise ValueError(f"Scene {idx} has no images")

        voice_text = scene.get("VO", "")
        voice_path: Optional[Path] = None
        if voice_dir:
            candidates = [
                input_dir / voice_dir / f"{idx}.wav",
                input_dir / voice_dir / f"{idx}.mp3",
                input_dir / voice_dir / f"scene_{idx}.wav",
                input_dir / voice_dir / f"scene_{idx}.mp3",
            ]
            voice_path = next((c for c in candidates if c.exists()), None)

        audio_wav = work_dir / f"scene_{idx}.wav"
        update("AUDIO_PREP", 0.1 + index * 0.02)
        if voice_path is None:
            if voice_text.strip():
                try:
                    if tts_api == "azure":
                        synthesize_azure(
                            voice_text,
                            audio_wav,
                            voice=tts_voice or None,
                            language=tts_language,
                            log=log,
                        )
                    else:
                        synthesize_xtts(
                            voice_text,
                            audio_wav,
                            voice=tts_voice or DEFAULT_TTS_VOICE,
                            language=tts_language,
                            log=log,
                        )
                except TTSConfigurationError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    api_label = tts_api.upper() if tts_api else "TTS"
                    raise RuntimeError(
                        f"{api_label} synthesis failed for scene {idx}: {exc}"
                    ) from exc
            else:
                if voice_text.strip():
                    _log(
                        log,
                        f"Scene {idx}: no TTS voice configured; generating silence",
                    )
                duration = estimate_seconds(voice_text)
                run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=48000:cl=stereo",
                        "-t",
                        f"{duration:.3f}",
                        str(audio_wav),
                    ],
                    log,
                )
        else:
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(voice_path),
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(audio_wav),
                ],
                log,
            )

        audio_duration = ffprobe_duration(audio_wav) or estimate_seconds(voice_text)
        timeline_durations = _timeline_durations(
            scene,
            images,
            fps,
            audio_duration,
            idx,
            log,
        )
        if timeline_durations:
            per_image_durations = timeline_durations
            _log(
                log,
                "Scene %s: using timeline durations (total %.3fs)"
                % (idx, sum(per_image_durations)),
            )
        else:
            per_image = max(
                min_shot,
                min(max_shot, (max(1.0, audio_duration - 0.4)) / max(1, len(images))),
            )
            per_image_durations = [per_image] * len(images)

        temp_dir = work_dir / f"scene_{idx}"
        temp_dir.mkdir(exist_ok=True)
        segment_paths = []

        for img_index, (image_name, duration_seconds) in enumerate(zip(images, per_image_durations)):
            image_path = input_dir / "images" / image_name
            if not image_path.exists():
                raise FileNotFoundError(f"Missing image for scene {idx}: {image_name}")
            frames = max(1, round(duration_seconds * fps))
            segment = temp_dir / f"seg_{img_index:02d}.mp4"
            zoom_target = 1.05
            if frames <= 1:
                zoom_expr = "1"
            else:
                # Ramp zoom so motion spans the full segment length.
                zoom_delta = zoom_target - 1.0
                zoom_steps = frames - 1
                zoom_expr = f"1+{zoom_delta:.6f}*(on/{zoom_steps})"
            filter_complex = (
                "[0:v]scale=1920:1080,format=yuv420p,"
                f"zoompan=z='{zoom_expr}':d={frames}:s=1920x1080:fps={fps}[v]"
            )
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(image_path),
                    "-filter_complex",
                    filter_complex,
                    # Cap the output to the expected frame count; combining -t with
                    # zoompan caused wildly inflated durations when the image input
                    # looped forever.
                    "-frames:v",
                    str(frames),
                    "-map",
                    "[v]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    preset,
                    "-crf",
                    crf,
                    "-pix_fmt",
                    "yuv420p",
                    str(segment),
                ],
                log,
            )
            _log(
                log,
                (
                    "Scene {idx}: added segment {name} from {image} "
                    "({seconds:.3f}s, {frames} frames)"
                ).format(
                    idx=idx,
                    name=segment.name,
                    image=image_name,
                    seconds=duration_seconds,
                    frames=frames,
                ),
            )
            segment_paths.append(segment)

        update(f"SCENE_BUILD[{idx}]", 0.2 + (index / max(1, len(scenes))) * 0.6)
        silent_scene = temp_dir / "scene_silent.mp4"
        concat_list = temp_dir / "segments.txt"
        concat_lines = []
        for seg in segment_paths:
            escaped = str(seg.resolve()).replace("'", "'\\''")
            concat_lines.append(f"file '{escaped}'")
            duration = ffprobe_duration(seg)
            _log(
                log,
                (
                    "Scene {idx}: appending {name} to concat list "
                    "({seconds:.3f}s)"
                ).format(
                    idx=idx,
                    name=seg.name,
                    seconds=duration,
                ),
            )
        concat_list.write_text("\n".join(concat_lines), encoding="utf-8")

        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                crf,
                "-pix_fmt",
                "yuv420p",
                str(silent_scene),
            ],
            log,
        )

        scene_file = work_dir / f"scene_{idx}.mp4"
        title_text = str(scene.get("title", "")).strip()
        titled_scene_source = silent_scene
        if title_text:
            title_file = temp_dir / "title.txt"
            title_file.write_text(title_text, encoding="utf-8")
            titled_scene = temp_dir / "scene_titled.mp4"
            try:
                font_size_candidate = float(title_style.get("fontSize")) if title_style.get("fontSize") is not None else 72.0
            except (TypeError, ValueError):
                font_size_candidate = 72.0
            font_size = max(1, int(round(font_size_candidate)))
            font_color = _normalize_color(title_style.get("fill")) or "white"
            border_color = _normalize_color(title_style.get("outline"), default_alpha=0.65) or "black@0.65"
            x_expr, y_expr = _title_coordinates(title_style.get("position"))
            draw_segments = [
                "drawtext",
                f"fontfile='{_ffmpeg_escape(str(title_font_path))}'",
                f"textfile='{_ffmpeg_escape(str(title_file))}'",
                f"fontsize={font_size}",
                f"fontcolor={font_color}",
                "line_spacing=6",
                "borderw=2",
                f"bordercolor={border_color}",
                "box=1",
                "boxcolor=black@0.35",
                "boxborderw=20",
                f"x={x_expr}",
                f"y={y_expr}",
            ]
            drawtext = ":".join(draw_segments)
            _log(log, f"Scene {idx}: overlaying title '{title_text}'")
            if title_style:
                tracked = {key: title_style.get(key) for key in ("fontFamily", "fontSize", "fill", "outline", "position") if title_style.get(key) is not None}
                if tracked:
                    _log(log, f"Scene {idx}: title style overrides {tracked}")
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(silent_scene),
                    "-vf",
                    drawtext,
                    "-c:v",
                    "libx264",
                    "-preset",
                    preset,
                    "-crf",
                    crf,
                    "-pix_fmt",
                    "yuv420p",
                    str(titled_scene),
                ],
                log,
            )
            titled_scene_source = titled_scene

        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(titled_scene_source),
                "-i",
                str(audio_wav),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(scene_file),
            ],
            log,
        )
        scene_files.append(scene_file)

    update("CONCAT", 0.9)
    final_path = output_dir / output_name
    concat_inputs: List[str] = []
    for scene_file in scene_files:
        concat_inputs.extend(["-i", str(scene_file)])

    concat_filter = "".join(f"[{i}:v][{i}:a]" for i in range(len(scene_files)))
    concat_filter += f"concat=n={len(scene_files)}:v=1:a=1[v][a]"

    run(
        [
            "ffmpeg",
            "-y",
            *concat_inputs,
            "-filter_complex",
            concat_filter,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(final_path),
        ],
        log,
    )

    update("FINALIZE", 0.98)
    return final_path
