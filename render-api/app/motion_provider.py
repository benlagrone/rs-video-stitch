"""Sextant-owned adapter for the model-only ComfyUI runtime on Fortress LAN."""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests

COMFYUI_MODEL_API_URL = os.getenv("COMFYUI_MODEL_API_URL", "http://100.100.97.30:8188")
STABLE_DIFFUSION_API_URL = os.getenv(
    "STABLE_DIFFUSION_API_URL", "http://100.100.97.30:7861/sdapi/v1/txt2img"
)
BACKGROUND_PLATE_API_URL = STABLE_DIFFUSION_API_URL.replace("/txt2img", "/img2img")
IMAGE_INTERROGATE_API_URL = STABLE_DIFFUSION_API_URL.replace("/txt2img", "/interrogate")
COMFYUI_TIMEOUT_SECONDS = float(os.getenv("COMFYUI_TIMEOUT_SECONDS", "7200"))
COMFYUI_POLL_SECONDS = float(os.getenv("COMFYUI_POLL_SECONDS", "5"))
WORKFLOW_PATH = Path(__file__).resolve().parent / "workflows" / "wan2_2_ti2v_5b_api.json"
OBJECT_VECTOR_PROVIDER = "sextant-object-vector-v1"
LTX_KEYFRAME_CHECKPOINT = os.getenv(
    "LTX_KEYFRAME_CHECKPOINT",
    "ltxv-2b-0.9.8-distilled-fp8.safetensors",
)
OBJECT_MOTION_CORRIDOR_EXPANSION = 40
OBJECT_MOTION_CORRIDOR_FEATHER = 14.0
FRAME_PROTECTION_WIDTH = 576
FRAME_PROTECTION_HEIGHT = 320
FRAME_PROTECTION_X = 69
FRAME_PROTECTION_Y = 45
FRAME_PROTECTION_FEATHER = 4
LOCKED_EDGE_PROTECTION = 24
LOCKED_EDGE_FEATHER = 6
LOCKED_CAMERA_P95_TRANSLATION_LIMIT = float(os.getenv("LOCKED_CAMERA_P95_TRANSLATION_LIMIT", "12"))
LOCKED_CAMERA_LARGE_CORRECTION_RATIO = float(os.getenv("LOCKED_CAMERA_LARGE_CORRECTION_RATIO", "0.20"))
SOURCE_FRAME_MIN_SSIM = float(os.getenv("SOURCE_FRAME_MIN_SSIM", "0.28"))
# Wan22ImageToVideoLatent produces a conditioned noise latent, not a finished source frame.
# It requires a full diffusion pass; partial denoise leaves static residue and colored latent blocks.
LOCKED_CAMERA_DENOISE = float(os.getenv("LOCKED_CAMERA_DENOISE", "1.0"))
SEQUENCE_SATURATION_JUMP_LIMIT = float(os.getenv("SEQUENCE_SATURATION_JUMP_LIMIT", "6.0"))
SEQUENCE_LUMA_JUMP_LIMIT = float(os.getenv("SEQUENCE_LUMA_JUMP_LIMIT", "20.0"))
SEQUENCE_MAX_LUMA_FRAME_DIFFERENCE = float(os.getenv("SEQUENCE_MAX_LUMA_FRAME_DIFFERENCE", "26.0"))
SEQUENCE_MIN_MEAN_LUMA_DIFFERENCE = float(os.getenv("SEQUENCE_MIN_MEAN_LUMA_DIFFERENCE", "0.35"))
GENERATIVE_MIN_MEAN_LUMA_DIFFERENCE = float(os.getenv("GENERATIVE_MIN_MEAN_LUMA_DIFFERENCE", "0.9"))
SEQUENCE_EDGE_TILE_SATURATION_JUMP_LIMIT = float(
    os.getenv("SEQUENCE_EDGE_TILE_SATURATION_JUMP_LIMIT", "4.0")
)
SEQUENCE_EDGE_TILE_LUMA_DIFFERENCE_LIMIT = float(
    os.getenv("SEQUENCE_EDGE_TILE_LUMA_DIFFERENCE_LIMIT", "12.0")
)
SEQUENCE_EDGE_TILE_ABSOLUTE_SATURATION_LIMIT = float(
    os.getenv("SEQUENCE_EDGE_TILE_ABSOLUTE_SATURATION_LIMIT", "7.0")
)


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


def _extract_motion_keyframe(video_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-sseof", "-0.08",
        "-i", str(video_path), "-frames:v", "1", "-vf",
        f"scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to extract the object-vector ending keyframe: {exc}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Object-vector tween did not yield an ending keyframe")


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
    return _upload_asset(session, image_path, "image/png")


def _upload_asset(session, asset_path: Path, content_type: str) -> str:
    with asset_path.open("rb") as handle:
        response = session.post(
            f"{COMFYUI_MODEL_API_URL.rstrip('/')}/upload/image",
            files={"image": (asset_path.name, handle, content_type)},
            data={"overwrite": "true"},
            timeout=120,
        )
    response.raise_for_status()
    return str(response.json().get("name") or asset_path.name)


def _patched_workflow(
    image_name: str,
    prompt: str,
    negative_prompt: str,
    filename_prefix: str,
    seed: int | None = None,
    denoise: float = 1.0,
) -> dict:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    values = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "uploaded_image_name": image_name,
        "width": 576,
        "height": 320,
        "frames": 81,
        "fps": 16,
        "seed": seed if seed is not None else random.randint(1, 2**63 - 1),
        "steps": 20,
        "cfg": 3.5,
        "sampler_name": "uni_pc",
        "scheduler": "simple",
        "denoise": denoise,
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


def _svd_fallback_workflow(image_name: str, prefix: str, seed: int) -> dict[str, Any]:
    return {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": "svd.safetensors"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "3": {
            "class_type": "SVD_img2vid_Conditioning",
            "inputs": {
                "clip_vision": ["1", 1],
                "init_image": ["2", 0],
                "vae": ["1", 2],
                "width": FRAME_PROTECTION_WIDTH,
                "height": FRAME_PROTECTION_HEIGHT,
                "video_frames": 25,
                "motion_bucket_id": 45,
                "fps": 5,
                "augmentation_level": 0.0,
            },
        },
        "4": {"class_type": "VideoLinearCFGGuidance", "inputs": {"model": ["1", 0], "min_cfg": 1.0}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "seed": seed,
                "steps": 20,
                "cfg": 2.5,
                "sampler_name": "euler",
                "scheduler": "karras",
                "positive": ["3", 0],
                "negative": ["3", 1],
                "latent_image": ["3", 2],
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "CreateVideo", "inputs": {"images": ["6", 0], "fps": 5}},
        "8": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["7", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"},
        },
    }


def _vace_region_workflow(
    image_name: str,
    control_video_name: str,
    mask_video_name: str,
    prompt: str,
    negative_prompt: str,
    prefix: str,
    seed: int,
    strength: float = 1.0,
) -> dict[str, Any]:
    """Build a Wan VACE graph using an explicit full-frame control track and motion mask."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.1_vace_1.3B_fp16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.1_vae.pth"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt, "clip": ["2", 0]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "7": {"class_type": "LoadVideo", "inputs": {"file": control_video_name}},
        "8": {"class_type": "GetVideoComponents", "inputs": {"video": ["7", 0]}},
        "9": {"class_type": "LoadVideo", "inputs": {"file": mask_video_name}},
        "10": {"class_type": "GetVideoComponents", "inputs": {"video": ["9", 0]}},
        "11": {"class_type": "ImageToMask", "inputs": {"image": ["10", 0], "channel": "red"}},
        "12": {
            "class_type": "WanVaceToVideo",
            "inputs": {
                "positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0],
                "width": FRAME_PROTECTION_WIDTH, "height": FRAME_PROTECTION_HEIGHT,
                "length": 81, "batch_size": 1, "strength": strength,
                "control_video": ["8", 0], "control_masks": ["11", 0], "reference_image": ["6", 0],
            },
        },
        "13": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "14": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["13", 0], "seed": seed, "steps": 20, "cfg": 3.5,
                "sampler_name": "uni_pc", "scheduler": "simple",
                "positive": ["12", 0], "negative": ["12", 1], "latent_image": ["12", 2], "denoise": 1.0,
            },
        },
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "CreateVideo", "inputs": {"images": ["15", 0], "fps": 16}},
        "17": {"class_type": "SaveVideo", "inputs": {"video": ["16", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"}},
    }


def _ltx_keyframe_workflow(
    start_image_name: str,
    end_image_name: str,
    prompt: str,
    negative_prompt: str,
    prefix: str,
    seed: int,
) -> dict[str, Any]:
    """Generate real motion between the deterministic tween's exact endpoint frames."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": LTX_KEYFRAME_CHECKPOINT,
        }},
        "2": {"class_type": "LoadImage", "inputs": {"image": start_image_name}},
        "3": {"class_type": "LoadImage", "inputs": {"image": end_image_name}},
        "4": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "t5xxl_fp16.safetensors", "type": "ltxv", "device": "cpu",
        }},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {
            "text": negative_prompt, "clip": ["4", 0],
        }},
        "7": {"class_type": "LTXVConditioning", "inputs": {
            "positive": ["5", 0], "negative": ["6", 0], "frame_rate": 16,
        }},
        "8": {"class_type": "EmptyLTXVLatentVideo", "inputs": {
            "width": FRAME_PROTECTION_WIDTH, "height": FRAME_PROTECTION_HEIGHT,
            # This checkpoint decodes 16 more frames than its latent length.
            # A 65-frame latent therefore yields the required 81-frame, 5.0625s clip.
            "length": 65, "batch_size": 1,
        }},
        "9": {"class_type": "LTXVAddGuide", "inputs": {
            "positive": ["7", 0], "negative": ["7", 1], "vae": ["1", 2],
            "latent": ["8", 0], "image": ["2", 0], "frame_idx": 0, "strength": 1.0,
        }},
        "10": {"class_type": "LTXVAddGuide", "inputs": {
            "positive": ["9", 0], "negative": ["9", 1], "vae": ["1", 2],
            "latent": ["9", 2], "image": ["3", 0], "frame_idx": 64, "strength": 1.0,
        }},
        "11": {"class_type": "ModelSamplingLTXV", "inputs": {
            "model": ["1", 0], "max_shift": 2.05, "base_shift": 0.95, "latent": ["10", 2],
        }},
        "12": {"class_type": "KSampler", "inputs": {
            "model": ["11", 0], "seed": seed, "steps": 20, "cfg": 3.0,
            "sampler_name": "euler", "scheduler": "normal", "positive": ["10", 0],
            "negative": ["10", 1], "latent_image": ["10", 2], "denoise": 1.0,
        }},
        "13": {"class_type": "VAEDecode", "inputs": {
            "samples": ["12", 0], "vae": ["1", 2],
        }},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": 16}},
        "15": {"class_type": "SaveVideo", "inputs": {
            "video": ["14", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264",
        }},
    }


