"""Build a Grok Imagine video request from a MediaStudio motion-previs shot.

The Blender shot is the timing authority. Its action beats become the prompt.
A photographed plate can pin the opening frame. Sampled 3D blockout frames are
sent as references so they cue placement without becoming the pixels of the clip.
"""
from __future__ import annotations

import base64
from fractions import Fraction
from typing import Any


MODEL = "grok-imagine-video-1.5"
MAX_DURATION_SECONDS = 15
MAX_KEYFRAMES = 4
MAX_REFERENCES = 14
MAX_IMAGE_BYTES = 8_000_000
TIME_GRID = Fraction(1, 3)
ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
RESOLUTIONS = {"480p", "720p", "1080p"}


class GrokVideoRequestError(ValueError):
    pass


def motion_keyframe_times(shot: dict[str, Any]) -> list[float]:
    """Interior beat boundaries, snapped to the 1/3-second grid Grok accepts."""
    duration = _duration_seconds(shot)
    slots: dict[Fraction, Fraction] = {}
    for start, end in _beat_bounds(shot):
        for raw in (start, end):
            if raw <= 0 or raw >= duration:
                continue
            snapped = _snap_to_grid(raw, duration)
            if snapped is None:
                continue
            error = abs(Fraction(raw).limit_denominator(1000) - snapped)
            current = slots.get(snapped)
            if current is None or error < current:
                slots[snapped] = error
    ordered = sorted(slots)
    if len(ordered) <= MAX_KEYFRAMES:
        return [float(item) for item in ordered]
    chosen_indexes = []
    for index in range(MAX_KEYFRAMES):
        chosen_indexes.append(round(index * (len(ordered) - 1) / (MAX_KEYFRAMES - 1)))
    unique: list[Fraction] = []
    for index in chosen_indexes:
        item = ordered[index]
        if item not in unique:
            unique.append(item)
    return [float(item) for item in unique]


