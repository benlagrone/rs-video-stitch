#!/usr/bin/env python3
"""Apply Jie Huang's August 17 real-estate video requests to MediaStudio."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import requests


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "jie-2026-08-17-video-requests.json"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = requests.request(method, url, timeout=120, **kwargs)
    response.raise_for_status()
    return response.json()


def upload_files(base: str, project_id: str, subdir: str, paths: list[Path]) -> None:
    files = []
    handles = []
    try:
        for path in paths:
            handle = path.open("rb")
            handles.append(handle)
            files.append(("files", (path.name, handle)))
        request_json(
            "POST",
            f"{base}/v1/projects/{project_id}/assets",
            files=files,
            data={"subdir": subdir},
        )
    finally:
        for handle in handles:
            handle.close()


def wait_for_job(base: str, job_id: str) -> dict[str, Any]:
    while True:
        job = request_json("GET", f"{base}/v1/jobs/{job_id}")
        print(
            f"{job_id}: {job.get('status')} {job.get('stage')} "
            f"{float(job.get('progress') or 0):.0%}",
            flush=True,
        )
        if job.get("status") in TERMINAL_STATES:
            if job.get("status") != "SUCCEEDED":
                raise RuntimeError(f"Render {job_id} failed: {job.get('error') or job}")
            return job
        time.sleep(5)


def render_options(state: dict[str, Any], intro_title: str) -> dict[str, Any]:
    options = {
        "fps": 30,
        "minShot": 2.5,
        "maxShot": 8,
        "xfade": 0.5,
        "crf": 18,
        "preset": "medium",
        "tts": state.get("voice") or None,
        "ttsLanguage": state.get("language") or None,
        "ttsApi": state.get("ttsApi") or "voice-gateway",
        "voiceDir": None,
        "music": None,
        "ducking": False,
        "introEnabled": True,
        "introTitle": intro_title,
        "introLeaderImage": state.get("leaderImageName"),
        "introDuration": float(state.get("introDuration") or 4),
        "thumbnailEnabled": True,
        "logoEnabled": True,
        "logoImage": state.get("logoChoice") or "lecrown-compliant-contact-badge.png",
        "logoCorner": state.get("logoCorner") or "bottom-right",
        "logoMargin": int(state.get("logoMargin") or 24),
    }
    title_style = (state.get("renderOptions") or {}).get("titleStyle")
    if title_style:
        options["titleStyle"] = title_style
    return options


def mark_render_complete(
    base: str, project_id: str, output_name: str, job: dict[str, Any]
) -> None:
    project = request_json("GET", f"{base}/v1/projects/{project_id}")
    state = dict(project.get("state") or {})
    state["status"] = "render_complete_not_published"
    state.setdefault("pipeline", {})["render"] = "completed"
    state["pipeline"]["updatedRenderUpload"] = "not_requested"
    state["renderResult"] = {
        "jobId": job.get("jobId") or job.get("id"),
        "outputName": output_name,
        "completedAt": job.get("updatedAt"),
        "published": False,
    }
    request_json("PUT", f"{base}/v1/projects/{project_id}/state", json={"state": state})


def merge_update_scenes(
    current: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    addition_titles = {item["title"] for item in additions}
    retained = [scene for scene in current if scene.get("title") not in addition_titles]
    insert_at = max(len(retained) - 1, 0)
    return retained[:insert_at] + additions + retained[insert_at:]


def update_7131(
    base: str,
    project_id: str,
    additions: list[dict[str, Any]],
    source_paths: list[Path],
    mandarin_font: Optional[Path] = None,
) -> tuple[str, str]:
    project = request_json("GET", f"{base}/v1/projects/{project_id}")
    spec = project.get("scenes")
    state = dict(project.get("state") or {})
    if not spec or not spec.get("scenes"):
        raise RuntimeError(f"{project_id} has no saved scenes")

    upload_files(base, project_id, "images", source_paths)
    if mandarin_font:
        upload_files(base, project_id, "logo", [mandarin_font])
        state.setdefault("renderOptions", {})["titleStyle"] = {
            "fontFile": f"input/logo/{mandarin_font.name}",
            "fontSize": 58,
            "fill": "#ffffff",
            "outline": "#000000",
            "position": "top-center",
        }
        state.setdefault("pipeline", {})["mandarinTitleFont"] = "CJK verified"
    spec["scenes"] = merge_update_scenes(spec["scenes"], additions)
    request_json("PUT", f"{base}/v1/projects/{project_id}/scenes", json=spec)

    existing_images = [item for item in state.get("images") or [] if item.get("name")]
    known = {item["name"] for item in existing_images}
    for addition in additions:
        for image_name in addition["images"]:
            if image_name not in known:
                existing_images.append({"name": image_name})
                known.add(image_name)
            state.setdefault("imageHeaders", {})[image_name] = addition["title"]
    state["images"] = existing_images
    state["script"] = "\n\n".join(scene["VO"] for scene in spec["scenes"])
    state["sceneCount"] = len(spec["scenes"])
    state["status"] = "render_requested_not_published"
    state.setdefault("pipeline", {})["requesterPhotoUpdate"] = "applied"
    state["pipeline"]["updatedRenderUpload"] = "not_requested"

    previous_output = str(state.get("outputName") or "video.mp4")
    if "mandarin" in previous_output or project_id.endswith("zh-cn"):
        output_name = "7131-harmony-cove-mandarin-v4.mp4"
    else:
        output_name = "7131-harmony-cove-english-v5.mp4"
    state["outputName"] = output_name
    request_json("PUT", f"{base}/v1/projects/{project_id}/state", json={"state": state})

    job = request_json(
        "POST",
        f"{base}/v1/projects/{project_id}/render",
        json={
            "outputName": output_name,
            "renderOptions": render_options(state, state.get("introTitle") or state.get("title") or "7131 Harmony Cove"),
        },
    )
    finished = wait_for_job(base, job["jobId"])
    mark_render_complete(base, project_id, output_name, finished)
    return project_id, output_name


def build_new_state(
    project_id: str,
    listing: dict[str, Any],
    variant: dict[str, Any],
    voice_source: dict[str, Any],
    selected_assets: list[str],
) -> dict[str, Any]:
    scenes = variant["scenes"]
    intro_lines = [
        "9800 Richmond Ave — Suite 700",
        "LeCrown Properties",
        "Jie Huang, Broker" if variant["language"] == "en-US" else "经纪人 Jie Huang",
    ]
    state = {
        "schemaVersion": 1,
        "kind": "real-estate-video-project",
        "title": listing["listing"],
        "projectId": project_id,
        "language": variant["language"],
        "voice": voice_source.get("voice") or "",
        "ttsApi": voice_source.get("ttsApi") or "voice-gateway",
        "script": "\n\n".join(scene["VO"] for scene in scenes),
        "sceneCount": len(scenes),
        "targetSeconds": 90,
        "outputName": variant["outputName"],
        "images": [{"name": name} for name in selected_assets],
        "imageHeaders": {
            image: scene["title"] for scene in scenes for image in scene["images"]
        },
        "imageRoomInfo": {},
        "removedImages": [],
        "useIntro": True,
        "introTitle": "\n".join(intro_lines),
        "introLines": intro_lines,
        "introDuration": 4,
        "leaderImageName": "leader.png",
        "useLogo": True,
        "logoChoice": "lecrown-compliant-contact-badge.png",
        "logoCorner": "bottom-right",
        "logoMargin": 24,
        "branding": {
            "organization": "LeCrown Properties",
            "contactName": "Jie Huang",
            "contactPhone": "832-798-3991",
            "contactWeChat": "jessicah007",
            "contactBadge": "lecrown-compliant-contact-badge.png",
            "leaderImage": "leader.png",
        },
        "listing": {
            "address": listing["facts"]["address"],
            "scope": listing["scope"],
            "offerings": listing["facts"]["offerings"],
            "pricing": listing["facts"]["pricing"],
            "disclaimer": listing["facts"]["disclaimer"],
        },
        "source": listing["source"],
        "status": "render_requested_not_published",
        "pipeline": {
            "photos": "requester_supplied",
            "scenes": "ready",
            "script": "bilingual_cta_included",
            "render": "requested",
            "youtubeUpload": "not_requested",
        },
        "complianceReview": {
            "companyNameVisible": "LeCrown Properties",
            "brokerNameVisible": True,
            "licenseHolder": "Jie Huang",
            "licenseNumber": "734275-B",
            "licenseType": "Broker Individual",
            "publicationApproved": False,
            "scope": "Suite 700 and seventh-floor offering only",
        },
    }
    if variant["language"] == "zh-CN":
        state["renderOptions"] = {
            "titleStyle": {
                "fontFile": "input/logo/Arial-Unicode.ttf",
                "fontSize": 58,
                "fill": "#ffffff",
                "outline": "#000000",
                "position": "top-center",
            }
        }
        state["pipeline"]["mandarinTitleFont"] = "CJK verified"
    return state


def create_9800(
    base: str,
    listing: dict[str, Any],
    language_key: str,
    photo_root: Path,
    brand_root: Path,
    voice_source: dict[str, Any],
    mandarin_font: Optional[Path] = None,
) -> tuple[str, str]:
    project_id = listing["projectIds"][language_key]
    variant = listing["variants"][language_key]
    selected = listing["selectedAssets"]
    photo_paths = [photo_root / name for name in selected]
    upload_files(base, project_id, "images", photo_paths)
    upload_files(base, project_id, "logo", [brand_root / "logo" / "lecrown-compliant-contact-badge.png"])
    upload_files(base, project_id, "leader", [brand_root / "leader" / "leader.png"])
    if mandarin_font:
        upload_files(base, project_id, "logo", [mandarin_font])

    state = build_new_state(project_id, listing, variant, voice_source, selected)
    if mandarin_font:
        state["renderOptions"]["titleStyle"]["fontFile"] = (
            f"input/logo/{mandarin_font.name}"
        )
    spec = {
        "info": {
            "name": listing["listing"],
            "source": "Requester-supplied professional photos",
            "scope": listing["scope"],
        },
        "vid": {
            "voice": state["voice"] or None,
            "lang": state["language"],
            "api": state["ttsApi"],
        },
        "scenes": variant["scenes"],
    }
    request_json("PUT", f"{base}/v1/projects/{project_id}/scenes", json=spec)
    request_json("PUT", f"{base}/v1/projects/{project_id}/state", json={"state": state})

    job = request_json(
        "POST",
        f"{base}/v1/projects/{project_id}/render",
        json={
            "outputName": variant["outputName"],
            "renderOptions": render_options(state, state["introTitle"]),
        },
    )
    finished = wait_for_job(base, job["jobId"])
    mark_render_complete(base, project_id, variant["outputName"], finished)
    return project_id, variant["outputName"]


def download_and_probe(base: str, project_id: str, output_name: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for filename in (output_name, "thumbnail.jpg"):
        response = requests.get(
            f"{base}/v1/projects/{project_id}/outputs/video",
            params={"filename": filename},
            timeout=300,
        )
        response.raise_for_status()
        (destination / f"{project_id}-{filename}").write_bytes(response.content)
    subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,width,height",
            "-of",
            "json",
            str(destination / f"{project_id}-{output_name}"),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://fortress-sextant.lan:8082")
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument(
        "--mandarin-font",
        type=Path,
        default=Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    update, new_listing = manifest["projects"]
    if not args.mandarin_font.is_file():
        raise FileNotFoundError(f"Mandarin font not found: {args.mandarin_font}")

    request_json("GET", f"{base}/healthz")
    english_project = request_json("GET", f"{base}/v1/projects/{update['projectIds']['english']}")
    mandarin_project = request_json("GET", f"{base}/v1/projects/{update['projectIds']['mandarin']}")
    voice_sources = {
        "english": english_project.get("state") or {},
        "mandarin": mandarin_project.get("state") or {},
    }

    source_names = {
        "7131 Front Gate.jpeg": "7131-front-gate.jpeg",
        "7131 Pool.jpg": "7131-pool.jpg",
        "7131 Gym.jpeg": "7131-gym.jpeg",
    }
    staged = args.asset_root / "7131-upload"
    staged.mkdir(parents=True, exist_ok=True)
    source_paths = []
    for destination_name, source_name in source_names.items():
        destination = staged / destination_name
        destination.write_bytes((args.asset_root / source_name).read_bytes())
        source_paths.append(destination)

    completed = []
    for language_key in ("english", "mandarin"):
        completed.append(
            update_7131(
                base,
                update["projectIds"][language_key],
                update["appendBeforeCallToAction"][language_key],
                source_paths,
                args.mandarin_font if language_key == "mandarin" else None,
            )
        )
    for language_key in ("english", "mandarin"):
        completed.append(
            create_9800(
                base,
                new_listing,
                language_key,
                args.asset_root / "9800-mls",
                args.asset_root / "7131-source",
                voice_sources[language_key],
                args.mandarin_font if language_key == "mandarin" else None,
            )
        )
    for project_id, output_name in completed:
        download_and_probe(base, project_id, output_name, args.download_dir)
    print(json.dumps({"completed": completed, "published": False}, indent=2))


if __name__ == "__main__":
    main()