def _region_motion_offset(region: dict[str, Any], index: int) -> tuple[str, str]:
    strength = min(1.0, max(0.1, float(region.get("strength") or 0.5)))
    distance = round(5 + (strength * 15), 2)
    direction = str(region.get("direction") or "right")
    wave = "sin(2*PI*t/5.0625)"
    mapping = {
        "left": (f"-{distance}*t/5.0625", f"2*{wave}"),
        "right": (f"{distance}*t/5.0625", f"2*{wave}"),
        "up": (f"2*{wave}", f"-{distance}*t/5.0625"),
        "down": (f"2*{wave}", f"{distance}*t/5.0625"),
        "outward": (f"{distance}*{wave}", f"{distance / 2}*sin(PI*t/5.0625)"),
        "clockwise": (f"{distance}*sin(2*PI*t/5.0625)", f"{distance}*(1-cos(2*PI*t/5.0625))"),
        "counterclockwise": (f"-{distance}*sin(2*PI*t/5.0625)", f"{distance}*(1-cos(2*PI*t/5.0625))"),
        "pulse": ("0", f"2*{wave}"),
    }
    return mapping.get(direction, mapping["right"])


def _generate_region_control_assets(
    image_path: Path,
    motion_plan: dict[str, Any],
    control_destination: Path,
    mask_destination: Path,
    *,
    control_source: Path | None = None,
    mask_source: Path | None = None,
) -> int:
    """Create VACE inpaint tracks over a fixed still or deterministic object-vector control track."""
    regions = [region for region in motion_plan.get("regions") or [] if region.get("enabled") is not False][:5]
    if not regions:
        raise MotionProviderError("Motion plan has no enabled regions")
    mask_terms = []
    has_temporal_mask = False
    for region in regions:
        box = region.get("box") or {}
        x = max(0, min(FRAME_PROTECTION_WIDTH - 24, round(float(box.get("x", 0.1)) * FRAME_PROTECTION_WIDTH)))
        y = max(0, min(FRAME_PROTECTION_HEIGHT - 24, round(float(box.get("y", 0.1)) * FRAME_PROTECTION_HEIGHT)))
        width = max(24, min(FRAME_PROTECTION_WIDTH - x, round(float(box.get("width", 0.35)) * FRAME_PROTECTION_WIDTH)))
        height = max(24, min(FRAME_PROTECTION_HEIGHT - y, round(float(box.get("height", 0.35)) * FRAME_PROTECTION_HEIGHT)))
        center_x, center_y = x + (width / 2), y + (height / 2)
        if control_source and region.get("method") == "object-vector":
            vector = region.get("vector") or {}
            dx = round(float(vector.get("dx", 0.0)) * FRAME_PROTECTION_WIDTH, 3)
            dy = round(float(vector.get("dy", 0.0)) * FRAME_PROTECTION_HEIGHT, 3)
            easing = _vector_easing_expression(
                str(region.get("easing") or "ease-in-out"), 5.0625, "T"
            )
            center_x = f"({center_x}+({dx})*({easing}))"
            center_y = f"({center_y}+({dy})*({easing}))"
            has_temporal_mask = has_temporal_mask or abs(dx) >= 1.0 or abs(dy) >= 1.0
        spread_x, spread_y = max(18, width / 2.8), max(18, height / 2.8)
        mask_terms.append(
            f"255*exp(-(((X-{center_x})*(X-{center_x})/(2*{spread_x}*{spread_x}))"
            f"+((Y-{center_y})*(Y-{center_y})/(2*{spread_y}*{spread_y}))))"
        )
    mask_expression = mask_terms[0]
    for term in mask_terms[1:]:
        mask_expression = f"max({mask_expression},{term})"
    mask_filter = f"format=gray,geq=lum='{mask_expression}',boxblur=8:1,format=yuv420p"
    if mask_source:
        mask_command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(mask_source),
            "-t", "5.0625", "-r", "16", "-c:v", "libx264", "-preset", "fast",
            "-crf", "12", "-pix_fmt", "yuv420p", str(mask_destination),
        ]
        has_temporal_mask = True
    else:
        mask_command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            f"color=black:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d=5.0625",
            "-vf", mask_filter, "-t", "5.0625", "-r", "16", "-c:v", "libx264", "-preset", "fast",
            "-crf", "12", "-pix_fmt", "yuv420p", str(mask_destination),
        ]
    control_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "format=yuv420p[source];"
        "[1:v]format=gray[mask];"
        "[2:v]format=yuv420p[neutral];"
        "[source][neutral][mask]maskedmerge,format=yuv420p[control]"
    )
    control_input = ["-i", str(control_source)] if control_source else [
        "-loop", "1", "-framerate", "16", "-i", str(image_path)
    ]
    control_command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *control_input,
        "-i", str(mask_destination), "-f", "lavfi", "-i",
        f"color=gray:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d=5.0625",
        "-filter_complex", control_graph, "-map", "[control]", "-t", "5.0625", "-r", "16",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(control_destination),
    ]
    try:
        subprocess.run(mask_command, check=True, capture_output=True, text=True)
        subprocess.run(control_command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise MotionProviderError(f"Unable to build region-control tracks: {detail[-600:]}") from exc
    if not control_destination.exists() or not mask_destination.exists():
        raise MotionProviderError("Region-control track generation produced no video")
    if control_source is None:
        _verify_static_control_track(control_destination, "control")
    if has_temporal_mask:
        _verify_temporal_control_track(mask_destination, "mask")
    else:
        _verify_static_control_track(mask_destination, "mask")
    return len(regions)


def _verify_static_control_track(path: Path, label: str) -> None:
    """Reject temporal control tracks so VACE cannot be driven by translated source patches."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to validate the VACE {label} track: {exc}") from exc
    frame_differences = [
        float(line.split("=", 1)[1])
        for line in completed.stdout.splitlines()
        if line.startswith("lavfi.signalstats.YDIF=")
    ]
    if not frame_differences:
        raise MotionProviderError(f"Unable to measure temporal movement in the VACE {label} track")
    if max(frame_differences) > 0.05:
        raise MotionProviderError(
            f"VACE {label} track contains temporal image movement; translated source patches are prohibited"
        )


def _verify_temporal_control_track(path: Path, label: str) -> None:
    """Require an object-vector mask to carry a measurable trajectory."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to validate the VACE {label} trajectory: {exc}") from exc
    frame_differences = [
        float(line.split("=", 1)[1])
        for line in completed.stdout.splitlines()
        if line.startswith("lavfi.signalstats.YDIF=")
    ]
    if not frame_differences or max(frame_differences) <= 0.05:
        raise MotionProviderError(f"VACE {label} trajectory contains no measurable movement")


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


def _queue_and_download_workflow(session, workflow: dict[str, Any], destination: Path) -> None:
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


def _prepare_source_image(image_path: Path, destination: Path) -> None:
    command = [
        "ffmpeg", "-y", "-i", str(image_path), "-frames:v", "1", "-sws_flags", "lanczos",
        "-vf", (
            f"scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black"
        ),
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to prepare a crop-safe source frame: {exc}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Crop-safe source preparation produced no image")


def _parse_deshake_log(path: Path) -> dict[str, float | int]:
    translations: list[float] = []
    large_corrections = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            try:
                values = [float(value.strip()) for value in line.split(",")]
                magnitude = (values[0] ** 2 + values[3] ** 2) ** 0.5
            except (ValueError, IndexError):
                continue
            translations.append(magnitude)
            if magnitude >= LOCKED_CAMERA_P95_TRANSLATION_LIMIT:
                large_corrections += 1
    if not translations:
        return {"sampleCount": 0, "p95TranslationPixels": 0.0, "maxTranslationPixels": 0.0, "largeCorrectionRatio": 0.0}
    ordered = sorted(translations)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "sampleCount": len(translations),
        "p95TranslationPixels": round(ordered[p95_index], 3),
        "maxTranslationPixels": round(max(translations), 3),
        "largeCorrectionRatio": round(large_corrections / len(translations), 4),
        "medianTranslationPixels": round(statistics.median(translations), 3),
    }


