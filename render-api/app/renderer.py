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
DEFAULT_XFADE = float(os.getenv("DEFAULT_XFADE", "0.5"))

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

    project_root = storage_root / "projects" / pid
    input_dir = project_root / "input"
    work_dir = project_root / "work"
    output_dir = project_root / "output"
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
    xfade = float(opts.get("xfade", DEFAULT_XFADE))
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
            filter_complex = (
                f"[0:v]scale=1920:1080,format=yuv420p,"
                f"zoompan=z='min(zoom+0.0008,1.05)':d={frames}:s=1920x1080:fps={fps}"
                f"[z];[z]fade=t=in:st=0:d=0.4,"
                f"fade=t=out:st={max(0.0, per_image - xfade):.3f}:d={xfade:.3f}[v]"
            )
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-t",
                    f"{per_image:.3f}",
                    "-i",
                    str(image_path),
                    "-filter_complex",
                    filter_complex,
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
            segment_paths.append(segment)

        concat_list = temp_dir / "list.txt"
        concat_list.write_text(
            "".join(f"file '{path.as_posix()}'\n" for path in segment_paths),
            encoding="utf-8",
        )

        update(f"SCENE_BUILD[{idx}]", 0.2 + (index / max(1, len(scenes))) * 0.6)
        silent_scene = temp_dir / "scene_silent.mp4"
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
                "-c",
                "copy",
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

    master_list = work_dir / "master.txt"
    master_list.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in scene_files),
        encoding="utf-8",
    )

    update("CONCAT", 0.9)
    final_path = output_dir / output_name
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(master_list),
            "-c",
            "copy",
            str(final_path),
        ],
        log,
    )

    update("FINALIZE", 0.98)
    return final_path
