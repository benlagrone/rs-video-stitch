"""Rendering pipeline built around FFmpeg."""
from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

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
DEFAULT_LEADER_TEMPLATE = os.getenv("LEADER_TEMPLATE_PATH", "")
DEFAULT_LOGO_IMAGE = os.getenv("LOGO_IMAGE_PATH", "")
DEFAULT_LOGO_WIDTH = int(os.getenv("LOGO_WIDTH", "360"))
AZURE_VOICE_API_URL = os.getenv("AZURE_VOICE_API_URL", "http://host.docker.internal:8013")
AZURE_VOICE_API_TOKEN = os.getenv("AZURE_VOICE_API_TOKEN", "")
AZURE_VOICE_TIMEOUT_SECONDS = float(os.getenv("AZURE_VOICE_TIMEOUT_SECONDS", "120"))
VIBEVOICE_API_URL = os.getenv("VIBEVOICE_API_URL", "http://host.docker.internal:8011")
VIBEVOICE_API_TOKEN = os.getenv("VIBEVOICE_API_TOKEN", "")
VIBEVOICE_TIMEOUT_SECONDS = float(os.getenv("VIBEVOICE_TIMEOUT_SECONDS", "300"))
VIBEVOICE_POLL_INTERVAL_SECONDS = float(os.getenv("VIBEVOICE_POLL_INTERVAL_SECONDS", "2"))
VIBEVOICE_DEFAULT_SPEAKER = os.getenv("VIBEVOICE_DEFAULT_SPEAKER", "Carter")
VIBEVOICE_DEFAULT_CFG_SCALE = float(os.getenv("VIBEVOICE_DEFAULT_CFG_SCALE", "1.5"))
VOICE_GATEWAY_URL = os.getenv("VOICE_GATEWAY_URL", "http://100.100.97.30:8133")
VOICE_GATEWAY_TIMEOUT_SECONDS = float(os.getenv("VOICE_GATEWAY_TIMEOUT_SECONDS", "300"))

_FONT_ENV_VAR = "TITLE_FONT_FILE"
_DEFAULT_FONT_RELATIVE = Path("media") / "EB_Garamond" / "EBGaramond-VariableFont_wght.ttf"
_DEFAULT_LEADER_RELATIVE = Path("media") / "leader.png"
_DEFAULT_LOGO_RELATIVE = Path("media") / "brand" / "logophone.png"
_BRAND_MEDIA_RELATIVE = Path("media") / "brand"


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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "off", "no", "none"}


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


def _timeline_headers(scene: Dict[str, Any], images: List[str]) -> Dict[str, str]:
    timeline = scene.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return {}

    headers: Dict[str, str] = {}
    image_set = set(images)
    for raw in timeline:
        if isinstance(raw, dict):
            image_name = raw.get("image")
            header = raw.get("header")
        else:
            image_name = getattr(raw, "image", None)
            header = getattr(raw, "header", None)
        if image_name in image_set and header is not None:
            cleaned = str(header).strip()
            if cleaned:
                headers[str(image_name)] = cleaned
    return headers


def _timeline_videos(scene: Dict[str, Any], images: List[str]) -> Dict[str, str]:
    timeline = scene.get("timeline")
    if not isinstance(timeline, list):
        return {}
    videos: Dict[str, str] = {}
    for raw in timeline:
        if isinstance(raw, dict) and raw.get("image") in images and raw.get("video"):
            videos[str(raw["image"])] = str(raw["video"])
    return videos


def _default_image_durations(
    scene: Dict[str, Any],
    image_count: int,
    min_shot: float,
    audio_duration: float,
) -> List[float]:
    scene_duration_raw = scene.get("duration")
    try:
        explicit_duration = float(scene_duration_raw) if scene_duration_raw is not None else 0.0
    except (TypeError, ValueError):
        explicit_duration = 0.0

    target_total = max(audio_duration + 0.25, explicit_duration, min_shot * max(1, image_count))
    per_image = target_total / max(1, image_count)
    return [per_image] * max(1, image_count)