def build_grok_video_request(
    shot: dict[str, Any],
    *,
    plate: tuple[bytes, str, str] | None = None,
    end_plate: tuple[bytes, str, str] | None = None,
    appearance: list[tuple[bytes, str, str]] | None = None,
    blockout_frames: list[tuple[float, bytes, str]] | None = None,
    pin_blockout: bool = False,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
) -> dict[str, Any]:
    """Return the xAI request body plus the review plan. Image tuples are (bytes, media type, label)."""
    if resolution not in RESOLUTIONS:
        raise GrokVideoRequestError(f"Unsupported resolution: {resolution}")
    if aspect_ratio not in ASPECT_RATIOS:
        raise GrokVideoRequestError(f"Unsupported aspect ratio: {aspect_ratio}")

    duration = _duration_seconds(shot)
    keyframe_times = motion_keyframe_times(shot)
    appearance = list(appearance or [])
    blockout_frames = list(blockout_frames or [])
    if len(appearance) > MAX_REFERENCES:
        raise GrokVideoRequestError(f"A Grok video accepts at most {MAX_REFERENCES} reference images")

    warnings: list[str] = []
    pins: dict[str, Any] = {"first": None, "last": None, "keyframes": []}
    body: dict[str, Any] = {
        "model": MODEL,
        "duration": duration,
        "resolution": resolution,
    }

    if plate is not None:
        image_bytes, media_type, label = plate
        body["image"] = {"url": _data_uri(image_bytes, media_type)}
        pins["first"] = label
    if end_plate is not None:
        image_bytes, media_type, label = end_plate
        body["last_frame"] = {"url": _data_uri(image_bytes, media_type)}
        pins["last"] = label

    reference_entries: list[dict[str, str]] = []
    reference_images: list[dict[str, str]] = []
    next_index = 1 if plate is not None else 0
    for image_bytes, media_type, label in appearance:
        reference_images.append({"url": _data_uri(image_bytes, media_type)})
        reference_entries.append({"token": f"<IMAGE_{next_index}>", "role": "appearance", "label": label})
        next_index += 1

    pinned_blockout = pin_blockout and bool(blockout_frames)
    if pinned_blockout:
        selected = _blockout_at_times(blockout_frames, keyframe_times)
        if len(selected) > MAX_KEYFRAMES:
            selected = selected[:MAX_KEYFRAMES]
            warnings.append("Only the first four blockout keyframes were pinned.")
        body["keyframes"] = [
            {"image": {"url": _data_uri(image_bytes, media_type)}, "timestamp_s": timestamp}
            for timestamp, image_bytes, media_type in selected
        ]
        pins["keyframes"] = [timestamp for timestamp, _, _ in selected]
        warnings.append(
            "Pinned blockout frames appear in the finished video at those times. "
            "Leave pinBlockout off when the 3D render should guide motion without replacing the picture."
        )
    else:
        room = MAX_REFERENCES - len(reference_images)
        usable = blockout_frames[:room]
        if len(usable) < len(blockout_frames):
            warnings.append("Some 3D blockout frames were left out because the reference list is full.")
        for timestamp, image_bytes, media_type in usable:
            reference_images.append({"url": _data_uri(image_bytes, media_type)})
            reference_entries.append({
                "token": f"<IMAGE_{next_index}>",
                "role": "blockout",
                "label": f"3D blockout at {timestamp:.3f}s",
            })
            next_index += 1
        if usable:
            warnings.append(
                "3D blockout frames are references. Grok uses them for placement and does not copy their pixels into the clip."
            )

    if reference_images:
        body["reference_images"] = reference_images

    uses_reference_mode = bool(reference_images or end_plate is not None or body.get("keyframes"))
    if uses_reference_mode and body["resolution"] == "1080p":
        body["resolution"] = "720p"
        warnings.append("1080p is only available for text-to-video and a single pinned first frame. This request uses 720p.")
    if uses_reference_mode or plate is None:
        body["aspect_ratio"] = aspect_ratio

    if plate is None and not reference_images:
        warnings.append("No plate or reference image was attached. The 3D shot controls this clip through the timed prompt only.")

    prompt = _prompt(shot, duration, reference_entries, has_plate=plate is not None, pinned_blockout=pinned_blockout)
    body["prompt"] = prompt
    return {
        "body": body,
        "plan": {
            "model": MODEL,
            "shotId": str(shot.get("id") or ""),
            "title": str(shot.get("title") or ""),
            "duration": duration,
            "resolution": body["resolution"],
            "aspectRatio": body.get("aspect_ratio"),
            "prompt": prompt,
            "keyframeTimes": keyframe_times,
            "pins": pins,
            "references": reference_entries,
            "warnings": warnings,
        },
    }


def _duration_seconds(shot: dict[str, Any]) -> int:
    try:
        raw = float(shot.get("duration_seconds"))
    except (TypeError, ValueError) as exc:
        raise GrokVideoRequestError("The motion shot needs duration_seconds") from exc
    duration = int(round(raw))
    if duration < 1 or duration > MAX_DURATION_SECONDS:
        raise GrokVideoRequestError(f"Grok video duration must be 1 to {MAX_DURATION_SECONDS} seconds")
    return duration


def _beat_bounds(shot: dict[str, Any]) -> list[tuple[float, float]]:
    beats = shot.get("action_beats") or []
    if not isinstance(beats, list) or not beats:
        raise GrokVideoRequestError("The motion shot needs at least one action beat")
    bounds = []
    for beat in beats:
        if not isinstance(beat, dict):
            raise GrokVideoRequestError("Each action beat must include a time range and an action")
        spec = str(beat.get("seconds") or "")
        try:
            start_text, end_text = spec.split("-", 1)
            start, end = float(start_text), float(end_text)
        except (TypeError, ValueError) as exc:
            raise GrokVideoRequestError(f"Action beat has an unreadable time range: {spec}") from exc
        if end <= start:
            raise GrokVideoRequestError(f"Action beat ends before it starts: {spec}")
        bounds.append((start, end))
    return bounds


