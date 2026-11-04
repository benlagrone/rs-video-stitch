"""Fit the visible picture to one vertical canvas, preserving its full contents.

Flattened exports can contain existing letterbox/pillarbox bars. Only stable,
uniform black margins with a rectangular, lit interior are removed. Native
assets bypass this analysis and use ``vertical_filter`` directly.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Callable

_SAMPLE_WIDTH = 192
_SAMPLE_HEIGHT = 108
_SAMPLE_FPS = 10
_BLACK = 12


def _geometry(probe: dict) -> tuple[int, int, float]:
    video = next(stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video")
    width, height = int(video["width"]), int(video["height"])
    if width <= 0 or height <= 0:
        raise ValueError("The video has no usable picture dimensions")
    try:
        numerator, denominator = str(video.get("sample_aspect_ratio") or "1:1").split(":")
        sar = float(numerator) / float(denominator)
        if not math.isfinite(sar) or sar <= 0:
            sar = 1.0
    except (ValueError, ZeroDivisionError):
        sar = 1.0
    rotation = (video.get("tags") or {}).get("rotate", 0)
    for item in video.get("side_data_list") or []:
        if "rotation" in item:
            rotation = item["rotation"]
    try:
        if abs(round(float(rotation))) % 180 == 90:
            width, height, sar = height, width, 1 / sar
    except (ValueError, TypeError):
        pass
    return width, height, sar


def _probe(source: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def _black_line(values: list[int]) -> bool:
    # A very small compression outlier is allowed; textured/dim walls are not
    # treated as padding merely because their average is dark.
    return bool(values) and sum(value <= _BLACK for value in values) / len(values) >= 0.98 and max(values) <= 28


def _edge_count(flags: list[bool]) -> int:
    return next((index for index, flag in enumerate(flags) if not flag), len(flags))


def _balanced_pair(first: int, second: int, size: int) -> bool:
    return (
        min(first, second) >= max(3, math.ceil(size * 0.04))
        and first + second <= size * 0.80
        and abs(first - second) <= max(3, (first + second) * 0.20)
    )


def _picture_bounds(frame: bytes) -> tuple[int, int, int, int] | None:
    """Return a conservative rectangle in the fixed-size decoded sample."""
    width, height = _SAMPLE_WIDTH, _SAMPLE_HEIGHT
    if len(frame) != width * height:
        return None
    # Existing phone/logo overlays generally occupy the top or bottom edge.
    # The middle bands distinguish picture bounds from those overlays.
    ys = range(round(height * 0.23), round(height * 0.77))
    xs = range(round(width * 0.23), round(width * 0.77))
    columns = [_black_line([frame[y * width + x] for y in ys]) for x in range(width)]
    rows = [_black_line([frame[y * width + x] for x in xs]) for y in range(height)]
    left, right = _edge_count(columns), _edge_count(columns[::-1])
    top, bottom = _edge_count(rows), _edge_count(rows[::-1])
    if not _balanced_pair(left, right, width):
        left = right = 0
    if not _balanced_pair(top, bottom, height):
        top = bottom = 0
    if not any((left, right, top, bottom)):
        return None
    x_end, y_end = width - right, height - bottom
    interior = [frame[y * width + x] for y in range(top + 1, y_end - 1) for x in range(left + 1, x_end - 1)]
    if (not interior or sum(interior) / len(interior) < 24 or max(interior) < 48
            or sum(value > 20 for value in interior) / len(interior) < 0.50):
        return None

    def lit(values: list[int]) -> bool:
        return bool(values) and sum(value > 20 for value in values) / len(values) >= 0.30

    # Require a hard rectangular transition into actual picture on both sides.
    # A dark scene whose edges gradually fade toward its center fails this test.
    if left and not all(lit([frame[y * width + x] for y in ys]) for x in (left + 1, x_end - 2)):
        left = right = 0
    if top and not all(lit([frame[y * width + x] for x in xs]) for y in (top + 1, y_end - 2)):
        top = bottom = 0
    if not any((left, right, top, bottom)):
        return None
    return left, top, width - left - right, height - top - bottom


def _pixel_crop(bounds: tuple[int, int, int, int] | None, width: int, height: int) -> dict | None:
    if bounds is None:
        return None
    left, top, visible_width, visible_height = bounds
    # Round outward and retain a two-pixel safety margin rather than shaving
    # architectural detail at a compressed edge of the original picture.
    x = max(0, int(math.floor(left * width / _SAMPLE_WIDTH / 2)) * 2 - 2)
    y = max(0, int(math.floor(top * height / _SAMPLE_HEIGHT / 2)) * 2 - 2)
    end_x = min(width - width % 2, int(math.ceil((left + visible_width) * width / _SAMPLE_WIDTH / 2)) * 2 + 2)
    end_y = min(height - height % 2, int(math.ceil((top + visible_height) * height / _SAMPLE_HEIGHT / 2)) * 2 + 2)
    return {"x": x, "y": y, "width": end_x - x, "height": end_y - y}


def _compatible(first: dict | None, second: dict | None, width: int, height: int) -> bool:
    if first is None or second is None:
        return first is second
    tolerances = {"x": max(4, width / _SAMPLE_WIDTH * 2), "width": max(4, width / _SAMPLE_WIDTH * 4),
                  "y": max(4, height / _SAMPLE_HEIGHT * 2), "height": max(4, height / _SAMPLE_HEIGHT * 4)}
    return all(abs(first[key] - second[key]) <= tolerance for key, tolerance in tolerances.items())


def _union(first: dict, second: dict) -> dict:
    x, y = min(first["x"], second["x"]), min(first["y"], second["y"])
    return {"x": x, "y": y,
            "width": max(first["x"] + first["width"], second["x"] + second["width"]) - x,
            "height": max(first["y"] + first["height"], second["y"] + second["height"]) - y}


def _framing_runs(crops: list[dict | None], duration: float, width: int, height: int, sar: float) -> list[dict]:
    # Isolated detections cannot establish a real picture boundary. Full-frame
    # samples remain full-frame even between matching portrait bounds: a short
    # landscape insert must not inherit the neighboring portrait's crop.
    stable = list(crops)
    for index, crop in enumerate(crops):
        if crop is not None and not any(
            0 <= other < len(crops) and crops[other] is not None and _compatible(crop, crops[other], width, height)
            for other in (index - 1, index + 1)
        ):
            stable[index] = None
    groups: list[dict] = []
    for index, crop in enumerate(stable):
        if not groups or not _compatible(groups[-1]["crop"], crop, width, height):
            groups.append({"start": index / _SAMPLE_FPS, "crop": crop})
        elif crop is not None:
            groups[-1]["crop"] = _union(groups[-1]["crop"], crop)
    if not groups:
        groups = [{"start": 0.0, "crop": None}]
    for index, group in enumerate(groups):
        end = min(duration, groups[index + 1]["start"]) if index + 1 < len(groups) else duration
        group["duration"] = max(0.0, end - group["start"])
        crop = group["crop"]
        picture_width, picture_height = (crop["width"], crop["height"]) if crop else (width, height)
        group.update(orientation="portrait" if picture_width * sar < picture_height else "landscape",
                     confidence="black-bars" if crop else "full-frame")
    return [group for group in groups if group["duration"] > 0.00001]


def analyze_framing(
    source: Path, start: float, duration: float, probe: dict | Callable[[Path], dict] | None = None,
) -> list[dict]:
    """Find stable per-shot bounds, relative to the requested source window.

    Analysis samples at ten frames/second and processes at most sixty seconds
    per decoder call. A missing/unreliable analysis preserves the full frame.
    FFmpeg's normal autorotation is retained, including phone-video metadata.
    """
    start, duration = float(start), float(duration)
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
        raise ValueError("Framing requires a finite source start and positive duration")
    width, height, sar = 1920, 1080, 1.0
    try:
        width, height, sar = _geometry(probe(source) if callable(probe) else probe if probe is not None else _probe(source))
        crops: list[dict | None] = []
        offset = 0.0
        while offset < duration - 0.00001:
            chunk = min(60.0, duration - offset)
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{start + offset:.6f}", "-i", str(source),
                 "-t", f"{chunk:.6f}", "-an", "-sn", "-dn", "-vf",
                 f"fps={_SAMPLE_FPS},scale={_SAMPLE_WIDTH}:{_SAMPLE_HEIGHT}:flags=area,format=gray",
                 "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                capture_output=True, check=True, timeout=max(30.0, chunk * 3),
            )
            frame_size = _SAMPLE_WIDTH * _SAMPLE_HEIGHT
            count = len(result.stdout) // frame_size
            if len(result.stdout) % frame_size or abs(count - chunk * _SAMPLE_FPS) > 1.1:
                raise ValueError("Incomplete picture-boundary samples")
            crops.extend(_pixel_crop(_picture_bounds(result.stdout[index * frame_size:(index + 1) * frame_size]), width, height) for index in range(count))
            offset += chunk
        return _framing_runs(crops, duration, width, height, sar)
    except (OSError, ValueError, KeyError, StopIteration, TypeError, subprocess.SubprocessError):
        return _framing_runs([], duration, width, height, sar)


def vertical_filter(
    input_label: str = "0:v", output_label: str = "v", crop: dict | None = None,
    *, width: int = 1080, height: int = 1920, fps: int = 30,
) -> str:
    """Return a single-canvas graph: native picture first, soft fill behind it.

    No center crop is applied to the foreground. A native 9:16 shot fills the
    canvas; a landscape room stays entirely visible across its full width.
    """
    if not all(re.fullmatch(r"[A-Za-z0-9_:.-]+", label) for label in (input_label, output_label)):
        raise ValueError("Invalid video filter label")
    width, height, fps = int(width), int(height), int(fps)
    if width <= 0 or height <= 0 or fps <= 0 or width % 2 or height % 2:
        raise ValueError("Vertical canvas dimensions must be positive even integers")
    crop_filter = ""
    if crop is not None:
        values = {key: int(crop[key]) for key in ("x", "y", "width", "height")}
        if min(values["x"], values["y"]) < 0 or min(values["width"], values["height"]) <= 0:
            raise ValueError("Invalid visible-picture crop")
        crop_filter = f"crop={values['width']}:{values['height']}:{values['x']}:{values['y']},"
    prefix = re.sub(r"[^A-Za-z0-9_]", "_", output_label)
    return (
        f"[{input_label}]{crop_filter}scale=trunc(iw*sar/2)*2:ih,setsar=1,split=2[{prefix}_bg_src][{prefix}_fg_src];"
        f"[{prefix}_bg_src]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:10[{prefix}_bg];"
        f"[{prefix}_fg_src]scale={width}:{height}:force_original_aspect_ratio=decrease[{prefix}_fg];"
        f"[{prefix}_bg][{prefix}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[{output_label}]"
    )