def _overlay_title(
    source: Path,
    destination: Path,
    title_text: str,
    title_file: Path,
    title_font_path: Path,
    title_style: Dict[str, Any],
    preset: str,
    crf: str,
    log: Optional["LogFunc"],
) -> None:
    title_file.write_text(title_text, encoding="utf-8")
    try:
        font_size_candidate = float(title_style.get("fontSize")) if title_style.get("fontSize") is not None else 72.0
    except (TypeError, ValueError):
        font_size_candidate = 72.0
    font_size = max(1, int(round(font_size_candidate)))
    font_color = _normalize_color(title_style.get("fill")) or "white"
    border_color = _normalize_color(title_style.get("outline"), default_alpha=0.65) or "black@0.65"
    x_expr, y_expr = _title_coordinates(title_style.get("position"))
    draw_segments = [
        f"drawtext=fontfile='{_ffmpeg_escape(str(title_font_path))}'",
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
    video_filter = ":".join(draw_segments) + ",setsar=1"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        log,
    )


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


def _write_silence(destination: Path, duration: float, log: Optional[LogFunc]) -> None:
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
            str(destination),
        ],
        log,
    )


def _resolve_leader_template(input_dir: Path, requested: Optional[str], log: Optional[LogFunc]) -> Path:
    candidates: List[Path] = []
    if requested:
        requested_path = Path(str(requested).strip())
        if requested_path.is_absolute():
            candidates.append(requested_path)
        else:
            candidates.extend(
                [
                    input_dir / "leader" / requested_path.name,
                    input_dir / requested_path,
                ]
            )
    if DEFAULT_LEADER_TEMPLATE:
        candidates.append(Path(DEFAULT_LEADER_TEMPLATE).expanduser())

    base = Path(__file__).resolve()
    candidates.extend(
        [
            base.parents[2] / _DEFAULT_LEADER_RELATIVE,
            base.parents[1] / _DEFAULT_LEADER_RELATIVE,
            Path("/srv") / _DEFAULT_LEADER_RELATIVE,
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    searched = ", ".join(str(c) for c in candidates)
    _log(log, f"Leader template not found. Checked: {searched}")
    raise FileNotFoundError("Leader template image is missing")


def _resolve_logo_image(input_dir: Path, requested: Optional[str], log: Optional[LogFunc]) -> Path:
    candidates: List[Path] = []
    if requested:
        requested_path = Path(str(requested).strip())
        if requested_path.is_absolute():
            candidates.append(requested_path)
        else:
            base = Path(__file__).resolve()
            candidates.extend(
                [
                    input_dir / "logo" / requested_path.name,
                    input_dir / requested_path,
                    base.parents[2] / _BRAND_MEDIA_RELATIVE / requested_path.name,
                    base.parents[1] / _BRAND_MEDIA_RELATIVE / requested_path.name,
                    Path("/srv") / _BRAND_MEDIA_RELATIVE / requested_path.name,
                ]
            )
    if DEFAULT_LOGO_IMAGE:
        candidates.append(Path(DEFAULT_LOGO_IMAGE).expanduser())

    base = Path(__file__).resolve()
    candidates.extend(
        [
            base.parents[2] / _DEFAULT_LOGO_RELATIVE,
            base.parents[1] / _DEFAULT_LOGO_RELATIVE,
            Path("/srv") / _DEFAULT_LOGO_RELATIVE,
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate

    searched = ", ".join(str(c) for c in candidates)
    _log(log, f"Logo image not found. Checked: {searched}")
    raise FileNotFoundError("Logo image is missing")


def _logo_overlay_xy(corner: str, margin: int) -> tuple[str, str]:
    normalized = str(corner or "top-right").strip().lower().replace("_", "-")
    margin_expr = str(max(0, margin))
    if normalized in {"top-left", "left-top"}:
        return margin_expr, margin_expr
    if normalized in {"bottom-left", "left-bottom"}:
        return margin_expr, f"H-h-{margin_expr}"
    if normalized in {"bottom-right", "right-bottom"}:
        return f"W-w-{margin_expr}", f"H-h-{margin_expr}"
    return f"W-w-{margin_expr}", margin_expr


def _fit_image_frame_filter(input_label: str = "0:v", output_label: str = "v") -> str:
    return (
        f"[{input_label}]"
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,format=yuv420p"
        f"[{output_label}]"
    )


def _overlay_logo_on_video(
    source: Path,
    destination: Path,
    logo_path: Path,
    corner: str,
    margin: int,
    preset: str,
    crf: str,
    log: Optional[LogFunc],
) -> None:
    x_expr, y_expr = _logo_overlay_xy(corner, margin)
    logo_width = max(1, DEFAULT_LOGO_WIDTH)
    filter_complex = (
        f"[1:v]scale='min({logo_width},iw)':-1[logo];"
        f"[0:v][logo]overlay={x_expr}:{y_expr},setsar=1[v]"
    )
    _log(log, f"Overlaying logo {logo_path.name} at {corner} with {margin}px margin")
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-i",
            str(logo_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        log,
    )


def _wrap_intro_title(title: str, max_line_chars: int = 26) -> str:
    explicit_lines = [line.strip() for line in title.splitlines() if line.strip()]
    if explicit_lines:
        return "\n".join(explicit_lines[:3])

    words = title.strip().split()
    if not words:
        return ""
    lines: List[str] = []
    current = ""
    for word in words:
        proposed = f"{current} {word}".strip()
        if current and len(proposed) > max_line_chars:
            lines.append(current)
            current = word
        else:
            current = proposed
    if current:
        lines.append(current)
    return "\n".join(lines[:3])


def _intro_title_drawtext_filter(
    *,
    title_lines: List[str],
    title_dir: Path,
    title_font_path: Path,
    input_label: str,
    output_label: str,
) -> str:
    if not title_lines:
        return f"[{input_label}]null[{output_label}]"

    safe_font = _ffmpeg_escape(str(title_font_path))
    filters: List[str] = []
    previous_label = input_label
    line_gap = 88
    y_offset = (len(title_lines) - 1) * (line_gap // 2)
    for index, line in enumerate(title_lines):
        line_file = title_dir / f"intro_title_{index + 1}.txt"
        line_file.write_text(line, encoding="utf-8")
        next_label = output_label if index == len(title_lines) - 1 else f"intro_text_{index}"
        safe_line_file = _ffmpeg_escape(str(line_file))
        y_expr = f"h*0.42-{y_offset}+{index * line_gap}"
        filters.append(
            f"[{previous_label}]"
            f"drawtext=fontfile='{safe_font}':"
            f"textfile='{safe_line_file}':"
            "fontsize=74:fontcolor=white:line_spacing=0:borderw=2:bordercolor=black@0.45:"
            f"x=(w-text_w)/2:y={y_expr}"
            f"[{next_label}]"
        )
        previous_label = next_label
    return ";".join(filters)


def _create_intro_card_assets(
    *,
    background_image: Path,
    leader_template: Path,
    title: str,
    work_dir: Path,
    output_dir: Path,
    fps: int,
    duration: float,
    title_font_path: Path,
    preset: str,
    crf: str,
    log: Optional[LogFunc],
    thumbnail_enabled: bool,
) -> tuple[Path, Optional[Path]]:
    intro_dir = work_dir / "intro"
    intro_dir.mkdir(parents=True, exist_ok=True)
    title_file = intro_dir / "intro_title.txt"
    wrapped_title = _wrap_intro_title(title)
    title_file.write_text(wrapped_title, encoding="utf-8")
    title_lines = [line for line in wrapped_title.splitlines() if line.strip()]
    intro_video = intro_dir / "intro.mp4"
    intro_still = intro_dir / "intro-card.png"
    thumbnail_path = output_dir / "thumbnail.jpg"
    frames = max(1, round(duration * fps))
    leader_height = 864
    title_filter = _intro_title_drawtext_filter(
        title_lines=title_lines,
        title_dir=intro_dir,
        title_font_path=title_font_path,
        input_label="card",
        output_label="v",
    )
    card_filter = (
        f"{_fit_image_frame_filter('0:v', 'bg')};"
        f"[1:v]scale=-2:{leader_height},format=rgba[leader];"
        "[bg][leader]overlay=(W-w)/2:(H-h)/2[card];"
        f"{title_filter}"
    )
    _log(log, f"Creating 1.0s leader intro from {leader_template.name} over {background_image.name}")
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(background_image),
            "-loop",
            "1",
            "-i",
            str(leader_template),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            card_filter,
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-t",
            f"{duration:.3f}",
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
            str(intro_video),
        ],
        log,
    )

    still_filter = card_filter.replace(f":d={frames}:s=1920x1080:fps={fps}", f":d=1:s=1920x1080:fps={fps}")
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(background_image),
            "-i",
            str(leader_template),
            "-filter_complex",
            still_filter,
            "-map",
            "[v]",
            "-frames:v",
            "1",
            str(intro_still),
        ],
        log,
    )

    if not thumbnail_enabled:
        return intro_video, None

    generated_thumbnail: Optional[Path] = None
    for quality in range(3, 22, 2):
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(intro_still),
                "-vf",
                "scale=1280:720",
                "-frames:v",
                "1",
                "-q:v",
                str(quality),
                str(thumbnail_path),
            ],
            log,
        )
        if thumbnail_path.exists() and thumbnail_path.stat().st_size <= 2_000_000:
            generated_thumbnail = thumbnail_path
            break
    if generated_thumbnail is None and thumbnail_path.exists():
        generated_thumbnail = thumbnail_path
        _log(log, f"Thumbnail is {thumbnail_path.stat().st_size} bytes, above the 2 MB target")
    elif generated_thumbnail is not None:
        _log(log, f"Generated YouTube thumbnail {generated_thumbnail.name} ({generated_thumbnail.stat().st_size} bytes)")

    return intro_video, generated_thumbnail


