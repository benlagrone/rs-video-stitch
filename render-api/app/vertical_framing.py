"""Reframe visible pictures with explicit full-canvas and whole-picture layouts.

Flattened exports can contain existing letterbox/pillarbox bars. Only stable,
uniform black margins with a rectangular, lit interior are removed. Native
assets bypass this analysis and use ``vertical_filter`` directly.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

_SAMPLE_WIDTH = 192
_SAMPLE_HEIGHT = 108
_SAMPLE_FPS = 10
_BLACK = 12
_FOCUS_FPS = 2
_MAX_FOCUS_POINTS = 96


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


@lru_cache(maxsize=1)
def _face_detector():
    """Use only the cascade already shipped with OpenCV, never download one."""
    import cv2

    directory = getattr(getattr(cv2, "data", None), "haarcascades", "")
    if not directory:
        return None
    path = Path(directory) / "haarcascade_frontalface_default.xml"
    if not path.is_file():
        return None
    classifier = cv2.CascadeClassifier(str(path))
    return None if classifier.empty() else classifier


def _focus_candidate(gray, visible_fraction: float, previous: float | None = None, *, allow_faces: bool = True) -> tuple[float, bool]:
    """Prefer visible faces, otherwise a stable cluster of structural details.

    This is geometric computer vision, not semantic room/subject recognition.
    Long isolated edges (for example a doorjamb) do not count as a detail cluster.
    """
    import cv2
    import numpy as np

    height, width = gray.shape
    half = min(.5, max(.01, visible_fraction / 2))
    detector = _face_detector() if allow_faces else None
    faces = [] if detector is None else detector.detectMultiScale(
        gray, scaleFactor=1.12, minNeighbors=6, minSize=(max(22, width // 16), max(22, width // 16)),
    )
    if len(faces):
        largest = max(float(w * h) for _x, _y, w, h in faces)
        choices = [box for box in faces if float(box[2] * box[3]) >= largest * .40]
        face = max(choices, key=lambda box: float(box[2] * box[3]) / largest
                   - (abs((float(box[0]) + float(box[2]) / 2) / width - previous) if previous is not None else 0))
        center = (float(face[0]) + float(face[2]) / 2) / width
        return min(1 - half, max(half, center)), True

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    gx = np.abs(cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3))
    corners = cv2.cornerMinEigenVal(blurred, blockSize=3, ksize=3)
    junctions = np.sqrt(gx * gy)

    def normalized(values):
        positive = values[values > 0]
        scale = float(np.percentile(positive, 97)) if positive.size else 0
        return np.minimum(values / max(scale, 1e-6), 2)

    features = normalized(corners) + .35 * normalized(junctions)
    # Ignore sky/ceilings and the near foreground. Favor the region in which
    # people, fixtures, furniture, and open-room sightlines normally appear.
    weights = np.exp(-.5 * ((np.arange(height) / max(1, height - 1) - .52) / .25) ** 2)
    weights[:max(1, height // 12)] = 0
    weights[round(height * .90):] = 0
    columns = np.sum(features * weights[:, None], axis=0)
    window = max(3, min(width, int(round(visible_fraction * width))))
    kernel = cv2.getGaussianKernel(window if window % 2 else window + 1, max(1, window * .36)).reshape(-1)
    scores = np.convolve(columns, kernel, mode="same")
    positions = np.arange(width) / max(1, width - 1)
    allowed = (positions >= half) & (positions <= 1 - half)
    maximum = float(np.max(scores[allowed])) if np.any(allowed) else 0
    if maximum <= 1e-7:
        return previous if previous is not None else .5, False
    scores = scores + maximum * .035 * (1 - np.abs(positions - .5) * 2)
    if previous is not None:
        scores = scores - maximum * .08 * np.abs(positions - previous)
    scores[~allowed] = -1
    best = int(np.argmax(scores))
    center = min(width - 1, round((width - 1) / 2))
    # A walkthrough's camera usually already points at its subject. Do not
    # abandon a credible central composition for busier foliage or furnishings
    # at an edge; reframe only when the central detail score is substantially
    # weaker (for example a blank partition hiding the open-room sightline).
    if scores[center] >= scores[best] * .70:
        return .5, False
    if previous is not None:
        prior = min(width - 1, max(0, round(previous * (width - 1))))
        if scores[prior] >= scores[best] * .94:
            return previous, False
    return min(1 - half, max(half, float(positions[best]))), False


def _confirmed_focus_candidate(gray, visible_fraction: float, previous: float | None, face_history: dict) -> tuple[float, bool]:
    anchor, is_face = _focus_candidate(gray, visible_fraction, previous)
    if not is_face:
        face_history.clear()
        return anchor, False
    nearby = face_history and abs(anchor - face_history["anchor"]) <= .12
    hits = face_history.get("hits", 0) + 1 if nearby else 1
    face_history.update(anchor=anchor, hits=hits)
    if hits < 2:
        # Reflections, artwork, and equipment can produce an isolated Haar hit.
        # A face only overrides the room-detail composition after a nearby
        # detection persists into the next sample; otherwise use structure.
        return _focus_candidate(gray, visible_fraction, previous, allow_faces=False)
    return anchor, True


def _scene_changed(previous, current) -> bool:
    import cv2
    import numpy as np

    before = cv2.resize(previous, (48, 27)).astype(np.float32)
    after = cv2.resize(current, (48, 27)).astype(np.float32)
    delta = float(np.mean(np.abs(before - after))) / 255
    histograms = [cv2.calcHist([frame], [0], None, [32], [0, 256]) for frame in (previous, current)]
    histograms = [histogram / max(1, float(histogram.sum())) for histogram in histograms]
    distance = cv2.compareHist(histograms[0], histograms[1], cv2.HISTCMP_BHATTACHARYYA)
    return bool((distance > .48 and delta > .12) or (distance > .25 and delta > .35))


def _simplify_focus(points: list[tuple[float, float]], tolerance: float = .012) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    first, last = points[0], points[-1]
    span = last[0] - first[0]
    errors = [abs(point[1] - (first[1] + (last[1] - first[1]) * (point[0] - first[0]) / span)) for point in points[1:-1]]
    error = max(errors, default=0)
    if error <= tolerance:
        return [first, last]
    index = errors.index(error) + 1
    return _simplify_focus(points[:index + 1], tolerance)[:-1] + _simplify_focus(points[index:], tolerance)


def _smooth_focus_samples(samples: list[tuple[float, float, bool, bool]], duration: float) -> list[tuple[float, float]]:
    """Bound pan velocity; only a detected shot cut may jump to a new anchor."""
    if not samples:
        return [(0.0, .5), (duration, .5)]
    chunks: list[list[tuple[float, float]]] = []
    current = [(0.0, samples[0][1])]
    last_time, last_focus = current[0]
    for second, target, cut, is_face in samples[1:]:
        second = min(duration, max(last_time, float(second)))
        if cut and second > last_time:
            current.append((max(last_time + .0001, second - .001), last_focus))
            chunks.append(current)
            current = [(second, target)]
            focus = target
        else:
            delta = max(0, second - last_time)
            # Faces can move faster than a room-detail pan; neither gets noisy
            # frame-to-frame jumps from tiny changes in the feature scores.
            limit = (.12 if is_face else .075) * delta
            desired = last_focus + (target - last_focus) * .42
            focus = last_focus + min(limit, max(-limit, desired - last_focus))
            current.append((second, focus))
        last_time, last_focus = second, focus
    if current[-1][0] < duration:
        current.append((duration, last_focus))
    chunks.append(current)
    tolerance = .012
    result = [point for chunk in chunks for point in _simplify_focus(chunk, tolerance)]
    while len(result) > _MAX_FOCUS_POINTS and tolerance < .10:
        tolerance *= 1.5
        result = [point for chunk in chunks for point in _simplify_focus(chunk, tolerance)]
    # Extremely rapid edited montages cannot be represented by one bounded
    # expression. Retain bounded, validated anchors rather than parser overflow.
    if len(result) > _MAX_FOCUS_POINTS:
        indices = sorted({round(index * (len(result) - 1) / (_MAX_FOCUS_POINTS - 1)) for index in range(_MAX_FOCUS_POINTS)})
        result = [result[index] for index in indices]
    return [(round(second, 6), round(focus, 6)) for second, focus in result]


def analyze_fill_focus(
    source: Path, start: float, duration: float, *, crop: dict | None = None,
    probe: dict | Callable[[Path], dict] | None = None,
) -> list[tuple[float, float]]:
    """Return editable relative-time/horizontal-focus anchors for a fill crop.

    Uses installed OpenCV geometry/texture analysis and its bundled face cascade.
    There is no model download, external inference, or claim to understand a
    scene's meaning. Native portrait shots need no horizontal tracking. Decoder
    or dependency failures yield a declared neutral center anchor.
    """
    start, duration = float(start), float(duration)
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
        raise ValueError("Focus analysis requires a finite start and positive duration")
    try:
        import cv2
        import numpy as np

        width, height, sar = _geometry(probe(source) if callable(probe) else probe if probe is not None else _probe(source))
        crop_filter = ""
        if crop is not None:
            values = _validated_crop(crop)
            width, height = values["width"], values["height"]
            crop_filter = f"crop={width}:{height}:{values['x']}:{values['y']},"
        aspect = width * sar / height
        visible_fraction = min(1.0, (9 / 16) / aspect)
        if visible_fraction >= .98:
            return [(0.0, .5), (duration, .5)]
        sample_width = 320
        sample_height = max(32, min(576, round(sample_width / aspect / 2) * 2))
        sample_width = max(32, round(sample_height * aspect / 2) * 2)
        samples = []
        previous_frame = None
        previous_focus = None
        face_history: dict = {}
        offset = 0.0
        while offset < duration - .00001:
            chunk = min(60.0, duration - offset)
            decoded = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{start + offset:.6f}", "-i", str(source),
                 "-t", f"{chunk:.6f}", "-an", "-sn", "-dn", "-vf",
                 f"{crop_filter}fps={_FOCUS_FPS},scale={sample_width}:{sample_height}:flags=area,format=gray",
                 "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                capture_output=True, check=True, timeout=max(30.0, chunk * 3),
            )
            frame_size = sample_width * sample_height
            count = len(decoded.stdout) // frame_size
            if len(decoded.stdout) % frame_size or abs(count - chunk * _FOCUS_FPS) > 1.1:
                raise ValueError("Incomplete focus samples")
            for index in range(count):
                frame = np.frombuffer(decoded.stdout[index * frame_size:(index + 1) * frame_size], dtype=np.uint8).reshape(sample_height, sample_width)
                cut = previous_frame is not None and _scene_changed(previous_frame, frame)
                if cut:
                    face_history.clear()
                anchor, is_face = _confirmed_focus_candidate(frame, visible_fraction, None if cut else previous_focus, face_history)
                samples.append((offset + index / _FOCUS_FPS, anchor, cut, is_face))
                previous_focus, previous_frame = anchor, frame
            offset += chunk
        return _smooth_focus_samples(samples, duration)
    except (ImportError, OSError, ValueError, KeyError, StopIteration, TypeError, subprocess.SubprocessError):
        return [(0.0, .5), (duration, .5)]


def _validated_crop(crop: dict) -> dict:
    values = {key: int(crop[key]) for key in ("x", "y", "width", "height")}
    if min(values["x"], values["y"]) < 0 or min(values["width"], values["height"]) <= 0:
        raise ValueError("Invalid visible-picture crop")
    return values


def _validated_focus_points(points) -> list[tuple[float, float]]:
    if points is None:
        return [(0.0, .5)]
    if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= _MAX_FOCUS_POINTS:
        raise ValueError(f"Focus requires 1–{_MAX_FOCUS_POINTS} numeric time/position points")
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("Each focus point needs a numeric time and horizontal position")
        try:
            second, focus = map(float, point)
        except (TypeError, ValueError) as exc:
            raise ValueError("Focus points must be numeric") from exc
        if not math.isfinite(second) or not math.isfinite(focus) or not 0 <= second <= 86400 or not 0 <= focus <= 1:
            raise ValueError("Focus times and positions must be finite and inside their valid range")
        if result and second - result[-1][0] < .000001:
            raise ValueError("Focus point times must strictly increase by at least one microsecond")
        result.append((second, focus))
    return result


def _focus_expression(points) -> str:
    points = _validated_focus_points(points)
    terms = [f"{points[0][1]:.6f}"]
    for first, second in zip(points, points[1:]):
        delta = second[1] - first[1]
        if abs(delta) > .0000001:
            terms.append(f"({delta:.6f})*min(max((t-{first[0]:.6f})/{second[0] - first[0]:.6f},0),1)")
    return "+".join(terms)


def vertical_filter(
    input_label: str = "0:v", output_label: str = "v", crop: dict | None = None,
    *, width: int = 1080, height: int = 1920, fps: int = 30,
    mode: str = "fit", focus_points=None,
) -> str:
    """Return a single-canvas graph with an explicit fit or fill policy.

    Fit (the compatibility default) preserves the whole picture over soft fill.
    Fill covers the canvas without an inset and follows validated horizontal
    focus anchors; it necessarily crops wide footage. Anchor times are relative
    to this input window. Vertical alignment stays centered.
    """
    if not all(re.fullmatch(r"[A-Za-z0-9_:.-]+", label) for label in (input_label, output_label)):
        raise ValueError("Invalid video filter label")
    width, height, fps = int(width), int(height), int(fps)
    if width <= 0 or height <= 0 or fps <= 0 or width % 2 or height % 2:
        raise ValueError("Vertical canvas dimensions must be positive even integers")
    if mode not in {"fit", "fill"}:
        raise ValueError("Choose fit or fill for vertical framing")
    crop_filter = ""
    if crop is not None:
        values = _validated_crop(crop)
        crop_filter = f"crop={values['width']}:{values['height']}:{values['x']}:{values['y']},"
    if mode == "fill":
        focus = _focus_expression(focus_points)
        return (
            f"[{input_label}]{crop_filter}scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"setpts=PTS-STARTPTS,crop={width}:{height}:x='max(0,min(iw-ow,iw*({focus})-ow/2))':y='(ih-oh)/2',"
            f"setsar=1,fps={fps},format=yuv420p[{output_label}]"
        )
    prefix = re.sub(r"[^A-Za-z0-9_]", "_", output_label)
    return (
        f"[{input_label}]{crop_filter}scale=trunc(iw*sar/2)*2:ih,setsar=1,split=2[{prefix}_bg_src][{prefix}_fg_src];"
        f"[{prefix}_bg_src]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=20:10[{prefix}_bg];"
        f"[{prefix}_fg_src]scale={width}:{height}:force_original_aspect_ratio=decrease[{prefix}_fg];"
        f"[{prefix}_bg][{prefix}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[{output_label}]"
    )
