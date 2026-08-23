"""Sextant-owned adapter for the model-only ComfyUI runtime on Fortress LAN."""
from __future__ import annotations

import json
import os
import random
import re
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests

COMFYUI_MODEL_API_URL = os.getenv("COMFYUI_MODEL_API_URL", "http://100.100.97.30:8188")
COMFYUI_TIMEOUT_SECONDS = float(os.getenv("COMFYUI_TIMEOUT_SECONDS", "7200"))
COMFYUI_POLL_SECONDS = float(os.getenv("COMFYUI_POLL_SECONDS", "5"))
WORKFLOW_PATH = Path(__file__).resolve().parent / "workflows" / "wan2_2_ti2v_5b_api.json"
FRAME_PROTECTION_WIDTH = 576
FRAME_PROTECTION_HEIGHT = 320
FRAME_PROTECTION_X = 69
FRAME_PROTECTION_Y = 45
FRAME_PROTECTION_FEATHER = 4
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
) -> int:
    """Create VACE inpaint tracks; planned regions regenerate while the source stays fixed."""
    regions = [region for region in motion_plan.get("regions") or [] if region.get("enabled") is not False][:5]
    if not regions:
        raise MotionProviderError("Motion plan has no enabled regions")
    mask_terms = []
    for region in regions:
        box = region.get("box") or {}
        x = max(0, min(FRAME_PROTECTION_WIDTH - 24, round(float(box.get("x", 0.1)) * FRAME_PROTECTION_WIDTH)))
        y = max(0, min(FRAME_PROTECTION_HEIGHT - 24, round(float(box.get("y", 0.1)) * FRAME_PROTECTION_HEIGHT)))
        width = max(24, min(FRAME_PROTECTION_WIDTH - x, round(float(box.get("width", 0.35)) * FRAME_PROTECTION_WIDTH)))
        height = max(24, min(FRAME_PROTECTION_HEIGHT - y, round(float(box.get("height", 0.35)) * FRAME_PROTECTION_HEIGHT)))
        center_x, center_y = x + (width / 2), y + (height / 2)
        spread_x, spread_y = max(18, width / 2.8), max(18, height / 2.8)
        mask_terms.append(
            f"255*exp(-(((X-{center_x})*(X-{center_x})/(2*{spread_x}*{spread_x}))"
            f"+((Y-{center_y})*(Y-{center_y})/(2*{spread_y}*{spread_y}))))"
        )
    mask_expression = mask_terms[0]
    for term in mask_terms[1:]:
        mask_expression = f"max({mask_expression},{term})"
    mask_filter = f"format=gray,geq=lum='{mask_expression}',boxblur=8:1,format=yuv420p"
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
    control_command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "16",
        "-i", str(image_path), "-i", str(mask_destination), "-f", "lavfi", "-i",
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
    _verify_static_control_track(control_destination, "control")
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


def _measure_sequence_integrity(video_path: Path) -> dict[str, float | int]:
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
        metrics.update(_measure_edge_tile_integrity(video_path))
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