def _synthesize_flite(text: str, destination: Path, voice: Optional[str], log: Optional[LogFunc]) -> None:
    text_path = destination.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    requested_voice = (voice or os.getenv("FLITE_VOICE") or "kal").strip() or "kal"
    flite_voice = requested_voice if requested_voice in {"kal", "awb", "rms", "slt"} else "kal"
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"flite=textfile={text_path}:voice={flite_voice}",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(destination),
        ],
        log,
    )


def _synthesize_azure_voice_proxy(
    text: str,
    destination: Path,
    *,
    project_id: str,
    voice: Optional[str],
    language: Optional[str],
    log: Optional[LogFunc],
) -> None:
    if not AZURE_VOICE_API_URL:
        raise TTSConfigurationError("AZURE_VOICE_API_URL is not configured")

    base_url = AZURE_VOICE_API_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if AZURE_VOICE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AZURE_VOICE_API_TOKEN}"

    payload = {
        "project_id": project_id,
        "speaker_name": voice or os.getenv("AZURE_TTS_VOICE") or "en-US-AdamMultilingualNeural",
        "language": language or os.getenv("AZURE_TTS_LANGUAGE") or "en-US",
        "turns": [{"speaker": "narrator", "text": text}],
        "output_subdir": "audio/azure-voice",
    }
    url = f"{base_url}/v1/tts/jobs"
    _log(log, f"Azure Voice proxy POST {url} voice={payload['speaker_name']!r}")
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=AZURE_VOICE_TIMEOUT_SECONDS)
        response.raise_for_status()
        job = response.json()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Azure Voice proxy request failed: {exc}") from exc
    except ValueError as exc:
        raise TTSConfigurationError("Azure Voice proxy returned invalid JSON") from exc

    status = str(job.get("status") or "").lower()
    if status not in {"completed", "succeeded", "success"}:
        detail = job.get("detail") or job.get("error") or job
        raise TTSConfigurationError(f"Azure Voice proxy job did not complete: {detail}")

    audio_url = str(job.get("audio_url") or "").strip()
    if not audio_url:
        raise TTSConfigurationError("Azure Voice proxy job did not return audio_url")
    download_url = audio_url if audio_url.startswith(("http://", "https://")) else f"{base_url}/{audio_url.lstrip('/')}"
    try:
        audio_response = requests.get(download_url, headers=headers, timeout=AZURE_VOICE_TIMEOUT_SECONDS)
        audio_response.raise_for_status()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Azure Voice proxy audio download failed: {exc}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_response.content)


