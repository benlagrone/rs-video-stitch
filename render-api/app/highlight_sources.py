"""Bind vertical highlight visuals to the native assets used by a saved master.

Retained proxies supply timing, but timing and filenames alone are not proof
that they belong to the selected MP4. A native shot is exposed only after its
pixels match its proxy and that proxy matches the selected master's pixels.
Unverified regions remain master-video regions for the framing fallback.
"""
from __future__ import annotations

import math
import statistics
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.storage import p_input, p_work


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns)}


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    value = float(result.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("A retained visual has no usable duration")
    return value


@lru_cache(maxsize=256)
def _frame(filename: str, size: int, mtime_ns: str, second: float) -> bytes:
    # Canonical 16:9 fitting reproduces the master's aspect-preserving fit.
    # Ignore the outer bands, where logos and optional photo labels live.
    # Image demuxers in the deployed FFmpeg can produce zero frames even for
    # an input-side seek to zero. A still has no timeline to seek.
    still = Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff"}
    seek = [] if still else ["-ss", f"{max(0.0, second):.6f}"]
    result = subprocess.run(
        ["ffmpeg", "-v", "error", *seek, "-i", filename,
         "-frames:v", "1", "-vf",
         "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,scale=160:90:flags=area,crop=128:54:16:18",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, timeout=30, check=True,
    )
    if len(result.stdout) != 128 * 54 * 3:
        raise ValueError("No complete verification frame was decoded")
    return result.stdout


def _sample(path: Path, second: float) -> bytes:
    fingerprint = _fingerprint(path)
    return _frame(str(path.resolve()), fingerprint["size"], fingerprint["mtimeNs"], round(second, 6))


def _similar(left: bytes, right: bytes) -> bool:
    if len(left) != len(right) or not left or len(left) % 3:
        return False
    # Blank/flat frames cannot establish that two video versions are the same.
    luminance = [(left[i] + left[i + 1] + left[i + 2]) / 3 for i in range(0, len(left), 3)]
    if statistics.pstdev(luminance) < 2.0 or max(luminance) - min(luminance) < 12:
        return False
    differences = [abs(a - b) for a, b in zip(left, right)]
    return sum(differences) / len(differences) <= 8.0 and sum(value > 24 for value in differences) / len(differences) <= 0.08


def _visual_match(master: Path, item: dict) -> bool:
    """Check the selected master, the retained proxy, and the current asset."""
    try:
        asset = item["assetPath"]
        proxy = item["proxyPath"]
        seconds = item["sourceEnd"] - item["sourceStart"]
        offsets = [seconds * 0.5] if item["kind"] == "image" else [seconds * 0.27, seconds * 0.73]
        for offset in offsets:
            master_pixels = _sample(master, item["sourceStart"] + offset)
            proxy_pixels = _sample(proxy, offset * item.get("proxyTimeScale", 1.0))
            asset_pixels = _sample(asset, item.get("assetStart", 0.0) + offset * item["assetTimeScale"])
            if not (_similar(master_pixels, proxy_pixels) and _similar(proxy_pixels, asset_pixels)):
                return False
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _prepared_windows(master: Path, item: dict, windows: list[tuple[float, float]]) -> list[dict]:
    """Expose only exact-master-verified proxy windows, without a raw claim.

    A retained constant-frame-rate clip can differ by a frame from its phone
    original. Its pixels are still safe when they independently match the
    selected finished master at both samples inside each requested window.
    """
    proxy = item["proxyPath"]
    rate = item.get("proxyTimeScale", 1.0)
    verified = []
    try:
        before = _fingerprint(proxy)
        for start, end in windows:
            left, right = max(start, item["sourceStart"]), min(end, item["sourceEnd"])
            if right <= left:
                continue
            offset = (left - item["sourceStart"]) * rate
            for fraction in (0.27, 0.73):
                delta = (right - left) * fraction
                if not _similar(_sample(master, left + delta), _sample(proxy, offset + delta * rate)):
                    break
            else:
                verified.append({"sourceStart": left, "sourceEnd": right,
                    "assetPath": proxy, "kind": "video", "assetStart": offset, "assetTimeScale": rate,
                    "proxyPath": proxy, "proxyTimeScale": rate, "fingerprint": before,
                    "sceneIndex": item["sceneIndex"], "sceneTitle": item["sceneTitle"],
                    "nativeVerified": False, "binding": "master-verified-prepared-proxy"})
        return verified if _fingerprint(proxy) == before else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _asset(root: Path, name: object) -> Path | None:
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name:
        return None
    path = root / name
    if not path.is_file() or path.is_symlink() or path.resolve().parent != root.resolve():
        return None
    return path


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.is_file() and not path.is_symlink()), None)