def _stabilize_locked_camera(video_path: Path) -> dict[str, float | int]:
    stabilized_path = video_path.with_name(f"{video_path.stem}.stabilized{video_path.suffix}")
    transform_log = video_path.with_name(f"{video_path.stem}.deshake.csv")
    filter_graph = (
        f"scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"deshake=rx=16:ry=16:edge=original:filename={transform_log}"
    )
    command = [
        "ffmpeg", "-y", "-i", str(video_path), "-vf", filter_graph, "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
        str(stabilized_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not stabilized_path.exists() or stabilized_path.stat().st_size == 0:
            raise MotionProviderError("Locked-camera stabilization produced no video")
        metrics = _parse_deshake_log(transform_log)
        if (
            float(metrics["p95TranslationPixels"]) > LOCKED_CAMERA_P95_TRANSLATION_LIMIT
            and float(metrics["largeCorrectionRatio"]) > LOCKED_CAMERA_LARGE_CORRECTION_RATIO
        ):
            raise MotionProviderError(
                "Animation rejected for uncontrolled camera shake: "
                f"p95 correction {metrics['p95TranslationPixels']}px, "
                f"large-correction ratio {metrics['largeCorrectionRatio']}"
            )
        stabilized_path.replace(video_path)
        return metrics
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to stabilize the locked-camera animation: {exc}") from exc
    finally:
        stabilized_path.unlink(missing_ok=True)
        transform_log.unlink(missing_ok=True)


def _measure_source_frame_fidelity(image_path: Path, video_path: Path) -> float:
    stats_path = video_path.with_name(f"{video_path.stem}.source-ssim.log")
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black[s];"
        f"[1:v]select='eq(n,0)',setpts=N/FRAME_RATE/TB[v];[s][v]ssim=stats_file={stats_path}"
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(image_path),
        "-i", str(video_path), "-filter_complex", filter_graph, "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        content = stats_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"All:([0-9.]+)", content)
        if not match:
            raise MotionProviderError("Unable to measure first-frame source fidelity")
        score = float(match.group(1))
        if score < SOURCE_FRAME_MIN_SSIM:
            raise MotionProviderError(
                f"Animation rejected for source framing drift or crop: first-frame SSIM {score:.3f} "
                f"is below {SOURCE_FRAME_MIN_SSIM:.3f}"
            )
        return round(score, 4)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        raise MotionProviderError(f"Unable to validate source framing fidelity: {exc}") from exc
    finally:
        stats_path.unlink(missing_ok=True)


def _measure_end_frame_fidelity(image_path: Path, video_path: Path) -> float:
    stats_path = video_path.with_name(f"{video_path.stem}.end-ssim.log")
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black[s];"
        f"[1:v]select='eq(n,0)',setpts=N/FRAME_RATE/TB[v];[s][v]ssim=stats_file={stats_path}"
    )
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(image_path),
        "-sseof", "-0.08", "-i", str(video_path), "-filter_complex", filter_graph,
        "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        content = stats_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"All:([0-9.]+)", content)
        if not match:
            raise MotionProviderError("Unable to measure final-keyframe fidelity")
        score = float(match.group(1))
        if score < SOURCE_FRAME_MIN_SSIM:
            raise MotionProviderError(
                f"Animation rejected for final-keyframe drift: ending SSIM {score:.3f} "
                f"is below {SOURCE_FRAME_MIN_SSIM:.3f}"
            )
        return round(score, 4)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        raise MotionProviderError(f"Unable to validate final-keyframe fidelity: {exc}") from exc
    finally:
        stats_path.unlink(missing_ok=True)


def _measure_sequence_integrity(
    video_path: Path,
    *,
    ignored_tile_boxes: list[dict[str, float]] | None = None,
) -> dict[str, float | int]:
    stats_path = video_path.with_name(f"{video_path.stem}.signalstats.log")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
        "-vf", f"signalstats,metadata=print:file={stats_path}", "-f", "null", "-",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        rows: list[dict[str, float]] = []
        current: dict[str, float] = {}
        for line in stats_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("frame:"):
                if current:
                    rows.append(current)
                current = {}
            elif line.startswith("lavfi.signalstats.") and "=" in line:
                key, raw_value = line.split("=", 1)
                try:
                    current[key.rsplit(".", 1)[-1]] = float(raw_value)
                except ValueError:
                    continue
        if current:
            rows.append(current)
        if len(rows) < 2:
            raise MotionProviderError("Unable to measure animation sequence integrity")
        saturation_jumps = [
            abs(rows[index].get("SATAVG", 0.0) - rows[index - 1].get("SATAVG", 0.0))
            for index in range(1, len(rows))
        ]
        jump_index = max(range(len(saturation_jumps)), key=saturation_jumps.__getitem__) + 1
        max_saturation_jump = saturation_jumps[jump_index - 1]
        luma_at_saturation_jump = rows[jump_index].get("YDIF", 0.0)
        luma_differences = [row.get("YDIF", 0.0) for row in rows[1:]]
        metrics = {
            "sampleCount": len(rows),
            "maxSaturationJump": round(max_saturation_jump, 4),
            "lumaDifferenceAtSaturationJump": round(luma_at_saturation_jump, 4),
            "maxLumaFrameDifference": round(max(row.get("YDIF", 0.0) for row in rows), 4),
            "meanLumaFrameDifference": round(statistics.fmean(luma_differences), 4),
        }
        if (
            max_saturation_jump > SEQUENCE_SATURATION_JUMP_LIMIT
            and luma_at_saturation_jump > SEQUENCE_LUMA_JUMP_LIMIT
        ):
            raise MotionProviderError(
                "Animation rejected for sudden color-block or scene-corruption artifacts: "
                f"saturation jump {max_saturation_jump:.2f}, luma difference {luma_at_saturation_jump:.2f}"
            )
        if metrics["maxLumaFrameDifference"] > SEQUENCE_MAX_LUMA_FRAME_DIFFERENCE:
            raise MotionProviderError(
                "Animation rejected for discontinuous scene corruption or an uncontrolled visual jump: "
                f"maximum luma-frame difference {metrics['maxLumaFrameDifference']:.2f} exceeds "
                f"{SEQUENCE_MAX_LUMA_FRAME_DIFFERENCE:.2f}"
            )
        if metrics["meanLumaFrameDifference"] < SEQUENCE_MIN_MEAN_LUMA_DIFFERENCE:
            raise MotionProviderError(
                "Animation rejected because it contains too little visible motion: "
                f"mean luma-frame difference {metrics['meanLumaFrameDifference']:.2f} is below "
                f"{SEQUENCE_MIN_MEAN_LUMA_DIFFERENCE:.2f}"
            )
        metrics.update(_measure_edge_tile_integrity(video_path, ignored_tile_boxes=ignored_tile_boxes))
        return metrics
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        raise MotionProviderError(f"Unable to validate animation sequence integrity: {exc}") from exc
    finally:
        stats_path.unlink(missing_ok=True)


def _verify_visible_generative_motion(metrics: dict[str, float | int]) -> None:
    mean_difference = float(metrics.get("meanLumaFrameDifference") or 0.0)
    if mean_difference < GENERATIVE_MIN_MEAN_LUMA_DIFFERENCE:
        raise MotionProviderError(
            "Animation rejected because model-generated scene motion is too subtle: "
            f"mean luma-frame difference {mean_difference:.2f} is below "
            f"{GENERATIVE_MIN_MEAN_LUMA_DIFFERENCE:.2f}"
        )


def _measure_edge_tile_integrity(
    video_path: Path,
    *,
    ignored_tile_boxes: list[dict[str, float]] | None = None,
) -> dict[str, float | int]:
    # Localized model corruption can hide inside healthy whole-frame averages. Sample every
    # 4x4 tile so central generation failures are caught as well as edge artifacts.
    def intersects_ignored_box(row: int, column: int) -> bool:
        tile_left, tile_top = column / 4, row / 4
        tile_right, tile_bottom = (column + 1) / 4, (row + 1) / 4
        return any(
            tile_left < float(box.get("x", 0.0)) + float(box.get("width", 0.0))
            and tile_right > float(box.get("x", 0.0))
            and tile_top < float(box.get("y", 0.0)) + float(box.get("height", 0.0))
            and tile_bottom > float(box.get("y", 0.0))
            for box in ignored_tile_boxes or []
        )

    tiles = [
        (row, column)
        for row in range(4)
        for column in range(4)
        if not intersects_ignored_box(row, column)
    ]
    if not tiles:
        raise MotionProviderError("Motion corridor leaves no fixed-background tiles for validation")
    worst_saturation_jump = 0.0
    worst_luma_difference = 0.0
    worst_tile = ""
    for row, column in tiles:
        stats_path = video_path.with_name(f"{video_path.stem}.edge-{row}-{column}.signalstats.log")
        crop = f"crop=iw/4:ih/4:{column}*iw/4:{row}*ih/4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
            "-vf", f"{crop},signalstats,metadata=print:file={stats_path}", "-f", "null", "-",
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            saturation_values: list[float] = []
            luma_differences: list[float] = []
            for line in stats_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("lavfi.signalstats.SATAVG="):
                    saturation_values.append(float(line.split("=", 1)[1]))
                elif line.startswith("lavfi.signalstats.YDIF="):
                    luma_differences.append(float(line.split("=", 1)[1]))
            if len(saturation_values) < 2 or not luma_differences:
                raise MotionProviderError("Unable to measure animation edge-tile integrity")
            tile_saturation_jump = max(
                abs(current - previous)
                for previous, current in zip(saturation_values, saturation_values[1:])
            )
            jump_frame = max(
                range(1, len(saturation_values)),
                key=lambda index: abs(saturation_values[index] - saturation_values[index - 1]),
            )
            tile_luma_difference = luma_differences[jump_frame]
            if tile_saturation_jump > worst_saturation_jump:
                worst_saturation_jump = tile_saturation_jump
                worst_luma_difference = tile_luma_difference
                worst_tile = f"{row},{column}"
        except (FileNotFoundError, subprocess.CalledProcessError, OSError, ValueError) as exc:
            raise MotionProviderError(f"Unable to validate animation edge-tile integrity: {exc}") from exc
        finally:
            stats_path.unlink(missing_ok=True)
    metrics: dict[str, float | int] = {
        "edgeTileSampleCount": len(tiles),
        "maxEdgeTileSaturationJump": round(worst_saturation_jump, 4),
        "maxEdgeTileLumaDifference": round(worst_luma_difference, 4),
    }
    if (
        worst_saturation_jump > SEQUENCE_EDGE_TILE_SATURATION_JUMP_LIMIT
        and worst_luma_difference > SEQUENCE_EDGE_TILE_LUMA_DIFFERENCE_LIMIT
    ):
        raise MotionProviderError(
            "Animation rejected for localized edge color-block corruption: "
            f"tile {worst_tile}, saturation jump {worst_saturation_jump:.2f}, "
            f"luma difference {worst_luma_difference:.2f}"
        )
    if worst_saturation_jump > SEQUENCE_EDGE_TILE_ABSOLUTE_SATURATION_LIMIT:
        raise MotionProviderError(
            "Animation rejected for localized chroma corruption or control-boundary leakage: "
            f"tile {worst_tile}, saturation jump {worst_saturation_jump:.2f} exceeds "
            f"{SEQUENCE_EDGE_TILE_ABSOLUTE_SATURATION_LIMIT:.2f}"
        )
    return metrics


def _protect_decorative_frame(image_path: Path, video_path: Path) -> None:
    protected_path = video_path.with_name(f"{video_path.stem}.frame-protected{video_path.suffix}")
    inner_width = FRAME_PROTECTION_WIDTH - (FRAME_PROTECTION_X * 2)
    inner_height = FRAME_PROTECTION_HEIGHT - (FRAME_PROTECTION_Y * 2)
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},"
        "format=gbrp,split=2[motion][masksource];"
        f"[1:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gbrp[still];"
        "[masksource]lutrgb=r=255:g=255:b=255,"
        f"drawbox=x={FRAME_PROTECTION_X}:y={FRAME_PROTECTION_Y}:w={inner_width}:h={inner_height}:"
        f"color=black:t=fill,boxblur={FRAME_PROTECTION_FEATHER}[mask];"
        "[motion][still][mask]maskedmerge[merged];[merged]format=yuv420p[v]"
    )
    command = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(image_path),
        "-filter_complex", filter_graph, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(protected_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not protected_path.exists() or protected_path.stat().st_size == 0:
            raise MotionProviderError("Decorative-frame protection produced no video")
        protected_path.replace(video_path)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        protected_path.unlink(missing_ok=True)
        raise MotionProviderError(f"Unable to protect the source image's decorative frame: {exc}") from exc


def _protect_locked_frame_edges(image_path: Path, video_path: Path) -> None:
    """Restore a narrow source perimeter so stabilization cannot expose moving black edge bars."""
    protected_path = video_path.with_name(f"{video_path.stem}.edge-protected{video_path.suffix}")
    inset = LOCKED_EDGE_PROTECTION
    duration = 5.0
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gbrp[motion];"
        f"[1:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gbrp[still];"
        f"color=white:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT},format=gray,"
        f"drawbox=x={inset}:y={inset}:w={FRAME_PROTECTION_WIDTH - (inset * 2)}:"
        f"h={FRAME_PROTECTION_HEIGHT - (inset * 2)}:color=black:t=fill,boxblur={LOCKED_EDGE_FEATHER}[mask];"
        "[motion][still][mask]maskedmerge,format=yuv420p[v]"
    )
    command = [
        "ffmpeg", "-y", "-i", str(video_path), "-loop", "1", "-i", str(image_path),
        "-filter_complex", filter_graph, "-map", "[v]", "-map", "0:a?",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", str(protected_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not protected_path.exists() or protected_path.stat().st_size == 0:
            raise MotionProviderError("Locked-edge protection produced no video")
        protected_path.replace(video_path)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        protected_path.unlink(missing_ok=True)
        raise MotionProviderError(f"Unable to protect locked frame edges: {exc}") from exc


def _generate_coherent_environmental_fallback(image_path: Path, destination: Path) -> None:
    """Create clean full-frame motion when image-to-video models corrupt the source.

    The source remains geometrically fixed while a broad, soft light movement
    crosses the complete composition. This is deliberately conservative: it
    provides visible environmental motion without hallucinating subjects,
    warping objects, cropping the frame, or compositing a moving inset panel.
    """
    duration = 5.0625
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "format=yuv420p,"
        f"eq=brightness='0.008*sin(2*PI*t/{duration})':"
        f"contrast='1+0.006*sin(2*PI*t/{duration})'[base];"
        "[1:v]geq=r='255':g='205':b='125':"
        f"a='56*exp(-((X-W*(-0.5+2*T/{duration}))*(X-W*(-0.5+2*T/{duration}))"
        "+(Y-H*0.62)*(Y-H*0.62))/(2*80*80))'[glow];"
        "[base][glow]overlay=shortest=1,format=yuv420p[v]"
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "16",
        "-i", str(image_path), "-f", "lavfi", "-i",
        f"color=c=black@0.0:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d={duration},format=rgba",
        "-filter_complex", filter_graph, "-map", "[v]", "-t", str(duration), "-r", "16",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MotionProviderError(f"Unable to create coherent environmental motion: {exc}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Coherent environmental motion produced no video")


def _generate_region_environmental_fallback(
    image_path: Path,
    destination: Path,
    motion_plan: dict[str, Any],
) -> int:
    """Animate planned environmental energy without moving or regenerating source pixels."""
    duration = 5.0625
    regions = [region for region in motion_plan.get("regions") or [] if region.get("enabled") is not False][:5]
    if not regions:
        raise MotionProviderError("Motion plan has no enabled environmental regions")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "16",
        "-i", str(image_path),
    ]
    graph = [
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "format=yuv420p[base]"
    ]
    current = "base"
    fire_region: dict[str, Any] | None = None
    for index, region in enumerate(regions, start=1):
        command.extend([
            "-f", "lavfi", "-i",
            f"color=c=black@0.0:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d={duration},format=rgba",
        ])
        box = region.get("box") or {}
        center_x = (float(box.get("x", 0.1)) + (float(box.get("width", 0.35)) / 2))
        center_y = (float(box.get("y", 0.1)) + (float(box.get("height", 0.35)) / 2))
        spread = max(28, round(min(
            float(box.get("width", 0.35)) * FRAME_PROTECTION_WIDTH,
            float(box.get("height", 0.35)) * FRAME_PROTECTION_HEIGHT,
        ) * 0.42))
        label = f"{region.get('label', '')} {region.get('effect', '')}".lower()
        if "fire" in label or "surge" in label:
            red, green, blue, alpha = 255, 112, 28, 86
            fire_region = region
        elif "dust" in label or "roll" in label:
            red, green, blue, alpha = 205, 138, 68, 44
        elif "atmos" in label or "haze" in label:
            red, green, blue, alpha = 190, 210, 225, 28
        else:
            red, green, blue, alpha = 255, 174, 62, 42
        dx, dy = _region_motion_offset(region, index)
        dx, dy = dx.replace("t", "T"), dy.replace("t", "T")
        x_expression = f"W*{center_x:.4f}+3*({dx})"
        y_expression = f"H*{center_y:.4f}+3*({dy})"
        glow = f"glow{index}"
        output = f"environment{index}"
        graph.append(
            f"[{index}:v]geq=r='{red}':g='{green}':b='{blue}':"
            f"a='{alpha}*exp(-((X-({x_expression}))*(X-({x_expression}))"
            f"+(Y-({y_expression}))*(Y-({y_expression})))/(2*{spread}*{spread}))'[{glow}]"
        )
        graph.append(f"[{current}][{glow}]overlay=shortest=1[{output}]")
        current = output

    if fire_region:
        command.extend([
            "-f", "lavfi", "-i",
            f"color=c=black@0.0:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d={duration},format=rgba",
        ])
        box = fire_region.get("box") or {}
        x0 = round(float(box.get("x", 0.72)) * FRAME_PROTECTION_WIDTH)
        y0 = round((float(box.get("y", 0.02)) + float(box.get("height", 0.96)) * 0.82) * FRAME_PROTECTION_HEIGHT)
        terms = []
        for spark in range(9):
            sx = min(FRAME_PROTECTION_WIDTH - 4, x0 + 4 + (spark % 4) * 17)
            sy = y0 - spark * 19
            moving_y = f"mod({sy}-({44 + spark * 3})*T+{FRAME_PROTECTION_HEIGHT},{FRAME_PROTECTION_HEIGHT})"
            terms.append(f"if(lt((X-{sx})*(X-{sx})+(Y-({moving_y}))*(Y-({moving_y})),{4 + (spark % 3) * 3}),210,0)")
        spark_alpha = terms[0]
        for term in terms[1:]:
            spark_alpha = f"max({spark_alpha},{term})"
        sparks_input = len(regions) + 1
        graph.append(
            f"[{sparks_input}:v]geq=r='255':g='178':b='62':a='{spark_alpha}'[sparks]"
        )
        graph.append(f"[{current}][sparks]overlay=shortest=1[withsparks]")
        current = "withsparks"
    graph.append(f"[{current}]format=yuv420p[v]")
    command.extend([
        "-filter_complex", ";".join(graph), "-map", "[v]", "-t", str(duration), "-r", "16",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(destination),
    ])
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise MotionProviderError(f"Unable to create region environmental motion: {detail[-600:]}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Region environmental motion produced no video")
    return len(regions)