def _vibevoice_speaker_name(voice: Optional[str]) -> str:
    requested = (voice or "").strip()
    if not requested:
        return VIBEVOICE_DEFAULT_SPEAKER
    if "neural" in requested.lower():
        return VIBEVOICE_DEFAULT_SPEAKER
    return requested


def _synthesize_vibevoice_proxy(
    text: str,
    destination: Path,
    *,
    project_id: str,
    voice: Optional[str],
    log: Optional[LogFunc],
) -> None:
    if not VIBEVOICE_API_URL:
        raise TTSConfigurationError("VIBEVOICE_API_URL is not configured")

    base_url = VIBEVOICE_API_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if VIBEVOICE_API_TOKEN:
        headers["Authorization"] = f"Bearer {VIBEVOICE_API_TOKEN}"

    speaker_name = _vibevoice_speaker_name(voice)
    payload = {
        "project_id": project_id,
        "mode": "single",
        "speaker_name": speaker_name,
        "turns": [{"speaker": "narrator", "text": text}],
        "cfg_scale": VIBEVOICE_DEFAULT_CFG_SCALE,
        "output_subdir": "audio/vibevoice",
    }
    create_url = f"{base_url}/v1/tts/jobs"
    _log(log, f"Fortress Voice API POST {create_url} speaker={speaker_name!r}")
    try:
        response = requests.post(create_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        job = response.json()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Fortress Voice API request failed: {exc}") from exc
    except ValueError as exc:
        raise TTSConfigurationError("Fortress Voice API returned invalid JSON") from exc

    job_id = str(job.get("job_id") or job.get("jobId") or "").strip()
    status = str(job.get("status") or "").lower()
    deadline = time.monotonic() + VIBEVOICE_TIMEOUT_SECONDS
    while job_id and status not in {"completed", "succeeded", "success", "failed", "error"}:
        if time.monotonic() >= deadline:
            raise TTSConfigurationError(f"Fortress Voice API job timed out: {job_id}")
        time.sleep(max(0.25, VIBEVOICE_POLL_INTERVAL_SECONDS))
        try:
            poll_response = requests.get(f"{base_url}/v1/tts/jobs/{job_id}", headers=headers, timeout=30)
            poll_response.raise_for_status()
            job = poll_response.json()
        except requests.RequestException as exc:
            raise TTSConfigurationError(f"Fortress Voice API poll failed: {exc}") from exc
        except ValueError as exc:
            raise TTSConfigurationError("Fortress Voice API poll returned invalid JSON") from exc
        status = str(job.get("status") or "").lower()

    if status not in {"completed", "succeeded", "success"}:
        detail = job.get("detail") or job.get("error") or job
        raise TTSConfigurationError(f"Fortress Voice API job did not complete: {detail}")

    audio_url = str(job.get("audio_url") or "").strip()
    if not audio_url:
        raise TTSConfigurationError("Fortress Voice API job did not return audio_url")
    download_url = audio_url if audio_url.startswith(("http://", "https://")) else f"{base_url}/{audio_url.lstrip('/')}"
    try:
        audio_response = requests.get(download_url, headers=headers, timeout=VIBEVOICE_TIMEOUT_SECONDS)
        audio_response.raise_for_status()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Fortress Voice API audio download failed: {exc}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_response.content)


