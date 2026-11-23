"""Plan listing-photo enhancement, then phrase one Grok edit. Staging and video come later."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable


ACTIONS = {"enhance", "polish", "leave"}
ACTION_ALIASES = {
    "stage": "enhance",
    "furnish": "enhance",
    "full": "enhance",
    "style": "polish",
    "light": "polish",
    "skip": "leave",
    "keep": "leave",
    "original": "leave",
}
BATCH_SIZE = 6

PRESERVATION = (
    "Use case: precise-object-edit. Image 1 is the EDIT TARGET, a real listing photograph. "
    "This step is photo enhancement only. Do not add furniture, decor, people, or landscaping. "
    "Return one full-frame photograph with the same camera, viewpoint, and contents. "
    "Correct exposure, white balance, and color so the space looks as it would in person. "
    "Recover detail in blown windows and bright exteriors without inventing a view, sky, or neighboring buildings. "
    "Straighten a mild camera tilt while keeping the same framing and proportions. "
    "Preserve walls and paint, window and door counts, floors, ceilings, fans, light fittings, trim, "
    "fireplace, columns, cabinetry, plumbing, outlets, garden, trees, fences, driveway, and existing wear. "
    "Do not remodel, repair, repaint, landscape, add or remove windows, or hide defects. "
    "Match the white balance of the other photos of this same space. Add no text."
)

ANALYSIS_INSTRUCTIONS = """
You plan the first step for a listing: photo enhancement. Virtual staging and video come later. Do not plan furniture.

Look at the house and the area before deciding how each photo should be corrected. Read the light, white balance, window exposure, verticals, and color cast. Read the area from exterior photos and the notes: the real sky, street, lawn, and neighboring houses. Keep that weather and that block. An overcast day stays overcast. Warm wood floors stay warm; do not cool the house into gray.

Group photos of the same space so their white balance matches. Name one anchor photo for that match.

Choose the treatment from the photograph:
- enhance: the photo needs a real correction, such as a dark interior, blown-out windows, a color cast, or tilted verticals.
- polish: the photo is already close and needs only a small exposure or color adjustment. Keep the same weather and the same view.
- leave: a close architectural or mechanical detail, signage, a defect, or a photo that is already clean. Do not hide defects or beautify damage.

Each enhance or polish brief names the correction for that photo. It must not ask for furniture, remodeling, sky replacement, or new landscaping. Leave briefs say why the photo stays as photographed.

