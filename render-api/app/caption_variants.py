"""Carry file-bound spoken captions through deterministic video derivatives."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from app.storage import p_input, p_output, p_work, read_project_state
from app.video_captions import (
    CAPTION_VERSION, CaptionCue, CaptionError, _duration, _probe, _publish_bundle,
    burn_captions, transcribe_video,
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sidecars(video: Path) -> dict[str, Path]:
    base = video.with_suffix("")
    return {key: Path(str(base) + suffix) for key, suffix in (
        ("srt", ".srt"), ("ass", ".ass"), ("json", ".captions.json"),
    )}


def _recognized_manifest(video: Path, value: dict) -> bool:
    checksum = value.get("videoSha256") or value.get("outputSha256")
    raw_checksum = value.get("inputSha256") or value.get("sourceSha256")
    return (
        value.get("version") == CAPTION_VERSION
        and value.get("outputFilename") == video.name
        and value.get("sourceKind") in {"whisper", "script"}
        and isinstance(checksum, str) and re.fullmatch(r"[a-f0-9]{64}", checksum) is not None
        and isinstance(raw_checksum, str) and re.fullmatch(r"[a-f0-9]{64}", raw_checksum) is not None
        and value.get("files") == {key: path.name for key, path in _sidecars(video).items()}
        and isinstance(value.get("cues"), list)
    )


@dataclass(frozen=True)
class CaptionSource:
    project_id: str
    source: Path
    source_sha256: str
    source_identity: tuple[int, int]
    clean_source: Path
    clean_sha256: str
    source_type: str
    cues: tuple[CaptionCue, ...]
    language: str | None
    enabled: bool
    content_windows: tuple[tuple[float, float], ...] | None = None


def _verify_source(prepared: CaptionSource) -> None:
    for path in {prepared.source, prepared.clean_source}:
        if not path.is_file() or path.is_symlink():
            raise CaptionError("The caption source changed or is unavailable; render the derivative again")
    if _identity(prepared.source) != prepared.source_identity or _hash(prepared.source) != prepared.source_sha256:
        raise CaptionError("The selected master changed during caption rendering; no derivative was published")
    if _hash(prepared.clean_source) != prepared.clean_sha256:
        raise CaptionError("The retained clean master changed during caption rendering; no derivative was published")


def verify_derivative_source(prepared: CaptionSource | None) -> None:
    """Recheck retained clean pixels before publishing caption-on or off edits."""
    if prepared is not None:
        _verify_source(prepared)


def _read_cues(manifest: dict, duration: float) -> tuple[CaptionCue, ...]:
    result = []
    previous = 0.0
    for value in manifest["cues"]:
        try:
            start, end, text = float(value["start"]), float(value["end"]), value["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptionError("The source caption timings are invalid; re-render the master with captions first") from exc
        if not all(math.isfinite(number) for number in (start, end)) or start < 0 or start < previous - 0.015 or end <= start or end > duration + 0.05 or not isinstance(text, str) or not text.strip():
            raise CaptionError("The source caption timings are invalid; re-render the master with captions first")
        result.append(CaptionCue(max(0.0, start), min(duration, end), text))
        previous = end
    if not result:
        raise CaptionError("The source has no spoken captions; re-render the master with captions first")
    return tuple(result)


def _content_windows(values, duration: float) -> tuple[tuple[float, float], ...] | None:
    if values is None:
        return None
    if not isinstance(values, (list, tuple)):
        raise CaptionError("The source content windows are invalid; re-render the master with captions first")
    result = []
    previous = 0.0
    for value in values:
        try:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError("Two bounds are required")
            start, end = float(value[0]), float(value[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise CaptionError("The source content windows are invalid; re-render the master with captions first") from exc
        if not all(math.isfinite(number) for number in (start, end)) or start < 0 or start < previous - 1e-6 or end <= start or end > duration + 0.05:
            raise CaptionError("The source content windows are invalid; re-render the master with captions first")
        end = min(duration, end)
        if end > start:
            result.append((start, end))
        previous = end
    return tuple(result)


def _source_kind(project_id: str, source: Path) -> str:
    # Prefer file-bound inputs over a project that may have been edited since
    # this MP4 was rendered. Never obtain photo words from the current script.
    media = _read(source.with_suffix(".media.json"))
    fingerprint = media.get("fingerprint") or {}
    stat = source.stat()
    if fingerprint.get("size") == stat.st_size and str(fingerprint.get("mtimeNs")) == str(stat.st_mtime_ns):
        kinds = {item.get("kind") for item in media.get("inputs", []) if isinstance(item, dict)}
        if "source-footage" in kinds:
            return "whisper"
        if kinds.intersection({"scene-image", "scene-motion", "prerecorded-narration"}):
            return "narration-script"
    document = _read(p_input(project_id) / "scenes.json")
    return "whisper" if document.get("sourceFootage") else "narration-script"


def prepare_derivative_captions(project_id: str, source: Path, *, enabled: bool, language: str | None = None) -> CaptionSource | None:
    """Resolve a verified clean visual source, including when captions are off."""
    source = Path(source)
    if source.is_symlink():
        raise CaptionError("Choose a finished regular video in this project")
    source = source.resolve()
    if source.parent != p_output(project_id).resolve() or not source.is_file():
        raise CaptionError("Choose a finished regular video in this project")
    manifest_path = _sidecars(source)["json"]
    if manifest_path.is_symlink():
        if enabled:
            raise CaptionError("The source caption metadata must be a regular project file")
        return None
    manifest = _read(manifest_path)
    recognized = _recognized_manifest(source, manifest)
    if not enabled and not recognized:
        return None
    state = read_project_state(project_id) or {}
    language = language or (state.get("renderOptions") or {}).get("ttsLanguage") or state.get("language")
    checksum, identity = _hash(source), _identity(source)
    clean, clean_checksum, cues, content_windows = source, checksum, (), None
    if recognized:
        if (manifest.get("videoSha256") or manifest.get("outputSha256")) != checksum:
            raise CaptionError("The source captions belong to an older video; re-render the master with captions first")
        clean = p_work(project_id) / f"uncaptioned_{source.name}"
        clean_checksum = str(manifest.get("inputSha256") or manifest.get("sourceSha256"))
        if not clean.is_file() or clean.is_symlink() or _hash(clean) != clean_checksum:
            target = "on" if enabled else "off"
            raise CaptionError(f"The retained clean master is unavailable; re-render the master with captions {target}")
        kind = "whisper" if manifest["sourceKind"] == "whisper" else "narration-script"
        if manifest.get("contentWindows") is not None:
            content_windows = _content_windows(manifest["contentWindows"], _duration(_probe(source)))
        if enabled and kind == "narration-script":
            cues = _read_cues(manifest, _duration(_probe(source)))
    else:
        if manifest_path.exists():
            raise CaptionError("The source caption metadata is invalid; re-render the master with captions first")
        kind = _source_kind(project_id, source)
        if kind == "narration-script":
            raise CaptionError("This photo video has no matching narration captions; re-render the master with captions first")
    prepared = CaptionSource(project_id, source, checksum, identity, clean, clean_checksum, kind, cues, language, bool(enabled), content_windows)
    _verify_source(prepared)
    return prepared


def _slice_script_cue(cue: CaptionCue, start: float, end: float) -> CaptionCue | None:
    left, right = max(cue.start, start), min(cue.end, end)
    if right <= left:
        return None
    if left <= cue.start and right >= cue.end:
        return cue
    # The master's script timings are proportional. Preserve that same method
    # when a highlight selects only part of a phrase, rather than displaying
    # words from outside the selected audio window.
    words = list(re.finditer(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff]+", cue.text))
    if not words:
        return None
    step = (cue.end - cue.start) / len(words)
    indexes = [index for index in range(len(words)) if left <= cue.start + (index + 0.5) * step < right]
    if not indexes:
        return None
    first, last = indexes[0], indexes[-1]
    return CaptionCue(max(left, cue.start + first * step), min(right, cue.start + (last + 1) * step), cue.text[words[first].start():words[last].end()])


def remap_script_cues(cues: tuple[CaptionCue, ...] | list[CaptionCue], windows: list[tuple[float, float, float]] | None, duration: float) -> list[CaptionCue]:
    if windows is None:
        return [CaptionCue(cue.start, min(duration, cue.end), cue.text) for cue in cues if 0 <= cue.start < min(duration, cue.end)]
    result = []
    previous_end = 0.0
    for raw_start, raw_end, raw_output in windows:
        start, end, output = float(raw_start), float(raw_end), float(raw_output)
        if not all(math.isfinite(value) for value in (start, end, output)) or start < 0 or end <= start or output < previous_end - 0.015 or output + end - start > duration + 0.05:
            raise CaptionError("The derivative caption timeline is invalid")
        for cue in cues:
            selected = _slice_script_cue(cue, start, end)
            if selected:
                result.append(CaptionCue(output + selected.start - start, output + selected.end - start, selected.text))
        previous_end = output + end - start
    return result


def derivative_content_windows(prepared: CaptionSource, windows: list[tuple[float, float, float]] | None, duration: float) -> list[tuple[float, float]] | None:
    """Fence words to selected content, excluding both source and new cards."""
    if windows is None:
        if prepared.content_windows is None:
            return None
        return [(start, min(end, duration)) for start, end in prepared.content_windows if start < min(end, duration)]
    result = []
    previous = 0.0
    for raw_start, raw_end, raw_output in windows:
        start, end, output = float(raw_start), float(raw_end), float(raw_output)
        if not all(math.isfinite(value) for value in (start, end, output)) or start < 0 or end <= start or output < previous - 0.015 or output + end - start > duration + 0.05:
            raise CaptionError("The derivative caption timeline is invalid")
        source_windows = prepared.content_windows if prepared.content_windows is not None else ((start, end),)
        for content_start, content_end in source_windows:
            left, right = max(start, content_start), min(end, content_end)
            if right > left:
                target_start, target_end = output + left - start, min(duration, output + right - start)
                if target_end > target_start:
                    if result and abs(target_start - result[-1][1]) < 1e-6:
                        result[-1] = (result[-1][0], target_end)
                    else:
                        result.append((target_start, target_end))
        previous = output + end - start
    return result


@contextmanager
def _output_lock(video: Path, work: Path):
    lock_root = work / "caption-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (hashlib.sha256(str(video.resolve()).encode()).hexdigest() + ".lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def apply_derivative_captions(prepared: CaptionSource, raw_output: Path, destination: Path, *, windows: list[tuple[float, float, float]] | None = None, progress_cb=None, log=None) -> dict:
    """Publish spoken subtitles only after the selected master stays verified."""
    if not prepared.enabled:
        raise CaptionError("Spoken captions were not enabled for this derivative")
    destination, raw_output = Path(destination), Path(raw_output)
    if destination.is_symlink() or destination.resolve() == prepared.source or destination.resolve().parent != p_output(prepared.project_id).resolve():
        raise CaptionError("Caption derivatives must preserve the original master")
    work = p_work(prepared.project_id)
    work.mkdir(parents=True, exist_ok=True)
    retained = work / f"uncaptioned_{destination.name}"
    if retained.is_symlink() or any(path.is_symlink() for path in _sidecars(destination).values()):
        raise CaptionError("Caption output and sidecars must be regular project files")
    _verify_source(prepared)
    if progress_cb:
        progress_cb(0.0)
    with tempfile.TemporaryDirectory(prefix="caption-derivative-", dir=work) as temporary:
        scratch = Path(temporary)
        raw_copy = scratch / f"uncaptioned_{destination.name}"
        before = _hash(raw_output)
        shutil.copyfile(raw_output, raw_copy)
        if _hash(raw_copy) != before or _hash(raw_output) != before:
            raise CaptionError("The derivative changed while its caption input was prepared")
        metadata = _probe(raw_copy)
        duration = _duration(metadata)
        content_windows = derivative_content_windows(prepared, windows, duration)
        video = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "video"), {})
        if prepared.source_type == "whisper":
            cues = transcribe_video(raw_copy, work / "caption-transcripts", language=prepared.language, progress_cb=(lambda value: progress_cb(0.85 * value)) if progress_cb else None, content_windows=content_windows)
        else:
            cues = remap_script_cues(prepared.cues, windows, duration)
        if not cues:
            raise CaptionError("No spoken captions occur in the selected video sections")
        _verify_source(prepared)
        if progress_cb:
            progress_cb(0.9)
        staged = scratch / destination.name
        report = burn_captions(raw_copy, staged, cues, width=int(video.get("width") or 0), height=int(video.get("height") or 0), sidecar_base=staged.with_suffix(""), log=log, source=prepared.source_type, content_windows=content_windows)
        report.update(parentFilename=prepared.source.name, parentSha256=prepared.source_sha256)
        _sidecars(staged)["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
        with _output_lock(destination, work):
            _verify_source(prepared)
            pairs = [(raw_copy, retained), *[(path, _sidecars(destination)[key]) for key, path in _sidecars(staged).items()], (staged, destination)]
            _publish_bundle(pairs, scratch)
    if progress_cb:
        progress_cb(1.0)
    return report


def retire_caption_sidecars(final_path: Path, *, history_dir: Path | None = None) -> Path | None:
    """Archive only this engine's old bundle after a successful caption-off render."""
    final_path = Path(final_path)
    paths = _sidecars(final_path)
    if not paths["json"].is_file() or paths["json"].is_symlink():
        return None
    manifest = _read(paths["json"])
    if not _recognized_manifest(final_path, manifest):
        return None
    work = final_path.parent.parent / "work"
    history = Path(history_dir) if history_dir is not None else work / "caption-history"
    with _output_lock(final_path, work):
        if _read(paths["json"]) != manifest:
            raise CaptionError("The caption bundle changed while its history was being archived")
        existing = [path for path in paths.values() if path.is_file() and not path.is_symlink()]
        history.mkdir(parents=True, exist_ok=True)
        checksum = manifest.get("videoSha256") or manifest["outputSha256"]
        archive = history / f"{checksum[:16]}-{uuid.uuid4().hex[:10]}"
        archive.mkdir()
        moved = []
        try:
            for path in existing:
                target = archive / path.name
                os.replace(path, target)
                moved.append((target, path))
        except OSError:
            for archived, original in reversed(moved):
                os.replace(archived, original)
            raise
    return archive