def _synthesize_voice_gateway(
    text: str,
    destination: Path,
    *,
    project_id: str,
    voice: Optional[str],
    language: Optional[str],
    log: Optional[LogFunc],
) -> None:
    """Request narration through the Fortress-owned provider gateway."""
    if not VOICE_GATEWAY_URL:
        raise TTSConfigurationError("VOICE_GATEWAY_URL is not configured")

    base_url = VOICE_GATEWAY_URL.rstrip("/")
    payload = {
        "project_id": project_id,
        "mode": "single",
        "speaker_name": voice or VIBEVOICE_DEFAULT_SPEAKER,
        "language": language or DEFAULT_TTS_LANGUAGE,
        "turns": [{"speaker": "narrator", "text": text}],
        "cfg_scale": VIBEVOICE_DEFAULT_CFG_SCALE,
        "output_subdir": "audio/voice-gateway",
    }
    create_url = f"{base_url}/v1/tts/jobs"
    _log(log, f"Fortress Voice Gateway POST {create_url} voice={payload['speaker_name']!r}")
    try:
        response = requests.post(create_url, json=payload, timeout=30)
        response.raise_for_status()
        job = response.json()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Fortress Voice Gateway request failed: {exc}") from exc
    except ValueError as exc:
        raise TTSConfigurationError("Fortress Voice Gateway returned invalid JSON") from exc

    job_id = str(job.get("job_id") or job.get("jobId") or "").strip()
    status = str(job.get("status") or "").lower()
    deadline = time.monotonic() + VOICE_GATEWAY_TIMEOUT_SECONDS
    while job_id and status not in {"completed", "succeeded", "success", "failed", "error"}:
        if time.monotonic() >= deadline:
            raise TTSConfigurationError(f"Fortress Voice Gateway job timed out: {job_id}")
        time.sleep(max(0.25, VIBEVOICE_POLL_INTERVAL_SECONDS))
        try:
            poll_response = requests.get(f"{base_url}/v1/tts/jobs/{job_id}", timeout=30)
            poll_response.raise_for_status()
            job = poll_response.json()
        except requests.RequestException as exc:
            raise TTSConfigurationError(f"Fortress Voice Gateway poll failed: {exc}") from exc
        except ValueError as exc:
            raise TTSConfigurationError("Fortress Voice Gateway poll returned invalid JSON") from exc
        status = str(job.get("status") or "").lower()

    if status not in {"completed", "succeeded", "success"}:
        detail = job.get("detail") or job.get("error") or job
        raise TTSConfigurationError(f"Fortress Voice Gateway job did not complete: {detail}")

    audio_url = str(job.get("audio_url") or "").strip()
    if not audio_url:
        raise TTSConfigurationError("Fortress Voice Gateway job did not return audio_url")
    download_url = audio_url if audio_url.startswith(("http://", "https://")) else f"{base_url}/{audio_url.lstrip('/')}"
    try:
        audio_response = requests.get(download_url, timeout=VOICE_GATEWAY_TIMEOUT_SECONDS)
        audio_response.raise_for_status()
    except requests.RequestException as exc:
        raise TTSConfigurationError(f"Fortress Voice Gateway audio download failed: {exc}") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_response.content)


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
    if tts_api in {"none", "off", "silent", "silence"}:
        tts_api = "none"
    elif tts_api in {"voice-gateway", "fortress-voice-gateway"}:
        tts_api = "voice-gateway"
    elif tts_api in {"azure-proxy", "azure_voice", "azure-voice"}:
        tts_api = "azure-proxy"
    elif tts_api in {"vibevoice-proxy", "vibevoice", "fortress-voice", "local-voice-api"}:
        tts_api = "vibevoice-proxy"
    elif tts_api not in {"xtts", "azure", "flite", "local"}:
        _log(log, f"Unknown TTS api '{tts_api}', falling back to local voice")
        tts_api = "flite"
    elif tts_api == "local":
        tts_api = "flite"
    def _sanitize(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip().strip("'\"")

    azure_env_voice = _sanitize(os.getenv("AZURE_TTS_VOICE"))
    if tts_api == "azure":
        tts_voice_default = azure_env_voice
    elif tts_api == "flite":
        tts_voice_default = "kal"
    elif tts_api in {"voice-gateway", "vibevoice-proxy"}:
        tts_voice_default = VIBEVOICE_DEFAULT_SPEAKER
    else:
        tts_voice_default = DEFAULT_TTS_VOICE
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
    intro_enabled = _truthy(opts.get("introEnabled") or opts.get("intro_enabled"))
    info_meta = scenes_doc.get("info") if isinstance(scenes_doc.get("info"), dict) else {}
    if "introTitle" in opts or "intro_title" in opts:
        raw_intro_title = opts.get("introTitle") if "introTitle" in opts else opts.get("intro_title")
        intro_title = str(raw_intro_title or "").strip()
    else:
        intro_title = str(info_meta.get("name") or pid).strip()
    intro_leader_image = opts.get("introLeaderImage") or opts.get("intro_leader_image")
    try:
        intro_duration = float(opts.get("introDuration") or opts.get("intro_duration") or 1.0)
    except (TypeError, ValueError):
        intro_duration = 1.0
    intro_duration = max(0.1, intro_duration)
    thumbnail_enabled = _truthy(opts.get("thumbnailEnabled", True))
    logo_enabled = _truthy(opts.get("logoEnabled", True))
    logo_image = opts.get("logoImage") or opts.get("logo_image")
    logo_corner = str(opts.get("logoCorner") or opts.get("logo_corner") or "top-right")
    try:
        logo_margin = int(opts.get("logoMargin") or opts.get("logo_margin") or 24)
    except (TypeError, ValueError):
        logo_margin = 24
    logo_margin = max(0, logo_margin)
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
            if tts_api == "none":
                duration = estimate_seconds(voice_text)
                _log(log, f"Scene {idx}: TTS disabled; generating {duration:.1f}s silence")
                _write_silence(audio_wav, duration, log)
            elif voice_text.strip():
                try:
                    if tts_api == "flite":
                        _synthesize_flite(voice_text, audio_wav, tts_voice, log)
                    elif tts_api == "voice-gateway":
                        _synthesize_voice_gateway(
                            voice_text,
                            audio_wav,
                            project_id=pid,
                            voice=tts_voice or None,
                            language=tts_language,
                            log=log,
                        )
                    elif tts_api == "azure-proxy":
                        _synthesize_azure_voice_proxy(
                            voice_text,
                            audio_wav,
                            project_id=pid,
                            voice=tts_voice or None,
                            language=tts_language,
                            log=log,
                        )
                    elif tts_api == "vibevoice-proxy":
                        _synthesize_vibevoice_proxy(
                            voice_text,
                            audio_wav,
                            project_id=pid,
                            voice=tts_voice or None,
                            log=log,
                        )
                    elif tts_api == "azure":
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
                except TTSConfigurationError as exc:
                    _log(
                        log,
                        f"Scene {idx}: {exc}; falling back to local voice",
                    )
                    try:
                        _synthesize_flite(voice_text, audio_wav, tts_voice, log)
                    except Exception as flite_exc:  # noqa: BLE001
                        duration = estimate_seconds(voice_text)
                        _log(
                            log,
                            f"Scene {idx}: local voice failed ({flite_exc}); generating {duration:.1f}s timed silence",
                        )
                        _write_silence(audio_wav, duration, log)
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
                _write_silence(audio_wav, duration, log)
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
            per_image_durations = _default_image_durations(
                scene,
                len(images),
                min_shot,
                audio_duration,
            )
            _log(
                log,
                (
                    "Scene %s: using narration-matched durations "
                    "(audio %.3fs, visual %.3fs across %d image%s)"
                )
                % (
                    idx,
                    audio_duration,
                    sum(per_image_durations),
                    len(images),
                    "" if len(images) == 1 else "s",
                ),
            )
        image_headers = _timeline_headers(scene, images)
        image_videos = _timeline_videos(scene, images)

        temp_dir = work_dir / f"scene_{idx}"
        temp_dir.mkdir(exist_ok=True)
        segment_paths = []

        for img_index, (image_name, duration_seconds) in enumerate(zip(images, per_image_durations)):
            image_path = input_dir / "images" / image_name
            if not image_path.exists():
                raise FileNotFoundError(f"Missing image for scene {idx}: {image_name}")
            frames = max(1, round(duration_seconds * fps))
            segment = temp_dir / f"seg_{img_index:02d}.mp4"
            motion_name = image_videos.get(image_name)
            if motion_name:
                motion_path = input_dir / "motion" / motion_name
                if not motion_path.exists():
                    raise FileNotFoundError(f"Missing motion clip for scene {idx}: {motion_name}")
                motion_duration = ffprobe_duration(motion_path)
                visual_duration = max(duration_seconds, motion_duration)
                speed_factor = visual_duration / motion_duration if motion_duration > 0 else 1.0
                motion_filter = _fit_image_frame_filter("0:v", "v").replace(
                    ",setsar=1",
                    f",setpts={speed_factor:.8f}*PTS,setsar=1",
                )
                _log(
                    log,
                    (
                        "Scene %s: playing motion once and stretching %.3fs to %.3fs "
                        "so its final frame becomes the next scene handoff"
                    )
                    % (idx, motion_duration, visual_duration),
                )
                run([
                    "ffmpeg", "-y", "-i", str(motion_path),
                    "-t", f"{visual_duration:.3f}", "-filter_complex", motion_filter,
                    "-map", "[v]", "-an", "-c:v", "libx264", "-preset", preset, "-crf", crf,
                    "-pix_fmt", "yuv420p", str(segment),
                ], log)
                duration_seconds = visual_duration
            else:
                filter_complex = _fit_image_frame_filter("0:v", "v")
                run(
                    [
                        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
                        "-filter_complex", filter_complex, "-frames:v", str(frames), "-map", "[v]",
                        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", str(segment),
                    ],
                    log,
                )
            image_header = image_headers.get(image_name, "")
            if image_header:
                titled_segment = temp_dir / f"seg_{img_index:02d}_titled.mp4"
                header_file = temp_dir / f"seg_{img_index:02d}_header.txt"
                _log(log, f"Scene {idx}: overlaying image header '{image_header}' on {image_name}")
                _overlay_title(
                    segment,
                    titled_segment,
                    image_header,
                    header_file,
                    title_font_path,
                    title_style,
                    preset,
                    crf,
                    log,
                )
                segment = titled_segment
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
        if title_text and not image_headers:
            title_file = temp_dir / "title.txt"
            titled_scene = temp_dir / "scene_titled.mp4"
            _log(log, f"Scene {idx}: overlaying title '{title_text}'")
            if title_style:
                tracked = {key: title_style.get(key) for key in ("fontFamily", "fontSize", "fill", "outline", "position") if title_style.get(key) is not None}
                if tracked:
                    _log(log, f"Scene {idx}: title style overrides {tracked}")
            _overlay_title(
                silent_scene,
                titled_scene,
                title_text,
                title_file,
                title_font_path,
                title_style,
                preset,
                crf,
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
                "-af",
                "apad",
                "-shortest",
                str(scene_file),
            ],
            log,
        )
        scene_files.append(scene_file)

    update("CONCAT", 0.9)
    if intro_enabled and scenes:
        first_images = scenes[0].get("images") or []
        first_image_name = first_images[0] if first_images else None
        if first_image_name:
            first_image = input_dir / "images" / first_image_name
            leader_template = _resolve_leader_template(input_dir, str(intro_leader_image) if intro_leader_image else None, log)
            intro_file, thumbnail_file = _create_intro_card_assets(
                background_image=first_image,
                leader_template=leader_template,
                title=intro_title,
                work_dir=work_dir,
                output_dir=output_dir,
                fps=fps,
                duration=intro_duration,
                title_font_path=title_font_path,
                preset=preset,
                crf=crf,
                log=log,
                thumbnail_enabled=thumbnail_enabled,
            )
            scene_files.insert(0, intro_file)
            if thumbnail_file:
                _log(log, f"Thumbnail ready: {thumbnail_file}")
        else:
            _log(log, "Intro card requested, but first scene has no image; skipping intro")

    final_path = output_dir / output_name
    concat_output_path = final_path
    if logo_enabled:
        concat_output_path = work_dir / f"pre_logo_{Path(output_name).name}"
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
            str(concat_output_path),
        ],
        log,
    )

    if logo_enabled:
        logo_path = _resolve_logo_image(input_dir, str(logo_image) if logo_image else None, log)
        _overlay_logo_on_video(
            concat_output_path,
            final_path,
            logo_path,
            logo_corner,
            logo_margin,
            preset,
            crf,
            log,
        )

    update("FINALIZE", 0.98)
    return final_path
