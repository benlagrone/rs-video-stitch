"""AI-media disclosure from file-bound render history, never editor defaults.

An absent history is deliberately unknown. A synthetic-voice selection in an
editor does not prove that an older MP4 contains that voice, and ordinary
HyperFrames/FFmpeg editing does not establish AI-generated media.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from app.storage import p_input, p_output

_AI_GENERATORS = (
    "stable-diffusion", "stable_diffusion", "comfyui", "vibevoice", "xtts",
    "azure-tts", "azure_voice", "azure-voice", "openai-tts", "dall-e",
    "dalle", "sora", "flux", "wan", "vace", "voice-gateway",
)
_SAFE_EDITORS = {"hyperframes", "ffmpeg", "blender", "object-vector", "deterministic"}


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    # Nanoseconds exceed JavaScript's safe-integer range.
    return {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns)}


def _matches_fingerprint(path: Path, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    actual = _fingerprint(path)
    return value.get("size") == actual["size"] and str(value.get("mtimeNs", "")) == actual["mtimeNs"]


@lru_cache(maxsize=64)
def _cached_sha256(filename: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(filename).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    stat = path.stat()
    return _cached_sha256(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _reasons(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip()[:400] for item in values if str(item).strip()))[:30]


def _result(ai: bool | None, reasons: object, *, basis: str = "unknown", required: bool | None = None) -> dict:
    return {
        "status": "ai" if ai is True else "original" if ai is False else "unknown",
        "isAigc": ai,
        "reasons": _reasons(reasons),
        "basis": basis,
        "required": bool(ai is True and (basis != "operator-review" if required is None else required)),
    }


def write_media_provenance(
    output_path: Path, *, ai_generated: bool | None, reasons: list[str],
    inputs: object = None, source: dict | None = None, expected_fingerprint: dict | None = None,
) -> Path:
    """Record the completed file's actual media inputs without modifying it."""
    output = Path(output_path)
    if not output.is_file() or output.is_symlink():
        raise ValueError("Media provenance requires a completed regular media file")
    if ai_generated is not None and not isinstance(ai_generated, bool):
        raise ValueError("AI media status must be true, false, or unknown")
    if source is not None and not isinstance(source, dict):
        raise ValueError("Media source history must be an object")
    fingerprint = _fingerprint(output)
    if expected_fingerprint is not None and not _matches_fingerprint(output, expected_fingerprint):
        raise ValueError("The selected video changed. Refresh its media review.")
    review = (source or {}).get("kind") == "operator-review"
    data = {
        "version": 1, "filename": output.name, "fingerprint": fingerprint,
        "aiGenerated": ai_generated, "reasons": _reasons(reasons),
        "basis": "operator-review" if review else "recorded", "recordedAt": time.time(),
    }
    if inputs is not None:
        data["inputs"] = inputs
    if source is not None:
        data["source"] = source
    destination = output.with_suffix(".media.json")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if not _matches_fingerprint(output, fingerprint):
            raise ValueError("The selected video changed while its media history was saved. Refresh its media review.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _record_assessment(path: Path, record: dict) -> dict:
    if record.get("version") != 1 or record.get("filename") != path.name:
        return _result(None, ["This video's saved media history is not valid for the selected file."])
    if not _matches_fingerprint(path, record.get("fingerprint")):
        return _result(None, ["The video changed after its media history was recorded. Review this version."])
    value = record.get("aiGenerated")
    if value is not None and not isinstance(value, bool):
        return _result(None, ["The video's saved AI-media status is invalid."])
    source = record.get("source")
    is_review = isinstance(source, dict) and source.get("kind") == "operator-review"
    basis = "operator-review" if record.get("basis") == "operator-review" or is_review else "recorded"
    return _result(value, record.get("reasons") or ["Media status recorded when this video was created."], basis=basis)


def _source_assessment(project_id: str, target: Path, source: object, seen: set[Path]) -> dict | None:
    if not isinstance(source, dict):
        return None
    name = source.get("filename") or source.get("sourceFilename")
    checksum = str(source.get("sha256") or source.get("sourceSha256") or "")
    if not isinstance(name, str) or Path(name).name != name or "\\" in name or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        return None
    parent = target.parent / name
    if parent == target or not parent.is_file() or parent.is_symlink() or parent.resolve().parent != target.parent.resolve():
        return None
    if _sha256(parent).lower() != checksum.lower():
        return None
    assessment = _artifact_assessment(project_id, parent, seen)
    if assessment["status"] == "unknown":
        return _result(None, ["The selected edit inherits an unreviewed source video.", *assessment["reasons"]], basis="inherited")
    return _result(assessment["isAigc"], [f"Inherited from source video {parent.name}.", *assessment["reasons"]], basis="inherited", required=assessment["required"])


def _artifact_assessment(project_id: str, path: Path, seen: set[Path]) -> dict:
    resolved = path.resolve()
    if resolved in seen or len(seen) >= 12:
        return _result(None, ["The video's source history contains a cycle or too many linked edits."])
    seen = {*seen, resolved}
    media_file = path.with_suffix(".media.json")
    if media_file.exists():
        record = _read(media_file)
        own = _record_assessment(path, record)
        if own["basis"] == "unknown":
            return own
        inherited = _source_assessment(project_id, path, record.get("source"), seen)
        # A file-level original-content confirmation cannot suppress a known
        # synthetic source. An unknown derivative can resolve after its exact
        # source receives an output-specific review.
        if inherited and (inherited["isAigc"] is True or own["isAigc"] is None):
            return inherited
        return own

    legacy_file = path.with_suffix(".highlight.json")
    legacy = _read(legacy_file)
    if legacy.get("filename") == path.name and legacy.get("sourceUnchanged") is True:
        # Legacy highlight manifests predate output fingerprints. A file
        # modified after the manifest cannot inherit its old source record.
        if path.stat().st_mtime_ns <= legacy_file.stat().st_mtime_ns:
            inherited = _source_assessment(project_id, path, {
                "filename": legacy.get("sourceFilename"), "sha256": legacy.get("sourceSha256"),
            }, seen)
            if inherited is not None:
                return inherited
    return _result(None, ["No file-bound media history is available for this video. Review whether its imagery, motion, or narration was AI-generated."])


def artifact_ai_status(project_id: str, filename: str) -> dict:
    """Assess the selected output and exact source lineage, without guessing."""
    if not filename or Path(filename).name != filename or "\\" in filename:
        raise ValueError("Choose a project output by filename")
    root = p_output(project_id).resolve()
    path = root / filename
    if not path.is_file() or path.is_symlink() or path.resolve().parent != root:
        raise FileNotFoundError("The selected project video is not available")
    result = _artifact_assessment(project_id, path, set())
    return {**result, "filename": filename, "fingerprint": _fingerprint(path)}


def _explicit_generation(metadata: dict) -> bool | None:
    for key in ("imageGeneration", "motionGeneration"):
        generation = metadata.get(key)
        if isinstance(generation, dict) and generation:
            method = str(generation.get("method") or generation.get("provider") or "").lower()
            if method in _SAFE_EDITORS and not generation.get("generative"):
                continue
            if key == "imageGeneration" and any(value in generation for value in ("model", "seed", "prompt")):
                return True
            if key == "motionGeneration" and any(value in generation for value in ("motionSeed", "sourceImageFingerprint", "model")):
                return True
    for key in ("aiGenerated", "ai_generated", "isAigc", "usesAiMedia"):
        if metadata.get(key) is True:
            return True
    generator = str(metadata.get("generator") or metadata.get("provider") or "").lower()
    if any(re.search(rf"(?:^|[^a-z]){re.escape(value)}(?:$|[^a-z])", generator) for value in _AI_GENERATORS):
        return True
    for key in ("aiGenerated", "ai_generated", "isAigc", "usesAiMedia"):
        if metadata.get(key) is False:
            return False
    if str(metadata.get("origin") or "").lower() in {"recorded", "camera-original", "original-photograph", "original-recording"}:
        return False
    return None


def asset_path_ai_status(asset_path: Path, *, provenance: dict | None = None) -> dict:
    """Assess one actual input selected by a render, using its origin record."""
    path = Path(asset_path)
    if not path.is_file() or path.is_symlink():
        return _result(None, ["Selected media input is missing or not a regular file."])
    sidecar = path.with_suffix(".media.json")
    if sidecar.exists():
        return _record_assessment(path, _read(sidecar))
    metadata = provenance if isinstance(provenance, dict) else {}
    fingerprint = metadata.get("fingerprint") or metadata.get("fileFingerprint")
    if fingerprint is not None and not _matches_fingerprint(path, fingerprint):
        return _result(None, [f"Saved media origin no longer matches {path.name}."])
    checksum = metadata.get("sha256") or metadata.get("fileSha256")
    if checksum and str(checksum).lower() != _sha256(path).lower():
        return _result(None, [f"Saved media origin no longer matches {path.name}."])
    ai = _explicit_generation(metadata)
    reason = (
        f"Selected input {path.name} has AI-generation provenance." if ai is True else
        f"Selected input {path.name} is declared as an original recording or image." if ai is False else
        f"Selected input {path.name} has no confirmed media-origin record."
    )
    return _result(ai, [reason], basis="recorded" if ai is not None else "unknown")


classify_asset_ai_status = asset_path_ai_status


def asset_ai_status(project_id: str, filename: str, subdir: str = "images") -> dict:
    if subdir not in {"images", "motion", "footage", "voiceovers", "leader", "logo"} or not filename or Path(filename).name != filename or "\\" in filename:
        raise ValueError("Choose a project media input by filename and supported category")
    root = (p_input(project_id) / subdir).resolve()
    path = root / filename
    if path.resolve().parent != root:
        raise ValueError("Media input path is outside its project")
    records = _read(p_input(project_id) / "asset_provenance.json")
    metadata = records.get(filename) or records.get(f"{subdir}/{filename}") or {}
    return asset_path_ai_status(path, provenance=metadata)


def is_lecrown_project(project_id: str, state: dict | None) -> bool:
    state = state if isinstance(state, dict) else {}
    profile = str(state.get("youtubeProfile") or "").lower()
    kind = str(state.get("projectType") or state.get("workflow") or "").lower()
    if profile in {"animals", "bible"} or kind.startswith(("bible", "animal")) or project_id.lower().startswith(("bible-", "animal-", "animals-")):
        return False
    if str(state.get("tiktokDefaultsProfile") or "").lower() == "lecrown":
        return True
    binding = state.get("hyperframes") or {}
    branding = " ".join(str(value) for value in (
        state.get("brand"), state.get("brokerage"), state.get("logoChoice"),
        binding.get("leadCardRight1") if isinstance(binding, dict) else "",
    ) if value)
    return bool(
        re.search(r"le\s*crown", branding, re.IGNORECASE)
        or project_id.lower().startswith("lecrown-")
        or state.get("propertyAddress")
        or profile in {"english", "mandarin"}
    )
