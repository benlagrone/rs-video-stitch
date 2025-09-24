"""Rendering pipeline built around FFmpeg."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from app.tts import TTSConfigurationError, synthesize_xtts

DEFAULT_PRESET = os.getenv("DEFAULT_PRESET", "medium")
DEFAULT_CRF = int(os.getenv("DEFAULT_CRF", "18"))
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_MIN_SHOT = float(os.getenv("DEFAULT_MIN_SHOT", "2.5"))
DEFAULT_MAX_SHOT = float(os.getenv("DEFAULT_MAX_SHOT", "8.0"))

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

    scenes = json.loads(scenes_path.read_text("utf-8")).get("scenes", [])
    if not scenes:
        raise ValueError("No scenes defined in scenes.json")

    fps = int(opts.get("fps", DEFAULT_FPS))
    min_shot = float(opts.get("minShot", DEFAULT_MIN_SHOT))
    max_shot = float(opts.get("maxShot", DEFAULT_MAX_SHOT))
    preset = opts.get("preset", DEFAULT_PRESET)
    crf = str(opts.get("crf", DEFAULT_CRF))
    voice_dir = opts.get("voiceDir")
    tts_voice = opts.get("tts")
    tts_language = opts.get("ttsLanguage")

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
            if tts_voice and voice_text.strip():
                try:
                    synthesize_xtts(
                        voice_text,
                        audio_wav,
                        voice=tts_voice,
                        language=tts_language,
                        log=log,
                    )
                except TTSConfigurationError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"xTTS synthesis failed for scene {idx}: {exc}") from exc
            else:
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
        per_image = max(
            min_shot,
            min(max_shot, (max(1.0, audio_duration - 0.4)) / max(1, len(images))),
        )

        temp_dir = work_dir / f"scene_{idx}"
        temp_dir.mkdir(exist_ok=True)
        segment_paths = []

        for img_index, image_name in enumerate(images):
            image_path = input_dir / "images" / image_name
            if not image_path.exists():
                raise FileNotFoundError(f"Missing image for scene {idx}: {image_name}")
            frames = max(1, round(per_image * fps))
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
                    seconds=per_image,
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
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(silent_scene),
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