Return JSON only:
{
  "styleName": "short enhancement look",
  "style": "white balance, contrast, and how far to push the correction",
  "styleReason": "what you saw in the house and the area",
  "photos": [
    {"filename": "exact filename", "group": "space-name", "anchor": "anchor filename", "action": "enhance|polish|leave", "brief": "one specific correction"}
  ]
}
""".strip()


class VirtualStagingError(ValueError):
    pass


def analysis_messages(
    photos: list[dict[str, str]],
    *,
    property_address: str,
    area: str,
    locked_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not photos:
        raise VirtualStagingError("Choose listing photos to read")
    notes = [
        f"Property: {property_address.strip() or 'not supplied'}.",
        f"Area notes: {area.strip() or 'Infer the area from the address and from what the exterior photos show.'}",
    ]
    if locked_plan:
        groups = sorted({str(item.get('group') or '') for item in locked_plan.get('photos') or [] if item.get('group')})
        notes.append(
            "The enhancement look is already decided. Do not change it. "
            f"Style name: {locked_plan.get('styleName')}. Style: {locked_plan.get('style')}. "
            f"Reason: {locked_plan.get('styleReason')}. "
            f"Reuse one of these groups when a photo is another angle of that space: {', '.join(groups) or 'none yet'}."
        )
    notes.append("Decide every photo below. Use the exact filename.")
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(notes)}]
    for photo in photos:
        content.append({"type": "text", "text": f"Photo {photo['filename']}:"})
        content.append({"type": "image_url", "image_url": {"url": photo["dataUri"]}})
    return [
        {"role": "system", "content": ANALYSIS_INSTRUCTIONS},
        {"role": "user", "content": content},
    ]


def plan_listing(
    photos: list[dict[str, str]],
    *,
    property_address: str,
    area: str,
    complete: Callable[[list[dict[str, Any]]], Any],
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    if batch_size < 1:
        raise VirtualStagingError("Batch size must be at least 1")
    filenames = [photo["filename"] for photo in photos]
    if len(filenames) != len(set(filenames)):
        raise VirtualStagingError("Each listing photo must have its own filename")
    combined: dict[str, Any] | None = None
    for start in range(0, len(photos), batch_size):
        batch = photos[start:start + batch_size]
        raw = complete(analysis_messages(
            batch,
            property_address=property_address,
            area=area,
            locked_plan=combined,
        ))
        parsed = parse_staging_plan(
            raw,
            [photo["filename"] for photo in batch],
            property_address=property_address,
            area=area,
            prior_photos=[] if combined is None else combined["photos"],
        )
        combined = parsed if combined is None else merge_plans(combined, parsed)
    if combined is None:
        raise VirtualStagingError("Choose listing photos to read")
    return combined


def merge_plans(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    seen = {item["filename"] for item in base["photos"]}
    photos = list(base["photos"])
    for item in extra["photos"]:
        if item["filename"] in seen:
            raise VirtualStagingError(f"Duplicate enhancement decision for {item['filename']}")
        photos.append(item)
        seen.add(item["filename"])
    return {**base, "photos": photos}


def parse_staging_plan(
    raw: Any,
    filenames: Iterable[str],
    *,
    property_address: str,
    area: str,
    prior_photos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = _load_payload(raw)
    expected = [str(name) for name in filenames]
    prior = [item for item in (prior_photos or []) if item.get("filename") not in expected]
    if not expected:
        raise VirtualStagingError("Choose listing photos to read")
    style_name = str(payload.get("styleName") or payload.get("style_name") or "").strip()
    style = str(payload.get("style") or "").strip()
    reason = str(payload.get("styleReason") or payload.get("style_reason") or "").strip()
    if not style_name or not style or not reason:
        raise VirtualStagingError("The enhancement plan needs a look, how far to push it, and why the house and area call for it")
    incoming = payload.get("photos")
    if incoming is None:
        incoming = payload.get("tasks")
    if not isinstance(incoming, list):
        raise VirtualStagingError("The enhancement plan needs one decision per photo")
    by_name: dict[str, dict[str, Any]] = {}
    for item in incoming:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        if filename in expected and filename not in by_name:
            by_name[filename] = item
    known = expected + [item["filename"] for item in prior]
    photos = [
        _normalize_photo(by_name.get(filename), filename, known)
        for filename in expected
    ]
    _check_anchors(photos, prior)
    return {
        "property": property_address.strip(),
        "area": area.strip(),
        "styleName": style_name,
        "style": style,
        "styleReason": reason,
        "step": "photo-enhancement",
        "photos": photos,
    }


def edit_prompt(plan: dict[str, Any], photo: dict[str, Any]) -> str:
    action = photo.get("action")
    if action == "leave":
        raise VirtualStagingError(f"{photo.get('filename') or 'This photo'} stays as photographed")
    if action not in {"enhance", "polish"}:
        raise VirtualStagingError("Only enhance and polish photos are edited")
    brief = str(photo.get("brief") or "").strip()
    if not brief:
        raise VirtualStagingError(f"{photo.get('filename')} needs an enhancement brief")
    return "\n".join([
        PRESERVATION,
        f"Property: {plan.get('property') or 'unspecified'}. Area: {plan.get('area') or 'unspecified'}.",
        f"Look: {plan.get('styleName')}. {plan.get('style')}.",
        (
            f"This is {photo['filename']} in the {photo['group']} group. "
            f"Match the white balance of the anchor photo {photo['anchor']}."
        ),
        brief,
    ])


def staged_filename(filename: str, suffix: str = ".jpg") -> str:
    stem = re.sub(r"\.[^.]+$", "", filename)
    stem = re.sub(r"-(?:enhanced|virtually-staged)$", "", stem, flags=re.IGNORECASE)
    extension = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{stem}-enhanced{extension.lower()}"


def is_staged_filename(filename: str) -> bool:
    return bool(re.search(r"-(?:enhanced|virtually-staged)\.[a-z0-9]+$", filename, re.IGNORECASE))


def _load_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VirtualStagingError("Grok did not return an enhancement plan") from exc
    if not isinstance(payload, dict):
        raise VirtualStagingError("Grok did not return an enhancement plan")
    return payload


def _normalize_photo(item: dict[str, Any] | None, filename: str, expected: list[str]) -> dict[str, Any]:
    if item is None:
        return {
            "filename": filename,
            "group": "unreviewed",
            "anchor": filename,
            "action": "leave",
            "brief": "Not selected after reviewing this photo.",
        }
    action = ACTION_ALIASES.get(str(item.get("action") or "").strip().lower(), str(item.get("action") or "").strip().lower())
    if action not in ACTIONS:
        raise VirtualStagingError(f"{filename} needs an action of enhance, polish, or leave")
    brief = str(item.get("brief") or "").strip()
    group = str(item.get("group") or item.get("g") or "").strip()
    anchor = str(item.get("anchor") or "").strip()
    if action == "leave":
        return {
            "filename": filename,
            "group": group or "leave",
            "anchor": filename,
            "action": "leave",
            "brief": brief or "Leave this photograph unchanged.",
        }
    if not brief or not group:
        raise VirtualStagingError(f"{filename} needs a group and a specific enhancement brief")
    if len(brief) > 4000:
        raise VirtualStagingError(f"The brief for {filename} is too long")
    if anchor not in expected:
        anchor = filename
    return {
        "filename": filename,
        "group": group,
        "anchor": anchor,
        "action": action,
        "brief": brief,
    }


def _check_anchors(photos: list[dict[str, Any]], prior: list[dict[str, Any]] | None = None) -> None:
    by_name = {item["filename"]: item for item in [*(prior or []), *photos]}
    for item in photos:
        if item["action"] == "leave":
            continue
        anchor = by_name.get(item["anchor"])
        if anchor is None or anchor["group"] != item["group"] or anchor["action"] == "leave":
            raise VirtualStagingError(
                f"{item['filename']} must use an enhance or polish anchor from the {item['group']} group"
            )
