"""Sextant-owned adapter for the model-only ComfyUI runtime on Fortress LAN."""
from __future__ import annotations

import json
import os
import random
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import requests

COMFYUI_MODEL_API_URL = os.getenv("COMFYUI_MODEL_API_URL", "http://100.100.97.30:8188")
COMFYUI_TIMEOUT_SECONDS = float(os.getenv("COMFYUI_TIMEOUT_SECONDS", "7200"))
COMFYUI_POLL_SECONDS = float(os.getenv("COMFYUI_POLL_SECONDS", "5"))
WORKFLOW_PATH = Path(__file__).resolve().parent / "workflows" / "wan2_2_ti2v_5b_api.json"


class MotionProviderError(RuntimeError):
    pass


def extract_last_frame(video_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-sseof", "-0.08", "-i", str(video_path),
        "-frames:v", "1", "-vf", "scale=1024:576:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1024:576:(ow-iw)/2:(oh-ih)/2", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to carry the previous scene into the next scene: {exc}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Previous scene did not yield a usable final frame")


def _set_path(document: dict[str, Any], dotted_path: str, value: Any) -> None:
    target: Any = document
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    final = parts[-1]
    if isinstance(target, list):
        target[int(final)] = value
    else:
        target[final] = value


def _upload_image(session, image_path: Path) -> str:
    with image_path.open("rb") as handle:
        response = session.post(
            f"{COMFYUI_MODEL_API_URL.rstrip('/')}/upload/image",
            files={"image": (image_path.name, handle, "image/png")},
            data={"overwrite": "true"},
            timeout=120,
        )
    response.raise_for_status()
    return str(response.json().get("name") or image_path.name)


def _patched_workflow(image_name: str, prompt: str, negative_prompt: str, filename_prefix: str) -> dict:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    values = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "uploaded_image_name": image_name,
        "width": 576,
        "height": 320,
        "frames": 81,
        "fps": 16,
        "seed": random.randint(1, 2**63 - 1),
        "steps": 20,
        "cfg": 3.5,
        "sampler_name": "uni_pc",
        "scheduler": "simple",
        "denoise": 1,
        "model_shift": 8,
        "filename_prefix": filename_prefix,
    }
    patches = {
        "6.inputs.text": "prompt",
        "7.inputs.text": "negative_prompt",
        "56.inputs.image": "uploaded_image_name",
        "55.inputs.width": "width",
        "55.inputs.height": "height",
        "55.inputs.length": "frames",
        "57.inputs.fps": "fps",
        "3.inputs.seed": "seed",
        "3.inputs.steps": "steps",
        "3.inputs.cfg": "cfg",
        "3.inputs.sampler_name": "sampler_name",
        "3.inputs.scheduler": "scheduler",
        "3.inputs.denoise": "denoise",
        "48.inputs.shift": "model_shift",
        "58.inputs.filename_prefix": "filename_prefix",
    }
    for path, key in patches.items():
        _set_path(workflow, path, values[key])
    return workflow


def _wait_for_output(session, prompt_id: str) -> dict:
    deadline = time.monotonic() + COMFYUI_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = session.get(
            f"{COMFYUI_MODEL_API_URL.rstrip('/')}/history/{prompt_id}", timeout=30
        )
        response.raise_for_status()
        entry = response.json().get(prompt_id)
        if entry:
            return entry
        time.sleep(max(0.25, COMFYUI_POLL_SECONDS))
    raise MotionProviderError(f"ComfyUI prompt {prompt_id} timed out")


def _output_record(history: dict) -> dict:
    for node in history.get("outputs", {}).values():
        for kind in ("videos", "gifs", "images"):
            for item in node.get(kind, []):
                if item.get("filename"):
                    return item
    raise MotionProviderError("ComfyUI completed without a downloadable motion artifact")


def _verify_video(path: Path) -> None:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,nb_frames:format=duration",
        "-of", "json", str(path),
    ]
    try:
        payload = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise MotionProviderError(f"Unable to validate motion artifact: {exc}") from exc
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    frames = int(stream.get("nb_frames") or 0)
    codec = str(stream.get("codec_name") or "")
    failures = []
    if duration < 4.5:
        failures.append(f"duration {duration:.2f}s")
    if int(stream.get("width") or 0) < 448 or int(stream.get("height") or 0) < 256:
        failures.append("resolution below 448x256")
    if frames and frames < 25:
        failures.append(f"only {frames} frames")
    if codec not in {"h264", "hevc", "vp9", "av1"}:
        failures.append(f"unsupported codec {codec or 'unknown'}")
    if failures:
        raise MotionProviderError("Motion artifact failed hard gates: " + ", ".join(failures))


def generate_motion_clip(
    image_path: Path,
    destination: Path,
    *,
    prompt: str,
    negative_prompt: str,
    session=requests,
) -> None:
    model_health = session.get(f"{COMFYUI_MODEL_API_URL.rstrip('/')}/system_stats", timeout=10)
    model_health.raise_for_status()
    uploaded_name = _upload_image(session, image_path)
    prefix = f"mediastudio/{uuid.uuid4().hex}"
    workflow = _patched_workflow(uploaded_name, prompt, negative_prompt, prefix)
    queued = session.post(
        f"{COMFYUI_MODEL_API_URL.rstrip('/')}/prompt",
        json={"client_id": f"mediastudio-sextant-{uuid.uuid4().hex}", "prompt": workflow},
        timeout=60,
    )
    queued.raise_for_status()
    prompt_id = str(queued.json().get("prompt_id") or "")
    if not prompt_id:
        raise MotionProviderError("ComfyUI did not return a prompt id")
    item = _output_record(_wait_for_output(session, prompt_id))
    artifact = session.get(
        f"{COMFYUI_MODEL_API_URL.rstrip('/')}/view",
        params={
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        },
        timeout=300,
    )
    artifact.raise_for_status()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(artifact.content)
    _verify_video(destination)