def native_source_map(
    project_id: str,
    master: Path,
    document: dict,
    state: dict,
    probe_duration: Callable[[Path], float] | None = None,
    *,
    windows: list[tuple[float, float]] | None = None,
    allow_prepared_proxy: bool = False,
) -> list[dict]:
    """Return only visually verified native shots in absolute master seconds.

    ``windows`` can restrict frame verification to the selected highlight beats.
    Every returned asset includes its file fingerprint for the caller's final
    unchanged-input check. Native video time is master delta times
    ``assetTimeScale``; image time is always zero.

    Highlights may opt into independently master-verified prepared video
    windows. Those entries explicitly set ``nativeVerified=False`` and expose
    the prepared file as their asset, without attributing pixels to a raw file.
    """
    duration = probe_duration or _duration
    work, inputs = p_work(project_id), p_input(project_id)
    footage = document.get("sourceFootage") or []
    scenes = document.get("scenes") or []
    if not master.is_file() or master.is_symlink() or not (footage or scenes):
        return []
    try:
        total = float(duration(master))
        if not math.isfinite(total) or total <= 0:
            return []
        options = state.get("renderOptions") or {}
        cursor = 0.0
        if options.get("introEnabled", state.get("useIntro", False)):
            intro = _first_existing([work / "hyperframes_intro.mp4", work / "intro.mp4"])
            if intro is None:
                return []
            cursor = float(duration(intro))
        candidates: list[dict] = []
        entries = footage or scenes
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return []
            proxy = work / "footage" / f"clip_{index:02d}.mp4" if footage else work / f"scene_{index:02d}.mp4"
            if not proxy.is_file() or proxy.is_symlink():
                return []
            scene_duration = float(duration(proxy))
            if not math.isfinite(scene_duration) or scene_duration <= 0:
                return []
            title = str(entry.get("title") or "").strip()
            if footage:
                native = _asset(inputs / "footage", entry.get("filename") or entry.get("name"))
                if native:
                    candidates.append({"sourceStart": cursor, "sourceEnd": cursor + scene_duration,
                        "assetPath": native, "kind": "video", "assetStart": 0.0,
                        "assetTimeScale": float(duration(native)) / scene_duration,
                        "proxyPath": proxy, "proxyTimeScale": 1.0,
                        "sceneIndex": index, "sceneTitle": title, "fingerprint": _fingerprint(native)})
            else:
                images = entry.get("images") or []
                proxies = [work / f"scene_{index:02d}" / f"seg_{photo_index:02d}.mp4" for photo_index in range(len(images))]
                if images and all(path.is_file() and not path.is_symlink() for path in proxies):
                    shot_durations = [float(duration(path)) for path in proxies]
                    visual_duration = sum(shot_durations)
                    # AAC padding may add a frame to a scene; a large mismatch
                    # instead means these intermediates describe another edit.
                    if visual_duration > 0 and abs(visual_duration - scene_duration) <= 0.5:
                        scene_cursor = cursor
                        timeline = {item.get("image"): item for item in entry.get("timeline", []) if isinstance(item, dict)}
                        for name, shot_proxy, shot_duration in zip(images, proxies, shot_durations):
                            length = shot_duration * scene_duration / visual_duration
                            timeline_entry = timeline.get(name) or {}
                            motion = timeline_entry.get("video")
                            native = _asset(inputs / ("motion" if motion else "images"), motion or name)
                            if native:
                                kind = "video" if motion else "image"
                                candidates.append({"sourceStart": scene_cursor, "sourceEnd": scene_cursor + length,
                                    "assetPath": native, "kind": kind, "assetStart": 0.0,
                                    "assetTimeScale": float(duration(native)) / length if motion else 0.0,
                                    "proxyPath": shot_proxy, "proxyTimeScale": shot_duration / length,
                                    "sceneIndex": index, "sceneTitle": title, "fingerprint": _fingerprint(native)})
                            scene_cursor += length
            cursor += scene_duration
            if index < len(entries) - 1:
                names = [f"hyperframes_footage_transition_{index:02d}.mp4"] if footage else [
                    f"hyperframes_scene_transition_{index:02d}.mp4", f"hyperframes_transition_{index:02d}.mp4"]
                transition = _first_existing([work / name for name in names])
                if transition:
                    cursor += float(duration(transition))
        if not math.isfinite(cursor) or abs(cursor - total) > 1.0:
            return []
        verified = []
        for item in candidates:
            if windows is not None and not any(item["sourceStart"] < end and item["sourceEnd"] > start for start, end in windows):
                continue
            if _visual_match(master, item) and _fingerprint(item["assetPath"]) == item["fingerprint"]:
                verified.append({**item, "nativeVerified": True})
            elif allow_prepared_proxy and windows and item["kind"] == "video":
                verified.extend(_prepared_windows(master, item, windows))
        return verified
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return []


def split_window(mapping: list[dict], start: float, end: float) -> list[dict]:
    """Cover a selected range completely, preserving master audio coordinates."""
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
        raise ValueError("A source window needs a finite positive duration")
    result: list[dict] = []
    cursor = start
    for item in sorted(mapping, key=lambda region: region["sourceStart"]):
        left, right = max(start, item["sourceStart"]), min(end, item["sourceEnd"])
        if right <= left or right <= cursor:
            continue
        left = max(left, cursor)
        if left > cursor:
            result.append({"sourceStart": cursor, "sourceEnd": left, "duration": left - cursor, "kind": "master", "assetStart": cursor})
        result.append({**item, "sourceStart": left, "sourceEnd": right, "duration": right - left,
            "assetStart": item.get("assetStart", 0.0) + (left - item["sourceStart"]) * item.get("assetTimeScale", 0.0)})
        cursor = right
    if cursor < end:
        result.append({"sourceStart": cursor, "sourceEnd": end, "duration": end - cursor, "kind": "master", "assetStart": cursor})
    return result