def _object_vector_regions(motion_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        region
        for region in (motion_plan or {}).get("regions") or []
        if region.get("enabled") is not False and region.get("method") == "object-vector"
    ][:5]


def _generative_regions(motion_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        region
        for region in (motion_plan or {}).get("regions") or []
        if region.get("enabled") is not False and region.get("method") != "object-vector"
    ][:5]


def _object_vector_motion_corridors(motion_plan: dict[str, Any] | None) -> list[dict[str, float]]:
    """Return swept object bounds so fixed-background QA does not flag intended motion."""
    corridors: list[dict[str, float]] = []
    for region in _object_vector_regions(motion_plan):
        box = region.get("box") or {}
        vector = region.get("vector") or {}
        x = float(box.get("x", 0.0))
        y = float(box.get("y", 0.0))
        width = float(box.get("width", 0.0))
        height = float(box.get("height", 0.0))
        dx = float(vector.get("dx", 0.0))
        dy = float(vector.get("dy", 0.0))
        left = max(0.0, min(x, x + dx))
        top = max(0.0, min(y, y + dy))
        right = min(1.0, max(x + width, x + dx + width))
        bottom = min(1.0, max(y + height, y + dy + height))
        corridors.append({
            "x": left,
            "y": top,
            "width": max(0.0, right - left),
            "height": max(0.0, bottom - top),
        })
    return corridors


def _vector_easing_expression(easing: str, duration: float, variable: str = "t") -> str:
    unit = f"({variable}/{duration})"
    return {
        "linear": unit,
        "ease-in": f"({unit}*{unit})",
        "ease-out": f"(1-(1-{unit})*(1-{unit}))",
        "ease-in-out": f"(3*{unit}*{unit}-2*{unit}*{unit}*{unit})",
    }.get(easing, f"(3*{unit}*{unit}-2*{unit}*{unit}*{unit})")


def _release_comfyui_gpu_memory(session=requests) -> None:
    """Unload retained video models before Stable Diffusion uses the shared Phronesis GPU."""
    response = session.post(
        f"{COMFYUI_MODEL_API_URL.rstrip('/')}/free",
        json={"unload_models": True, "free_memory": True},
        timeout=60,
    )
    response.raise_for_status()


def _release_stable_diffusion_gpu_memory(session=requests) -> None:
    """Unload the image checkpoint before ComfyUI begins the video synthesis phase."""
    response = session.post(
        STABLE_DIFFUSION_API_URL.replace("/txt2img", "/unload-checkpoint"),
        timeout=60,
    )
    response.raise_for_status()