def _measure_edge_tile_integrity(video_path: Path) -> dict[str, float | int]:
    # Localized model corruption can hide inside healthy whole-frame averages. Sample every
    # 4x4 tile so central generation failures are caught as well as edge artifacts.
    tiles = [(row, column) for row in range(4) for column in range(4)]
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
    model_health = session.get(f"{COMFYUI_MODEL_API_URL.rstrip('/')}/system_stats", timeout=10)
    model_health.raise_for_status()
    with tempfile.TemporaryDirectory(prefix="mediastudio-motion-") as temp_dir:
        prepared_source = Path(temp_dir) / f"prepared-{image_path.stem}.png"
        _prepare_source_image(image_path, prepared_source)
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
            if protect_style_frame:
                _protect_decorative_frame(prepared_source, destination)
                quality["decorativeFrameProtected"] = True
            quality["sourceFrameSsim"] = _measure_source_frame_fidelity(prepared_source, destination)
            quality["sequenceIntegrity"] = _measure_sequence_integrity(destination)
            if provider.startswith("wan2.1-vace-region-control"):
                _verify_visible_generative_motion(quality["sequenceIntegrity"])
            _verify_video(destination)
            return quality

        if motion_plan and any(region.get("enabled") is not False for region in motion_plan.get("regions") or []):
            control_path = Path(temp_dir) / f"control-{image_path.stem}.mp4"
            mask_path = Path(temp_dir) / f"mask-{image_path.stem}.mp4"
            try:
                region_count = _generate_region_control_assets(prepared_source, motion_plan, control_path, mask_path)
                control_name = _upload_asset(session, control_path, "video/mp4")
                mask_name = _upload_asset(session, mask_path, "video/mp4")
                region_workflow = _vace_region_workflow(
                    uploaded_name, control_name, mask_name, prompt, negative_prompt,
                    f"{prefix}-region-control", effective_seed,
                )
                _queue_and_download_workflow(session, region_workflow, destination)
                quality = validate_candidate("wan2.1-vace-region-control", 1.0)
                quality["motionRegionCount"] = region_count
                quality["motionPlanSummary"] = str(motion_plan.get("summary") or "")[:320]
                quality["lockedBackground"] = bool(motion_plan.get("lockedBackground", True))
                quality["controlMode"] = "masked-generative-inpaint"
                quality["semanticMotionGate"] = "static-control-and-visible-generation"
                return quality
            except Exception as exc:  # noqa: BLE001 - provider failures must preserve the existing fallback chain
                region_error = exc if isinstance(exc, MotionProviderError) else MotionProviderError(str(exc))
                try:
                    restrained_workflow = _vace_region_workflow(
                        uploaded_name, control_name, mask_name, prompt, negative_prompt,
                        f"{prefix}-region-control-restrained", effective_seed ^ 0x13A7,
                        strength=0.72,
                    )
                    _queue_and_download_workflow(session, restrained_workflow, destination)
                    quality = validate_candidate("wan2.1-vace-region-control-restrained", 1.0)
                    quality["motionRegionCount"] = region_count
                    quality["motionPlanSummary"] = str(motion_plan.get("summary") or "")[:320]
                    quality["lockedBackground"] = bool(motion_plan.get("lockedBackground", True))
                    quality["controlMode"] = "masked-generative-inpaint"
                    quality["semanticMotionGate"] = "static-control-and-visible-generation"
                    quality["fallbackFrom"] = "wan2.1-vace-region-control"
                    quality["fallbackReason"] = str(region_error)[:500]
                    return quality
                except Exception as restrained_error:  # noqa: BLE001 - retain the existing model fallback ladder
                    region_error = MotionProviderError(
                        f"VACE: {region_error}; restrained VACE: {restrained_error}"
                    )
        try:
            _queue_and_download_workflow(session, workflow, destination)
            quality = validate_candidate("wan2.2-ti2v-5b", model_denoise)
            if region_error:
                quality["fallbackFrom"] = "wan2.1-vace-region-control"
                quality["fallbackReason"] = str(region_error)[:500]
            return quality
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
                providers = "wan2.2-ti2v-5b,stable-video-diffusion"
                reasons = f"Wan: {wan_error}; SVD: {fallback_error}"
                if region_error:
                    providers = f"wan2.1-vace-region-control,{providers}"
                    reasons = f"Region control: {region_error}; {reasons}"
                quality["fallbackFrom"] = providers
                quality["fallbackReason"] = reasons[:500]
                return quality
            quality["fallbackFrom"] = "wan2.2-ti2v-5b" if not region_error else "wan2.1-vace-region-control,wan2.2-ti2v-5b"
            quality["fallbackReason"] = (str(wan_error) if not region_error else f"Region control: {region_error}; Wan: {wan_error}")[:500]
            return quality