def _snap_to_grid(seconds: float, duration: int) -> Fraction | None:
    slot = int(Fraction(seconds).limit_denominator(1000) / TIME_GRID + Fraction(1, 2))
    snapped = slot * TIME_GRID
    if snapped <= 0 or snapped >= duration:
        return None
    return snapped


def _blockout_at_times(
    frames: list[tuple[float, bytes, str]],
    times: list[float],
) -> list[tuple[float, bytes, str]]:
    selected = []
    for timestamp in times:
        nearest = min(frames, key=lambda item: abs(item[0] - timestamp))
        if abs(nearest[0] - timestamp) > 0.2:
            continue
        selected.append((timestamp, nearest[1], nearest[2]))
    return selected


def _data_uri(image_bytes: bytes, media_type: str) -> str:
    if not image_bytes:
        raise GrokVideoRequestError("An image attached to the Grok request is empty")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise GrokVideoRequestError(f"An image attached to the Grok request exceeds {MAX_IMAGE_BYTES} bytes")
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise GrokVideoRequestError(f"Grok image references must be PNG, JPEG, or WebP, not {media_type}")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _prompt(
    shot: dict[str, Any],
    duration: int,
    references: list[dict[str, str]],
    *,
    has_plate: bool,
    pinned_blockout: bool,
) -> str:
    lines = [
        f"Shot: {shot.get('title') or shot.get('id') or 'untitled'}.",
        f"Duration is exactly {duration} seconds.",
        f"Camera and framing: {str(shot.get('framing') or 'locked camera').strip()}",
        "Follow the timing below. Do not add people, objects, cuts, or camera moves that this shot does not name.",
    ]
    actors = shot.get("actors") or []
    if isinstance(actors, list) and actors:
        lines.append("People:")
        for actor in actors:
            if not isinstance(actor, dict):
                continue
            name = str(actor.get("id") or "actor")
            role = str(actor.get("role") or "").strip()
            appearance = str(actor.get("appearance_prompt") or "").strip()
            if appearance and role and role.lower() in appearance.lower():
                detail = appearance
            else:
                detail = ". ".join(part for part in (role, appearance) if part)
            lines.append(f"- {name}: {detail or 'as established by the reference images'}")
    lines.append("Timing:")
    for start, end in _beat_bounds(shot):
        lines.append(f"- {_trim(start)} to {_trim(end)} seconds: {_action_for_range(shot, start, end)}")
    zones = [str(item).strip() for item in (shot.get("collision_zones") or []) if str(item).strip()]
    if zones:
        lines.append("Staging:")
        lines.extend(f"- {zone}" for zone in zones)
    checks = [str(item).strip() for item in (shot.get("acceptance_checks") or []) if str(item).strip()]
    if checks:
        lines.append("The finished clip must show:")
        lines.extend(f"- {check}" for check in checks)
    if has_plate:
        lines.append(
            "The opening frame is pinned as the background photograph. Keep its room, crop, and lighting. "
            "Add only the people and moving objects named in this shot, on top of that background."
        )
    if references:
        lines.append("Images:")
        for entry in references:
            if entry["role"] == "blockout":
                lines.append(
                    f"- {entry['token']} is the 3D motion layer, {entry['label']}. "
                    "Use it only for where the people and moving objects sit on the background. "
                    "Do not copy its materials, lighting, or model style, and do not replace the background."
                )
            else:
                lines.append(f"- {entry['token']} is the appearance reference {entry['label']}. Match its people, wardrobe, and room.")
    if pinned_blockout:
        lines.append("The pinned interior frames are exact moments from the motion blockout. Land on them, then continue the timed action.")
    return "\n".join(lines)


def _action_for_range(shot: dict[str, Any], start: float, end: float) -> str:
    for beat in shot.get("action_beats") or []:
        if not isinstance(beat, dict):
            continue
        try:
            raw_start, raw_end = str(beat.get("seconds") or "").split("-", 1)
        except ValueError:
            continue
        if abs(float(raw_start) - start) < 0.001 and abs(float(raw_end) - end) < 0.001:
            return str(beat.get("action") or "").strip()
    return "hold"


def _trim(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"