def _generate_background_plate(
    image,
    object_mask,
    *,
    labels: list[str],
    scene_prompt: str,
    negative_prompt: str,
    session=requests,
):
    """Use protected local inpainting to reconstruct only pixels hidden by a large object."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise MotionProviderError("Background-plate generation requires the bundled OpenCV runtime") from exc

    _release_comfyui_gpu_memory(session)
    expanded_mask = cv2.dilate(object_mask, np.ones((11, 11), dtype=np.uint8), iterations=2)
    ok_image, encoded_image = cv2.imencode(".png", image)
    ok_mask, encoded_mask = cv2.imencode(".png", expanded_mask)
    if not ok_image or not ok_mask:
        raise MotionProviderError("Unable to encode the source and mask for background reconstruction")
    object_names = ", ".join(label for label in labels if label) or "masked foreground object"
    label_text = object_names.lower()
    celestial_object = any(
        token in label_text for token in ("planet", "moon", "sun", "orb", "sphere")
    )
    if celestial_object:
        background_subject = (
            "unobstructed continuation of the existing sky, atmosphere, stars, haze, and distant "
            "landscape visible immediately around the mask; no celestial body, circle, sphere, orb, moon, or planet"
        )
        object_negatives = "planet, moon, sun, orb, sphere, circle, circular silhouette, celestial body"
    else:
        background_subject = (
            "unobstructed continuation of the existing background textures and scenery visible immediately around the mask"
        )
        object_negatives = object_names
    prompt = (
        "Create an empty background plate for this exact image. Fill the white mask with "
        f"{background_subject}. Match the nearest boundary colors, texture, depth, lighting, and art style. "
        f"Remove the masked {object_names} completely. The filled area must contain background only. "
        "Do not add any subject, focal object, figure, structure, symbol, text, or border."
    )
    payload = {
        "init_images": [base64.b64encode(encoded_image.tobytes()).decode("ascii")],
        "mask": base64.b64encode(encoded_mask.tobytes()).decode("ascii"),
        "prompt": prompt,
        "negative_prompt": (
            f"{negative_prompt}, {object_negatives}, duplicate object, foreground subject, hard mask edge, "
            "black hole, circular cutout, seam, text, watermark"
        )[:1800],
        "width": FRAME_PROTECTION_WIDTH,
        "height": FRAME_PROTECTION_HEIGHT,
        "steps": 24,
        "cfg_scale": 6.0,
        "sampler_name": "DPM++ 2M Karras",
        "denoising_strength": 0.92,
        "mask_blur": 24,
        "inpainting_fill": 2,
        "inpaint_full_res": False,
        "inpaint_full_res_padding": 48,
    }
    forbidden_terms = {
        "planet": {"planet", "moon", "sun", "orb", "sphere", "celestial body"},
        "moon": {"planet", "moon", "sun", "orb", "sphere", "celestial body"},
        "person": {"person", "people", "man", "woman", "human", "figure"},
        "car": {"car", "vehicle", "automobile", "truck"},
        "boat": {"boat", "ship", "vessel"},
        "bird": {"bird", "animal"},
    }
    forbidden = set()
    for category, terms in forbidden_terms.items():
        if category in label_text:
            forbidden.update(terms)
    if not forbidden:
        forbidden.update(
            token for token in re.findall(r"[a-z]{4,}", label_text)
            if token not in {"existing", "foreground", "object", "region"}
        )

    if celestial_object:
        # A large independently generated empty plate needs a broad transition
        # into the preserved scenery. Keep the complete removal mask opaque so
        # no trace of the old celestial body can bleed back into the plate.
        transition_mask = cv2.dilate(
            expanded_mask,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (61, 61)),
            iterations=1,
        )
        blend_mask = cv2.GaussianBlur(
            transition_mask, (0, 0), sigmaX=24.0, sigmaY=24.0
        )
        blend_mask = cv2.max(blend_mask, expanded_mask)
    else:
        blend_mask = cv2.GaussianBlur(expanded_mask, (0, 0), sigmaX=10.0, sigmaY=10.0)
    alpha = (blend_mask.astype(np.float32) / 255.0)[:, :, None]

    def validate_generated_fill(generated) -> str:
        """Describe only the pixels replacing the object, not the preserved scene."""
        x, y, width, height = cv2.boundingRect(expanded_mask)
        crop = generated[y:y + height, x:x + width].copy()
        crop_mask = expanded_mask[y:y + height, x:x + width]
        inside = crop_mask > 0
        if not np.any(inside):
            raise MotionProviderError("Background validation mask was empty")
        outside = ~inside
        if np.any(outside):
            neutral = np.median(crop[inside], axis=0).astype(np.uint8)
            crop[outside] = neutral
        ok_crop, encoded_crop = cv2.imencode(".png", crop)
        if not ok_crop:
            raise MotionProviderError("Unable to encode the reconstructed background region")
        interrogation = session.post(
            IMAGE_INTERROGATE_API_URL,
            json={
                "image": base64.b64encode(encoded_crop.tobytes()).decode("ascii"),
                "model": "clip",
            },
            timeout=300,
        )
        interrogation.raise_for_status()
        caption = str(interrogation.json().get("caption") or "").strip().lower()
        if not caption:
            raise MotionProviderError("Background semantic validation returned no caption")
        return caption

    def decode_generated(response, failure_prefix: str):
        images = response.json().get("images") or []
        if not images:
            raise MotionProviderError(f"{failure_prefix} returned no image")
        try:
            decoded = base64.b64decode(str(images[0]).split(",")[-1])
            generated = cv2.imdecode(np.frombuffer(decoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        except (ValueError, TypeError) as exc:
            raise MotionProviderError(f"{failure_prefix} returned an invalid image") from exc
        if generated is None:
            raise MotionProviderError(f"{failure_prefix} returned an unreadable image")
        if generated.shape[:2] != image.shape[:2]:
            generated = cv2.resize(
                generated, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LANCZOS4
            )
        return generated

    rejected_captions: list[str] = []
    # Image-conditioned inpainting persistently reconstructs round celestial
    # subjects from their surrounding rim and silhouette. For those subjects,
    # skip directly to an independently generated empty plate so the removed
    # body cannot leak through the conditioning image.
    for _attempt in range(0 if celestial_object else 3):
        payload["seed"] = random.randint(1, 2**31 - 1)
        response = session.post(BACKGROUND_PLATE_API_URL, json=payload, timeout=600)
        response.raise_for_status()
        generated = decode_generated(response, "Background reconstruction")
        candidate = np.clip(
            (generated.astype(np.float32) * alpha) + (image.astype(np.float32) * (1.0 - alpha)),
            0,
            255,
        ).astype(np.uint8)
        caption = validate_generated_fill(generated)
        matches = sorted(term for term in forbidden if term in caption)
        if not matches:
            return candidate
        rejected_captions.append(caption[:240])

    style_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", scene_prompt)
        if any(
            cue in sentence.lower()
            for cue in ("art treatment", "visual treatment", "art style", "palette", "texture", "lighting")
        )
    ]
    style_context = " ".join(style_sentences)[:600]
    for term in sorted(forbidden | {label_text}, key=len, reverse=True):
        if term:
            style_context = re.sub(rf"\b{re.escape(term)}s?\b", "", style_context, flags=re.IGNORECASE)
    style_context = re.sub(r"\s+", " ", style_context).strip()
    if celestial_object:
        empty_background = (
            "empty primordial cosmic background plate, deep starfield, subtle atmospheric haze, "
            "distant barren rocky horizon along the lower edge, background only, no focal subject"
        )
    else:
        empty_background = (
            "empty unobstructed background plate matching the surrounding setting, depth, palette, "
            "texture, and light direction, background only, no focal subject"
        )
    canvas_payload = {
        "prompt": f"{empty_background}. {style_context}"[:1800],
        "negative_prompt": payload["negative_prompt"],
        "width": FRAME_PROTECTION_WIDTH,
        "height": FRAME_PROTECTION_HEIGHT,
        "steps": 24,
        "cfg_scale": 7.0,
        "sampler_name": "DPM++ 2M Karras",
    }
    for _attempt in range(3):
        canvas_payload["seed"] = random.randint(1, 2**31 - 1)
        response = session.post(STABLE_DIFFUSION_API_URL, json=canvas_payload, timeout=600)
        response.raise_for_status()
        generated = decode_generated(response, "Empty background generation")
        caption = validate_generated_fill(generated)
        matches = sorted(term for term in forbidden if term in caption)
        if not matches:
            if celestial_object:
                # Use the independently generated plate across the whole area
                # behind the moving body. Rectangular inpainting cannot invent
                # a large hidden sky and horizon without exposing its bounds.
                # Preserve only the far-right landmark/frame and near-bottom
                # foreground through long directional transitions.
                frame_height, frame_width = image.shape[:2]
                yy, xx = np.mgrid[0:frame_height, 0:frame_width]

                def smoothstep(values):
                    values = np.clip(values, 0.0, 1.0)
                    return values * values * (3.0 - (2.0 * values))

                preserve_right = smoothstep(
                    (xx - (frame_width * 0.68)) / max(1.0, frame_width * 0.20)
                )
                preserve_bottom = smoothstep(
                    (yy - (frame_height * 0.70)) / max(1.0, frame_height * 0.30)
                )
                preserve_source = np.maximum(preserve_right, preserve_bottom)[:, :, None]
                return np.clip(
                    (generated.astype(np.float32) * (1.0 - preserve_source))
                    + (image.astype(np.float32) * preserve_source),
                    0,
                    255,
                ).astype(np.uint8)
            return np.clip(
                (generated.astype(np.float32) * alpha)
                + (image.astype(np.float32) * (1.0 - alpha)),
                0,
                255,
            ).astype(np.uint8)
        rejected_captions.append(caption[:240])
    raise MotionProviderError(
        "Background plate rejected because the removed object remained or was regenerated: "
        + "; ".join(rejected_captions)
    )


def _deterministic_background_plate(image, object_mask):
    """Remove a large foreground object without allowing a model to invent a replacement."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise MotionProviderError("Deterministic background reconstruction requires OpenCV") from exc

    expanded_mask = cv2.dilate(object_mask, np.ones((11, 11), dtype=np.uint8), iterations=2)
    x, y, box_width, box_height = cv2.boundingRect(expanded_mask)
    box_area = max(1, box_width * box_height)
    rectangularity = float(np.count_nonzero(expanded_mask)) / float(box_area)
    if rectangularity >= 0.75 and x > 0 and x + box_width < image.shape[1]:
        band = min(12, x, image.shape[1] - (x + box_width))
        left = np.mean(image[y:y + box_height, x - band:x], axis=1)
        right = np.mean(image[y:y + box_height, x + box_width:x + box_width + band], axis=1)
        left = cv2.GaussianBlur(
            left[:, None, :].astype(np.float32), (1, 0), sigmaX=0.0, sigmaY=5.0
        )[:, 0, :]
        right = cv2.GaussianBlur(
            right[:, None, :].astype(np.float32), (1, 0), sigmaX=0.0, sigmaY=5.0
        )[:, 0, :]
        # Continue each background row through the removed object instead of
        # repeating one edge across its full box.  The old constant fill left a
        # visible rectangular band whenever the moving object uncovered it.
        horizontal_mix = np.linspace(0.0, 1.0, box_width, dtype=np.float32)[None, :, None]
        fill = (
            (left[:, None, :] * (1.0 - horizontal_mix))
            + (right[:, None, :] * horizontal_mix)
        )
        fill = cv2.GaussianBlur(fill, (0, 0), sigmaX=7.0, sigmaY=2.5)
        reconstructed = image.copy()
        reconstructed[y:y + box_height, x:x + box_width] = np.clip(fill, 0, 255).astype(np.uint8)
    else:
        full_resolution = cv2.inpaint(image, expanded_mask, 15, cv2.INPAINT_TELEA)
        half_size = (max(2, image.shape[1] // 2), max(2, image.shape[0] // 2))
        half_image = cv2.resize(image, half_size, interpolation=cv2.INTER_AREA)
        half_mask = cv2.resize(expanded_mask, half_size, interpolation=cv2.INTER_NEAREST)
        broad_fill = cv2.inpaint(half_image, half_mask, 11, cv2.INPAINT_NS)
        broad_fill = cv2.resize(
            broad_fill,
            (image.shape[1], image.shape[0]),
            interpolation=cv2.INTER_CUBIC,
        )
        reconstructed = cv2.addWeighted(full_resolution, 0.65, broad_fill, 0.35, 0.0)
    blend_mask = cv2.GaussianBlur(expanded_mask, (0, 0), sigmaX=10.0, sigmaY=10.0)
    alpha = (blend_mask.astype(np.float32) / 255.0)[:, :, None]
    return np.clip(
        (reconstructed.astype(np.float32) * alpha)
        + (image.astype(np.float32) * (1.0 - alpha)),
        0,
        255,
    ).astype(np.uint8)


def _generate_object_vector_clip(
    image_path: Path,
    motion_plan: dict[str, Any],
    destination: Path,
    *,
    scene_prompt: str = "",
    negative_prompt: str = "",
    guidance_mask_destination: Path | None = None,
    guidance_corridor_destination: Path | None = None,
    session=requests,
) -> dict[str, Any]:
    """Segment existing objects, inpaint their old locations, and tween exact vectors.

    This runs deterministic classical vision on Sextant. It does not invoke a
    generative model, cannot invent subjects, and keeps every unselected pixel
    fixed. GrabCut turns the planner's bounding boxes into soft object mattes;
    a scene fails closed when a box cannot produce a credible isolated object.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise MotionProviderError("Object-vector rendering requires the bundled OpenCV runtime") from exc

    regions = _object_vector_regions(motion_plan)
    if not regions:
        raise MotionProviderError("Motion plan has no enabled object-vector regions")
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise MotionProviderError("Unable to read the prepared source image for object-vector motion")
    height, width = image.shape[:2]
    if (width, height) != (FRAME_PROTECTION_WIDTH, FRAME_PROTECTION_HEIGHT):
        raise MotionProviderError(
            f"Object-vector source must be {FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = destination.parent / f".{destination.stem}-object-vector-{uuid.uuid4().hex[:8]}"
    temp_root.mkdir(parents=True, exist_ok=False)
    union_mask = np.zeros((height, width), dtype=np.uint8)
    background_removal_mask = np.zeros((height, width), dtype=np.uint8)
    sprite_paths: list[Path] = []
    mask_paths: list[Path] = []
    corridor_mask_paths: list[Path] = []
    route_regions: list[dict[str, Any]] = []
    region_labels: list[str] = []
    try:
        for index, region in enumerate(regions, start=1):
            box = region.get("box") or {}
            x = max(1, min(width - 3, round(float(box.get("x", 0.1)) * width)))
            y = max(1, min(height - 3, round(float(box.get("y", 0.1)) * height)))
            box_width = max(12, min(width - x - 1, round(float(box.get("width", 0.35)) * width)))
            box_height = max(12, min(height - y - 1, round(float(box.get("height", 0.35)) * height)))
            if box_width < 12 or box_height < 12:
                raise MotionProviderError(f"Object-vector region {region.get('label') or index} is too small")

            grab_mask = np.zeros((height, width), dtype=np.uint8)
            background_model = np.zeros((1, 65), np.float64)
            foreground_model = np.zeros((1, 65), np.float64)
            try:
                cv2.grabCut(
                    image,
                    grab_mask,
                    (x, y, box_width, box_height),
                    background_model,
                    foreground_model,
                    5,
                    cv2.GC_INIT_WITH_RECT,
                )
            except cv2.error as exc:
                raise MotionProviderError(
                    f"Unable to segment object-vector region {region.get('label') or index}"
                ) from exc
            binary = np.where(
                (grab_mask == cv2.GC_FGD) | (grab_mask == cv2.GC_PR_FGD), 255, 0
            ).astype(np.uint8)
            bounded = np.zeros_like(binary)
            bounded[y:y + box_height, x:x + box_width] = binary[y:y + box_height, x:x + box_width]
            component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
                (bounded > 0).astype(np.uint8), 8
            )
            if component_count <= 1:
                raise MotionProviderError(
                    f"Object-vector region {region.get('label') or index} did not isolate an existing object"
                )
            largest_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            hard_mask = np.where(labels == largest_label, 255, 0).astype(np.uint8)
            area_ratio = float(np.count_nonzero(hard_mask)) / float(box_width * box_height)
            if area_ratio < 0.025 or area_ratio > 0.92:
                raise MotionProviderError(
                    f"Object-vector region {region.get('label') or index} produced an unsafe matte "
                    f"({area_ratio:.3f} of its box)"
                )
            hard_mask = cv2.morphologyEx(
                hard_mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8), iterations=2
            )
            soft_mask = cv2.GaussianBlur(hard_mask, (0, 0), sigmaX=2.4, sigmaY=2.4)
            union_mask = cv2.max(union_mask, hard_mask)
            label_text = str(region.get("label") or "").lower()
            celestial_region = any(
                token in label_text for token in ("planet", "moon", "sun", "orb", "sphere")
            )
            region_removal_mask = hard_mask.copy()
            if celestial_region:
                horizontal_padding = round(box_width * 0.04)
                vertical_padding = round(box_height * 0.20)
                removal_left = max(0, x - horizontal_padding)
                removal_top = max(0, y - vertical_padding)
                removal_right = min(width, x + box_width + horizontal_padding)
                removal_bottom = min(height, y + box_height + vertical_padding)
                region_removal_mask = np.zeros_like(hard_mask)
                region_removal_mask[
                    removal_top:removal_bottom,
                    removal_left:removal_right,
                ] = 255
            background_removal_mask = cv2.max(background_removal_mask, region_removal_mask)
            sprite = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
            sprite[:, :, 3] = soft_mask
            sprite_path = temp_root / f"sprite-{index}.png"
            if not cv2.imwrite(str(sprite_path), sprite):
                raise MotionProviderError("Unable to save an object-vector sprite")
            sprite_paths.append(sprite_path)
            mask_path = temp_root / f"mask-{index}.png"
            if not cv2.imwrite(str(mask_path), soft_mask):
                raise MotionProviderError("Unable to save an object-vector guidance matte")
            mask_paths.append(mask_path)
            region_labels.append(str(region.get("label") or f"Region {index}"))

            vector = region.get("vector") or {}
            dx = round(float(vector.get("dx", 0.0)) * width, 3)
            dy = round(float(vector.get("dy", 0.0)) * height, 3)
            if abs(dx) < 1.0 and abs(dy) < 1.0:
                raise MotionProviderError(
                    f"Object-vector region {region.get('label') or index} has no visible displacement"
                )
            swept_corridor = np.zeros_like(hard_mask)
            for step in range(17):
                progress = step / 16.0
                eased = (3.0 * progress * progress) - (2.0 * progress * progress * progress)
                transform = np.float32([[1.0, 0.0, dx * eased], [0.0, 1.0, dy * eased]])
                translated = cv2.warpAffine(
                    region_removal_mask,
                    transform,
                    (width, height),
                    flags=cv2.INTER_NEAREST,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
                swept_corridor = cv2.max(swept_corridor, translated)
            corridor_kernel_size = (OBJECT_MOTION_CORRIDOR_EXPANSION * 2) + 1
            swept_corridor = cv2.dilate(
                swept_corridor,
                cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE,
                    (corridor_kernel_size, corridor_kernel_size),
                ),
                iterations=1,
            )
            swept_corridor = cv2.GaussianBlur(
                swept_corridor,
                (0, 0),
                sigmaX=OBJECT_MOTION_CORRIDOR_FEATHER,
                sigmaY=OBJECT_MOTION_CORRIDOR_FEATHER,
            )
            corridor_mask_path = temp_root / f"corridor-mask-{index}.png"
            if not cv2.imwrite(str(corridor_mask_path), swept_corridor):
                raise MotionProviderError("Unable to save an object-vector generative corridor")
            corridor_mask_paths.append(corridor_mask_path)
            route_regions.append({
                "id": str(region.get("id") or f"region-{index}"),
                "label": str(region.get("label") or f"Region {index}"),
                "dxPixels": dx,
                "dyPixels": dy,
                "easing": str(region.get("easing") or "ease-in-out"),
                "matteAreaRatio": round(area_ratio, 4),
            })

        occlusion_ratio = float(np.count_nonzero(union_mask)) / float(width * height)
        celestial_object = any(
            token in " ".join(region_labels).lower()
            for token in ("planet", "moon", "sun", "orb", "sphere")
        )
        if occlusion_ratio >= 0.10 and celestial_object:
            background = _generate_background_plate(
                image,
                background_removal_mask,
                labels=region_labels,
                scene_prompt=scene_prompt,
                negative_prompt=negative_prompt,
                session=session,
            )
            background_mode = "validated-full-canvas-celestial-plate"
        elif occlusion_ratio >= 0.10:
            background = _generate_background_plate(
                image,
                background_removal_mask,
                labels=region_labels,
                scene_prompt=scene_prompt,
                negative_prompt=negative_prompt,
                session=session,
            )
            background_mode = "protected-local-generative-plate"
        else:
            inpaint_mask = cv2.dilate(
                background_removal_mask,
                np.ones((7, 7), dtype=np.uint8),
                iterations=2,
            )
            background = cv2.inpaint(image, inpaint_mask, 5, cv2.INPAINT_TELEA)
            background_mode = "deterministic-small-object-inpaint"
        background_path = temp_root / "background.png"
        if not cv2.imwrite(str(background_path), background):
            raise MotionProviderError("Unable to save the object-vector background")

        duration = 5.0625
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for source in [background_path, *sprite_paths]:
            command.extend(["-framerate", "16", "-loop", "1", "-i", str(source)])
        graph = ["[0:v]format=rgba[base0]"]
        current = "base0"
        for index, route in enumerate(route_regions, start=1):
            easing = _vector_easing_expression(str(route["easing"]), duration)
            output = f"base{index}"
            graph.append(
                f"[{index}:v]format=rgba[sprite{index}];"
                f"[{current}][sprite{index}]overlay="
                f"x='{route['dxPixels']}*{easing}':y='{route['dyPixels']}*{easing}':"
                f"shortest=1:format=auto[{output}]"
            )
            current = output
        graph.append(f"[{current}]format=yuv420p[v]")
        command.extend([
            "-filter_complex", ";".join(graph), "-map", "[v]", "-t", str(duration), "-r", "16",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(destination),
        ])
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise MotionProviderError(f"Unable to render object-vector motion: {detail[-600:]}") from exc
        if not destination.exists() or destination.stat().st_size == 0:
            raise MotionProviderError("Object-vector rendering produced no video")
        def render_moving_matte(source: Path, target: Path, label: str) -> None:
            route = route_regions[0]
            easing = _vector_easing_expression(str(route["easing"]), duration)
            mask_command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                f"color=black:s={FRAME_PROTECTION_WIDTH}x{FRAME_PROTECTION_HEIGHT}:r=16:d={duration}",
                "-framerate", "16", "-loop", "1", "-i", str(source),
                "-filter_complex",
                f"[0:v]format=gray[base];[1:v]format=gray[matte];"
                f"[base][matte]overlay=x='{route['dxPixels']}*{easing}':"
                f"y='{route['dyPixels']}*{easing}':shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-t", str(duration), "-r", "16", "-c:v", "libx264",
                "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
                str(target),
            ]
            try:
                subprocess.run(mask_command, check=True, capture_output=True, text=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                raise MotionProviderError(
                    f"Unable to render object-vector {label}: {detail[-600:]}"
                ) from exc
            _verify_temporal_control_track(target, label)

        if guidance_mask_destination and len(mask_paths) == 1:
            render_moving_matte(mask_paths[0], guidance_mask_destination, "object matte")
        if guidance_corridor_destination and len(corridor_mask_paths) == 1:
            corridor_command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-framerate", "16", "-loop", "1", "-i", str(corridor_mask_paths[0]),
                "-vf", "format=gray", "-t", str(duration), "-r", "16", "-c:v", "libx264",
                "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
                str(guidance_corridor_destination),
            ]
            try:
                subprocess.run(corridor_command, check=True, capture_output=True, text=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                raise MotionProviderError(
                    f"Unable to render object-vector generative motion corridor: {detail[-600:]}"
                ) from exc
        return {
            "regions": route_regions,
            "backgroundInpainted": True,
            "backgroundMode": background_mode,
            "occlusionRatio": round(occlusion_ratio, 4),
            "generativeCorridorExpansionPixels": OBJECT_MOTION_CORRIDOR_EXPANSION,
            "generativeCorridorFeatherPixels": OBJECT_MOTION_CORRIDOR_FEATHER,
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _composite_generated_object_motion(
    vector_path: Path,
    generated_path: Path,
    moving_mask_path: Path,
    destination: Path,
) -> None:
    """Apply generated texture only inside the tracked moving-object matte.

    The deterministic vector render owns the subject count, trajectory, and
    background.  The model contributes evolving surface detail inside the one
    moving matte, so a model-retained copy at the original location cannot leak
    into the accepted clip.
    """
    filter_graph = (
        f"[0:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gbrp[vector];"
        f"[1:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gbrp[generated];"
        f"[2:v]scale={FRAME_PROTECTION_WIDTH}:{FRAME_PROTECTION_HEIGHT},format=gray[mask];"
        "[vector][generated][mask]maskedmerge,format=yuv420p[v]"
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(vector_path), "-i", str(generated_path), "-i", str(moving_mask_path),
        "-filter_complex", filter_graph, "-map", "[v]", "-t", "5.0625", "-r", "16",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise MotionProviderError(f"Unable to composite generated object motion: {detail[-600:]}") from exc
    if not destination.exists() or destination.stat().st_size == 0:
        raise MotionProviderError("Generated object-motion composite produced no video")


def generate_motion_clip(
    image_path: Path,
    destination: Path,
    *,
    prompt: str,
    negative_prompt: str,
    seed: int | None = None,
    protect_style_frame: bool = False,
    camera_behavior: str = "locked",
    motion_plan: dict[str, Any] | None = None,
    session=requests,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mediastudio-motion-") as temp_dir:
        prepared_source = Path(temp_dir) / f"prepared-{image_path.stem}.png"
        _prepare_source_image(image_path, prepared_source)
        object_regions = _object_vector_regions(motion_plan)
        generative_regions = _generative_regions(motion_plan)
        vector_path: Path | None = None
        vector_mask_path: Path | None = None
        vector_corridor_path: Path | None = None
        vector_route: dict[str, Any] | None = None

        def validate_object_vector() -> dict[str, Any]:
            quality: dict[str, Any] = {
                "status": "accepted",
                "cameraBehavior": "locked",
                "sourceSizing": "fit-and-pad-no-crop",
                "modelProvider": OBJECT_VECTOR_PROVIDER,
                "providerPolicy": "sextant-deterministic-local",
                "controlMode": "segmented-object-vector-tween",
                "modelDenoise": 0.0,
                "lockedBackground": True,
                "semanticIdentityPreserved": True,
                "fullFrameGeneration": False,
                "motionRegionCount": len(object_regions),
                "motionPlanSummary": str((motion_plan or {}).get("summary") or "")[:320],
                "route": vector_route or {},
                "stabilization": {
                    "sampleCount": 0,
                    "p95TranslationPixels": 0.0,
                    "maxTranslationPixels": 0.0,
                    "largeCorrectionRatio": 0.0,
                },
                "lockedEdgesProtected": True,
            }
            quality["sourceFrameSsim"] = _measure_source_frame_fidelity(prepared_source, destination)
            quality["sequenceIntegrity"] = _measure_sequence_integrity(destination)
            _verify_video(destination)
            return quality

        if object_regions:
            vector_path = Path(temp_dir) / f"vector-{image_path.stem}.mp4"
            vector_mask_path = (
                Path(temp_dir) / f"vector-mask-{image_path.stem}.mp4"
                if len(object_regions) == 1 else None
            )
            vector_corridor_path = (
                Path(temp_dir) / f"vector-corridor-{image_path.stem}.mp4"
                if len(object_regions) == 1 else None
            )
            vector_route = _generate_object_vector_clip(
                prepared_source,
                motion_plan or {},
                vector_path,
                scene_prompt=prompt,
                negative_prompt=negative_prompt,
                guidance_mask_destination=vector_mask_path,
                guidance_corridor_destination=vector_corridor_path,
                session=session,
            )
            _release_stable_diffusion_gpu_memory(session)

        model_health = session.get(f"{COMFYUI_MODEL_API_URL.rstrip('/')}/system_stats", timeout=10)
        model_health.raise_for_status()
        uploaded_name = _upload_image(session, prepared_source)
        prefix = f"mediastudio/{uuid.uuid4().hex}"
        model_denoise = LOCKED_CAMERA_DENOISE if camera_behavior == "locked" else 1.0
        workflow = _patched_workflow(
            uploaded_name,
            prompt,
            negative_prompt,
            prefix,
            seed=seed,
            denoise=model_denoise,
        )
        effective_seed = int(workflow["3"]["inputs"]["seed"])
        region_count = 0
        region_error: MotionProviderError | None = None

        def validate_candidate(provider: str, denoise: float) -> dict[str, Any]:
            quality: dict[str, Any] = {
                "status": "accepted",
                "cameraBehavior": camera_behavior,
                "sourceSizing": "fit-and-pad-no-crop",
                "modelProvider": provider,
                "modelDenoise": denoise,
            }
            if camera_behavior == "locked":
                quality["stabilization"] = _stabilize_locked_camera(destination)
                _protect_locked_frame_edges(prepared_source, destination)
                quality["lockedEdgesProtected"] = True
            if protect_style_frame:
                _protect_decorative_frame(prepared_source, destination)
                quality["decorativeFrameProtected"] = True
            quality["sourceFrameSsim"] = _measure_source_frame_fidelity(prepared_source, destination)
            quality["sequenceIntegrity"] = _measure_sequence_integrity(destination)
            if provider.startswith("wan2.1-vace-"):
                _verify_visible_generative_motion(quality["sequenceIntegrity"])
            _verify_video(destination)
            return quality

        def add_fallback(
            quality: dict[str, Any],
            provider: str | None = None,
            reason: str | None = None,
        ) -> dict[str, Any]:
            providers: list[str] = []
            reasons: list[str] = []
            if provider:
                providers.append(provider)
            if reason:
                reasons.append(reason)
            if providers:
                quality["fallbackFrom"] = ",".join(providers)
                quality["fallbackReason"] = "; ".join(reasons)[:500]
            return quality

        allow_full_frame = not motion_plan or bool((motion_plan or {}).get("allowFullFrameGeneration"))
        guided_regions = [
            region
            for region in (motion_plan or {}).get("regions") or []
            if region.get("enabled") is not False
        ] if vector_path else generative_regions
        keyframe_error: MotionProviderError | None = None
        if vector_path and not generative_regions:
            end_keyframe = Path(temp_dir) / f"end-{image_path.stem}.png"
            generated_keyframe_video = Path(temp_dir) / f"generated-{image_path.stem}.mp4"
            try:
                _extract_motion_keyframe(vector_path, end_keyframe)
                end_image_name = _upload_image(session, end_keyframe)
                actions = " ".join(
                    str(region.get("action") or "").strip()
                    for region in object_regions
                    if str(region.get("action") or "").strip()
                )
                keyframe_prompt = (
                    f"{prompt} The supplied first and final frames are mandatory trajectory keyframes. "
                    "Generate one continuous physical event between them. Keep exactly one subject, but let "
                    "its perspective, silhouette, surface, atmosphere, illumination, and shadows evolve "
                    "naturally as its center follows the required trajectory. Nearby dust, haze, light, and "
                    "terrain must react to the movement. This is dimensional scene motion, never a flat "
                    f"sprite or translated crop. Keep the camera and distant scenery fixed. {actions}"
                )[:2800]
                keyframe_negative_prompt = (
                    f"{negative_prompt}, cutout, collage, sprite, duplicate subject, trailing copy, "
                    "rigid pasted silhouette, moving crop, rectangular patch, matte seam, dissolve, "
                    "transparency, ghost image, frozen lighting, frozen atmosphere, "
                    "camera movement, zoom, shake, black splotch, color corruption"
                )[:2200]
                keyframe_workflow = _ltx_keyframe_workflow(
                    uploaded_name,
                    end_image_name,
                    keyframe_prompt,
                    keyframe_negative_prompt,
                    f"{prefix}-ltx-keyframe",
                    effective_seed ^ 0x4C5458,
                )
                _queue_and_download_workflow(session, keyframe_workflow, generated_keyframe_video)
                if not vector_mask_path:
                    raise MotionProviderError(
                        "Object-vector keyframe generation requires a tracked moving-object matte"
                    )
                _composite_generated_object_motion(
                    vector_path,
                    generated_keyframe_video,
                    vector_mask_path,
                    destination,
                )
                stabilization: dict[str, Any] = {
                    "sampleCount": 0,
                    "p95TranslationPixels": 0.0,
                    "maxTranslationPixels": 0.0,
                    "largeCorrectionRatio": 0.0,
                }
                if camera_behavior == "locked":
                    stabilization = _stabilize_locked_camera(destination)
                    _protect_locked_frame_edges(prepared_source, destination)
                sequence_integrity = _measure_sequence_integrity(
                    destination,
                    ignored_tile_boxes=_object_vector_motion_corridors(motion_plan),
                )
                _verify_visible_generative_motion(sequence_integrity)
                quality = {
                    "status": "accepted",
                    "cameraBehavior": camera_behavior,
                    "sourceSizing": "fit-and-pad-no-crop",
                    "modelProvider": f"{OBJECT_VECTOR_PROVIDER}+ltxv-keyframe",
                    "modelCheckpoint": LTX_KEYFRAME_CHECKPOINT,
                    "providerPolicy": "phronesis-local-model-via-sextant-orchestration",
                    "controlMode": "tracked-object-generative-texture",
                    "modelDenoise": 1.0,
                    "lockedBackground": bool((motion_plan or {}).get("lockedBackground", True)),
                    "semanticIdentityPreserved": True,
                    "fullFrameGeneration": False,
                    "motionRegionCount": len(object_regions),
                    "motionPlanSummary": str((motion_plan or {}).get("summary") or "")[:320],
                    "objectVectorRoute": vector_route,
                    "semanticMotionGate": "single-tracked-object-matte-and-fixed-background",
                    "stabilization": stabilization,
                    "lockedEdgesProtected": camera_behavior == "locked",
                    "sourceFrameSsim": _measure_source_frame_fidelity(prepared_source, destination),
                    "endFrameSsim": _measure_end_frame_fidelity(end_keyframe, destination),
                    "sequenceIntegrity": sequence_integrity,
                }
                _verify_video(destination)
                return quality
            except Exception as exc:  # noqa: BLE001 - continue through the controlled fallback ladder
                keyframe_error = exc if isinstance(exc, MotionProviderError) else MotionProviderError(str(exc))
        if guided_regions:
            regional_plan = {**(motion_plan or {}), "regions": guided_regions}
            control_path = Path(temp_dir) / f"control-{image_path.stem}.mp4"
            mask_path = Path(temp_dir) / f"mask-{image_path.stem}.mp4"
            try:
                region_count = _generate_region_control_assets(
                    prepared_source,
                    regional_plan,
                    control_path,
                    mask_path,
                    control_source=vector_path,
                    mask_source=(vector_mask_path if vector_path and not generative_regions else None),
                )
                control_name = _upload_asset(session, control_path, "video/mp4")
                mask_name = _upload_asset(session, mask_path, "video/mp4")
                region_prompt = prompt
                region_negative_prompt = negative_prompt
                if vector_path:
                    actions = " ".join(
                        str(region.get("action") or "").strip()
                        for region in object_regions
                        if str(region.get("action") or "").strip()
                    )
                    region_prompt = (
                        f"{prompt} Motion trajectory control: follow the supplied moving mask exactly. "
                        f"Resynthesize the moving subject as coherent natural footage at every frame; "
                        f"the tween supplies position only and must not look like a pasted or cropped layer. {actions}"
                    )[:2800]
                    region_negative_prompt = (
                        f"{negative_prompt}, pasted cutout, collage edge, moving crop, rectangular patch, "
                        "sprite, paper cutout, doubled subject, duplicate object, trailing copy, matte seam"
                    )[:2200]
                region_workflow = _vace_region_workflow(
                    uploaded_name, control_name, mask_name, region_prompt, region_negative_prompt,
                    f"{prefix}-region-control", effective_seed,
                )
                _queue_and_download_workflow(session, region_workflow, destination)
                provider = "wan2.1-vace-tween-control" if vector_path else "wan2.1-vace-region-control"
                quality = validate_candidate(provider, 1.0)
                quality["motionRegionCount"] = region_count
                quality["motionPlanSummary"] = str(motion_plan.get("summary") or "")[:320]
                quality["lockedBackground"] = bool(motion_plan.get("lockedBackground", True))
                quality["controlMode"] = "masked-generative-inpaint"
                quality["semanticMotionGate"] = "static-control-and-visible-generation"
                if vector_path:
                    quality["modelProvider"] = f"{OBJECT_VECTOR_PROVIDER}+wan2.1-vace-tween-control"
                    quality["controlMode"] = "tween-guided-generative-video"
                    quality["semanticMotionGate"] = "dynamic-vector-mask-and-visible-generation"
                    quality["objectVectorRoute"] = vector_route
                    quality["fullFrameGeneration"] = False
                return add_fallback(quality)
            except Exception as exc:  # noqa: BLE001 - provider failures must preserve the existing fallback chain
                region_error = exc if isinstance(exc, MotionProviderError) else MotionProviderError(str(exc))
                try:
                    restrained_workflow = _vace_region_workflow(
                        uploaded_name, control_name, mask_name, region_prompt, region_negative_prompt,
                        f"{prefix}-region-control-restrained", effective_seed ^ 0x13A7,
                        strength=0.72,
                    )
                    _queue_and_download_workflow(session, restrained_workflow, destination)
                    provider = (
                        "wan2.1-vace-tween-control-restrained"
                        if vector_path else "wan2.1-vace-region-control-restrained"
                    )
                    quality = validate_candidate(provider, 1.0)
                    quality["motionRegionCount"] = region_count
                    quality["motionPlanSummary"] = str(motion_plan.get("summary") or "")[:320]
                    quality["lockedBackground"] = bool(motion_plan.get("lockedBackground", True))
                    quality["controlMode"] = "masked-generative-inpaint"
                    quality["semanticMotionGate"] = "static-control-and-visible-generation"
                    if vector_path:
                        quality["modelProvider"] = f"{OBJECT_VECTOR_PROVIDER}+wan2.1-vace-tween-control-restrained"
                        quality["controlMode"] = "tween-guided-generative-video"
                        quality["semanticMotionGate"] = "dynamic-vector-mask-and-visible-generation"
                        quality["objectVectorRoute"] = vector_route
                        quality["fullFrameGeneration"] = False
                    return add_fallback(
                        quality,
                        "wan2.1-vace-region-control",
                        str(region_error),
                    )
                except Exception as restrained_error:  # noqa: BLE001 - retain the existing model fallback ladder
                    region_error = MotionProviderError(
                        f"VACE: {region_error}; restrained VACE: {restrained_error}"
                    )
        if region_error and vector_path:
            shutil.copy2(vector_path, destination)
            quality = validate_object_vector()
            providers = ["wan2.1-vace-tween-control"]
            reasons = [str(region_error)]
            if keyframe_error:
                providers.insert(0, "ltxv-2b-keyframe")
                reasons.insert(0, f"LTX keyframe: {keyframe_error}")
            quality["fallbackFrom"] = ",".join(providers)
            quality["fallbackReason"] = "; ".join(reasons)[:500]
            quality["suppressedGenerativeRegionCount"] = len(guided_regions)
            return quality
        if region_error and not allow_full_frame:
            raise MotionProviderError(
                f"Regional animation rejected: {region_error}. Full-frame generation is disabled; "
                "the approved still was preserved."
            ) from region_error
        try:
            _queue_and_download_workflow(session, workflow, destination)
            quality = validate_candidate("wan2.2-ti2v-5b", model_denoise)
            if region_error:
                return add_fallback(
                    quality,
                    "wan2.1-vace-region-control",
                    str(region_error),
                )
            return add_fallback(quality)
        except MotionProviderError as wan_error:
            if camera_behavior != "locked":
                raise
            fallback = _svd_fallback_workflow(
                uploaded_name,
                f"{prefix}-svd-fallback",
                effective_seed ^ 0x5A17D3,
            )
            try:
                _queue_and_download_workflow(session, fallback, destination)
                quality = validate_candidate("stable-video-diffusion", 1.0)
            except MotionProviderError as fallback_error:
                raise MotionProviderError(
                    f"Wan animation rejected: {wan_error}; SVD fallback rejected: {fallback_error}; "
                    "procedural motion is disabled because it is not generative scene animation"
                ) from fallback_error
            providers = "wan2.2-ti2v-5b"
            reasons = f"Wan: {wan_error}"
            if region_error:
                providers = f"wan2.1-vace-region-control,{providers}"
                reasons = f"Region control: {region_error}; {reasons}"
            return add_fallback(quality, providers, reasons)
