"""Recompose a complete saved video on one portrait canvas.

Verified retained proxies keep the master's established colour conversion and
timing. Native files establish provenance; raw phone HDR is never silently
substituted for an approved prepared proxy. Audio is taken once from the saved
master, independently of every visual framing boundary.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from app.highlight_sources import native_source_map, split_window, _sample, _similar
from app.media_ai_policy import is_lecrown_project
from app.storage import p_input, p_work, read_project_state
from app.vertical_framing import analyze_framing, vertical_filter

FPS = 30


def _optional_identity(path: Path) -> dict | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {"unsafeReplacement": True}
    return _identity(path) if path.exists() else None


class _VariantTransaction:
    def __init__(self, previous, original):
        self.previous = previous
        self.original = original
        self.published: dict[Path, dict | None] = {}

    def expect(self, identities: dict[Path, dict | None]) -> None:
        self.published.update(identities)

    def record(self, paths: list[Path]) -> None:
        self.expect({path: _optional_identity(path) for path in paths})

    def assert_unchanged(self, paths: list[Path]) -> None:
        if any(_optional_identity(path) != self.published.get(path, self.original.get(path)) for path in paths):
            raise RuntimeError("The previous variant bundle changed during rendering; the newer files were preserved")

    def rollback(self) -> None:
        for target, expected in reversed(list(self.published.items())):
            current = _optional_identity(target)
            # Restore only bytes this transaction is known to have published.
            # A later/unrelated writer's replacement must never be overwritten.
            if current != expected or current == self.original.get(target):
                continue
            if target in self.previous:
                os.replace(self.previous[target], target)
            else:
                target.unlink(missing_ok=True)


@contextmanager
def variant_output_transaction(destination: Path, project_id: str | None = None):
    """Restore the complete previous derivative if any renderer step raises.

    Caption publication and its retained clean file share this transaction with
    the framing manifest. Only paths owned by this selected derivative are
    backed up; caller-provided masters and other variants are never touched.
    """
    destination = Path(destination)
    work = p_work(project_id) if project_id else destination.parent / ".tiktok-variant-work"
    locks = work / "variant-locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock = locks / (hashlib.sha256(str(destination.resolve()).encode()).hexdigest() + ".lock")
    targets = [destination, *[destination.with_suffix(suffix) for suffix in
               (".media.json", ".framing.json", ".captions.json", ".srt", ".ass")]]
    if project_id:
        targets.append(work / f"uncaptioned_{destination.name}")
    backup = work / "full-variants" / ("rollback-" + uuid.uuid4().hex[:12])
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        backup.mkdir(parents=True)
        previous, original = {}, {}
        for index, target in enumerate(targets):
            if target.is_symlink():
                raise ValueError("Variant output and sidecars must be regular files")
            if target.exists():
                saved = backup / str(index)
                # Sidecars can be rewritten in place by external tooling. A
                # hardlink would mutate the backup along with the live file.
                shutil.copy2(target, saved)
                previous[target] = saved
            original[target] = _optional_identity(target)
            if target in previous and _identity(previous[target])["sha256"] != original[target]["sha256"]:
                raise RuntimeError("The previous variant changed while its rollback copy was prepared")
        transaction = _VariantTransaction(previous, original)
        try:
            yield transaction
        except BaseException:
            transaction.rollback()
            raise
        finally:
            # Cleanup of this transaction's temporary links cannot turn a
            # successful publication into a failure after live files changed.
            shutil.rmtree(backup, ignore_errors=True)


def carry_framing_manifest(framed: Path, destination: Path, parent: Path, *, before_publish=None) -> None:
    """Bind the clean framing report to a successfully captioned derivative."""
    report = _read(Path(framed).with_suffix(".framing.json"))
    if not report:
        raise RuntimeError("The portrait framing manifest is missing")
    report.update(renderSourceFilename=report.get("sourceFilename"), sourceFilename=Path(parent).name,
                  sourceIdentity=_identity(Path(parent)), outputFilename=Path(destination).name,
                  cleanOutputSha256=report.get("outputSha256"), outputSha256=_identity(Path(destination))["sha256"])
    staged = Path(framed).with_suffix(".final-framing.json")
    staged.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    if before_publish:
        before_publish({Path(destination).with_suffix(".framing.json"): _identity(staged)})
    os.replace(staged, Path(destination).with_suffix(".framing.json"))


def framing_caption_windows(framed: Path) -> list[tuple[float, float, float]] | None:
    """Fence captions to the exact body clock of this newly framed file.

    Missing legacy section metadata cannot justify muting an unknown region.
    New declared portrait cards, however, are excluded even when the original
    recorded master predates spoken-caption content-window manifests.
    """
    path = Path(framed).with_suffix(".framing.json")
    report = _read(path)
    if not report:
        raise RuntimeError("The framed video has no caption-safe framing history")
    if report.get("outputSha256") != _identity(Path(framed))["sha256"]:
        raise RuntimeError("The caption framing history belongs to a different video version")
    sections = report.get("sections")
    if not isinstance(sections, list) or not sections:
        return None
    fps = float(report.get("fps") or FPS)
    if not math.isfinite(fps) or fps <= 0:
        raise RuntimeError("The caption framing clock is invalid")
    windows: list[tuple[float, float, float]] = []
    previous = 0
    for section in sections:
        try:
            first, last = section["outputStartFrame"], section["outputEndFrame"]
            left, right = float(section["sourceStart"]), float(section["sourceEnd"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError("The caption framing timeline is incomplete") from exc
        if (not isinstance(first, int) or not isinstance(last, int) or isinstance(first, bool) or isinstance(last, bool)
                or first != previous or last <= first or not all(math.isfinite(value) for value in (left, right))
                or left < 0 or right <= left or abs((right - left) - (last - first) / fps) > 0.5 / fps):
            raise RuntimeError("The caption framing timeline is invalid")
        previous = last
        if section.get("kind") in {"vertical-opening", "vertical-transition"}:
            continue
        output = first / fps
        if windows and abs(windows[-1][1] - left) < 1e-6 and abs(windows[-1][2] + windows[-1][1] - windows[-1][0] - output) < 1e-6:
            windows[-1] = (windows[-1][0], right, windows[-1][2])
        else:
            windows.append((left, right, output))
    if report.get("frameCount") is not None and previous != report["frameCount"]:
        raise RuntimeError("The caption framing timeline does not cover the rendered file")
    report["captionBodyWindows"] = [list(window) for window in windows]
    staged = path.with_name("." + path.name + ".caption-windows.tmp")
    staged.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    os.replace(staged, path)
    return windows


def retire_variant_captions(destination: Path, transaction: _VariantTransaction) -> None:
    if not Path(destination).with_suffix(".captions.json").is_file():
        return
    from app.caption_variants import retire_caption_sidecars
    targets = [Path(destination).with_suffix(suffix) for suffix in (".captions.json", ".srt", ".ass")]
    transaction.assert_unchanged(targets)
    archive = retire_caption_sidecars(destination)
    if archive:
        transaction.expect({path: None for path in targets if (archive / path.name).is_file()})


def _read(path: Path) -> dict:
    try:
        result = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _probe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def _duration(path: Path) -> float:
    value = float((_probe(path).get("format") or {}).get("duration") or 0)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("The selected video has no usable duration")
    return value


def _identity(path: Path) -> dict:
    stat = path.stat()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Variant inputs must be regular media files")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"size": stat.st_size, "mtimeNs": str(stat.st_mtime_ns), "sha256": digest}


def _run(command: list[str], log: Callable | None = None) -> None:
    if log:
        log(f"Full portrait: {Path(command[0]).name}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Portrait render failed: {result.stderr[-2500:]}")


def _card_matches(master: Path, proxy: Path, start: float, length: float) -> bool:
    try:
        if abs(_duration(proxy) - length) > 0.08:
            return False
        return all(_similar(_sample(master, start + length * fraction), _sample(proxy, length * fraction))
                   for fraction in (0.27, 0.73))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _known_card(master: Path, piece: dict, mapping: list[dict], state: dict, work: Path) -> dict | None:
    """Recognize only gaps whose retained card pixels occur in this master."""
    start, end = piece["sourceStart"], piece["sourceEnd"]
    options = state.get("renderOptions") or {}
    candidates: list[tuple[str, Path]] = []
    if start < 0.025 and options.get("introEnabled", state.get("useIntro", False)):
        candidates.extend(("opening", work / name) for name in ("hyperframes_intro.mp4", "intro.mp4"))
    previous = next((item for item in reversed(mapping) if abs(item["sourceEnd"] - start) < 0.025), None)
    incoming = next((item for item in mapping if abs(item["sourceStart"] - end) < 0.025), None)
    if previous and incoming and incoming.get("sceneIndex") == previous.get("sceneIndex", -2) + 1:
        index = int(previous["sceneIndex"])
        candidates.extend(("transition", work / name) for name in (
            f"hyperframes_footage_transition_{index:02d}.mp4", f"hyperframes_scene_transition_{index:02d}.mp4",
            f"hyperframes_transition_{index:02d}.mp4"))
    for kind, proxy in candidates:
        if proxy.is_file() and not proxy.is_symlink() and _card_matches(master, proxy, start, end - start):
            return {"kind": kind, "proxyPath": proxy, "incoming": incoming}
    return None


def _copy(state: dict, document: dict, card: dict) -> dict:
    binding = state.get("hyperframes") or document.get("hyperframes") or {}
    title = str(state.get("title") or (document.get("info") or {}).get("name") or "Property tour").strip()
    lines = [str(value).strip() for value in state.get("introLines", []) if str(value).strip()]
    if not lines:
        lines = [value.strip() for value in str(state.get("introTitle") or (state.get("renderOptions") or {}).get("introTitle") or title).splitlines() if value.strip()]
    if card["kind"] == "opening":
        headline = lines[0] if lines else title
        supporting = " · ".join(lines[1:])
    else:
        incoming = card.get("incoming") or {}
        previous_index = int(incoming.get("sceneIndex", 1)) - 1
        transition_titles = binding.get("transitionTitles") or {}
        headline = str(transition_titles.get(str(previous_index)) or incoming.get("sceneTitle") or title)
        supporting = title
    # The existing vertical composition has generous but finite copy fields.
    # Refuse to drop listing facts silently when an exceptional card exceeds it.
    if len(headline) > 150 or len(supporting) > 160:
        raise ValueError("The saved lead card has more text than this portrait card supports; shorten its lines before rendering")
    contact = {"brokerage": "LeCrown Properties", "broker": "Jie Huang", "phone": "832-798-3991", "email": "",
               **(state.get("tiktokHighlightContact") or {})}
    if binding.get("leadCardRight1"):
        contact["right1"] = binding["leadCardRight1"]
    if binding.get("leadCardRight2"):
        contact["right2"] = binding["leadCardRight2"]
    return {"headline": headline, "supporting": supporting, "contact": contact}


def _visual_path(piece: dict, source: Path) -> tuple[Path, float, float, str]:
    if piece["kind"] == "master":
        return source, piece["sourceStart"], 1.0, "master-fallback"
    if piece["kind"] == "image":
        return Path(piece["assetPath"]), 0.0, 0.0, "verified-native-image"
    # The clean prepared clip retains the approved HLG/HDR conversion and avoids
    # the master's already burned-in landscape logo. It is visually bound to
    # both this master and the native camera recording by native_source_map.
    offset = (piece["sourceStart"] - piece["mappingStart"]) * piece.get("proxyTimeScale", 1.0)
    return Path(piece["proxyPath"]), offset, piece.get("proxyTimeScale", 1.0), "verified-prepared-proxy"


def _background(piece: dict, source: Path, target: Path, run: Callable, log=None) -> Path:
    path, offset, rate, _ = _visual_path(piece, source)
    if piece["kind"] == "image":
        return path
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0, offset + min(0.15, (piece['sourceEnd'] - piece['sourceStart']) * rate / 2)):.6f}",
         "-i", str(path), "-frames:v", "1", "-q:v", "2", str(target)], log)
    return target


def _contact_logo(project_id: str | None, state: dict, input_dir: Path | None) -> Path | None:
    if not project_id or input_dir is None or not is_lecrown_project(project_id, state):
        return None
    options = state.get("renderOptions") or {}
    if options.get("logoEnabled", True) is not True:
        return None
    from app.renderer import _resolve_logo_image
    return _resolve_logo_image(input_dir, options.get("logoImage") or state.get("logoChoice"), None)


def render_full_variant(
    source: Path, destination: Path, *, project_id: str | None = None, framing_mode: str = "fill",
    language: str | None = None, log=None, preview_window: tuple[float, float] | None = None,
    work_root: Path | None = None, run_command: Callable | None = None,
    before_publish: Callable | None = None,
) -> Path:
    """Render and atomically publish the full portrait MP4 and framing manifest.

    ``preview_window`` and ``work_root`` support short isolated QA proofs; the
    product route leaves them unset and always covers the complete master.
    """
    from app.vertical_framing import analyze_fill_focus
    from app.tiktok_highlight_graphics import render_highlight_graphics
    from app.video_captions import _publish_bundle

    source, destination = Path(source), Path(destination)
    if framing_mode not in {"fill", "fit"}:
        raise ValueError("Choose Fill portrait or Fit full picture")
    if not source.is_file() or source.is_symlink() or destination.resolve() == source.resolve():
        raise ValueError("Choose a regular finished video and a separate variant filename")
    if destination.is_symlink() or destination.with_suffix(".framing.json").is_symlink():
        raise ValueError("Variant outputs must be regular project files")
    run = run_command or _run
    snapshots: dict[Path, dict] = {source: _identity(source)}
    added_media: list[dict] = []
    output_baseline = {path: _optional_identity(path) for path in (destination, destination.with_suffix(".framing.json"))}
    metadata = _probe(source)
    total = float((metadata.get("format") or {}).get("duration") or 0)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("The selected video has no usable duration")
    start, end = preview_window or (0.0, total)
    if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start or end > total + 0.001:
        raise ValueError("The preview window must remain inside the selected master")
    frames = max(1, round((end - start) * FPS))
    state = read_project_state(project_id) or {} if project_id else {}
    resolved_language = str(language or (state.get("renderOptions") or {}).get("ttsLanguage") or state.get("language") or "en")
    input_dir = p_input(project_id) if project_id else None
    document = _read(input_dir / "scenes.json") if input_dir else {}
    project_work = p_work(project_id) if project_id else destination.parent / ".tiktok-variant-work"
    root = Path(work_root) if work_root is not None else project_work / "full-variants"
    work = root / uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=False)
    mapping = native_source_map(project_id, source, document, state, probe_duration=_duration,
                                windows=[(start, end)]) if project_id else []
    mapping = [{**item, "mappingStart": item["sourceStart"]} for item in mapping]
    for item in mapping:
        for path in (Path(item["assetPath"]), Path(item["proxyPath"])):
            snapshots.setdefault(path, _identity(path))
    logo = _contact_logo(project_id, state, input_dir)
    if logo:
        snapshots.setdefault(logo, _identity(logo))
        from app.renderer import _record_media_asset
        _record_media_asset(added_media, input_dir, logo, "portrait-contact-logo", stock_artwork=True)
    sections, clips = [], []
    cursor_frame = 0

    def add_visual(piece: dict, sub_start: float, sub_end: float, crop: dict | None) -> None:
        nonlocal cursor_frame
        first = cursor_frame
        last = max(first, min(frames, round((sub_end - start) * FPS)))
        if last <= first:
            return
        output_length = (last - first) / FPS
        absolute_start = start + first / FPS
        path, base, rate, origin = _visual_path(piece, source)
        offset = max(0.0, base + (absolute_start - piece["sourceStart"]) * rate)
        focus = analyze_fill_focus(path, offset, output_length * rate, crop=crop) if framing_mode == "fill" and rate else [(0.0, 0.5)]
        focus = [(float(second) / rate if rate else float(second), float(x)) for second, x in focus]
        graph = vertical_filter("timed", "framed", crop=crop, mode=framing_mode, focus_points=focus)
        prefix = f"[0:v]setpts=(PTS-STARTPTS)/{rate:.12f}[timed];" if rate else "[0:v]setpts=PTS-STARTPTS[timed];"
        graph = prefix + graph
        command = ["ffmpeg", "-v", "error", "-y"]
        if piece["kind"] == "image":
            command += ["-loop", "1", "-framerate", str(FPS), "-i", str(path)]
        else:
            command += ["-ss", f"{offset:.6f}", "-i", str(path)]
        contact = logo if logo and piece["kind"] != "master" else None
        if contact:
            command += ["-i", str(contact)]
            graph += ";[1:v]scale=700:-2[contact];[framed][contact]overlay=x=100:y=184:eof_action=repeat[marked]"
            label = "marked"
        else:
            label = "framed"
        graph += f";[{label}]tpad=stop_mode=clone:stop_duration=0.1[complete]"
        clip = work / f"visual_{len(clips):04d}.mp4"
        command += ["-filter_complex", graph, "-map", "[complete]", "-an", "-frames:v", str(last - first),
                    "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(clip)]
        run(command, log)
        clips.append(clip)
        sections.append({"sourceStart": absolute_start, "sourceEnd": start + last / FPS,
                         "outputStartFrame": first, "outputEndFrame": last, "kind": origin,
                         "visualFilename": path.name, "nativeFilename": Path(piece["assetPath"]).name if piece.get("assetPath") else None,
                         "visualStart": offset, "visualTimeScale": rate, "crop": crop, "focusPoints": focus,
                         "contactFilename": contact.name if contact else None, "filterGraph": graph,
                         "stages": [origin, "verified-padding-removal" if crop else "picture-bounds-preserved", framing_mode + "-portrait",
                                    *( ["portrait-contact-overlay"] if contact else [])]})
        cursor_frame = last

    try:
        for piece in split_window(mapping, start, end):
            card = _known_card(source, piece, mapping, state, project_work) if project_id and piece["kind"] == "master" else None
            if card:
                last = min(frames, round((piece["sourceEnd"] - start) * FPS))
                if last <= cursor_frame:
                    continue
                snapshots.setdefault(card["proxyPath"], _identity(card["proxyPath"]))
                from app.tiktok_highlight_graphics import _brand_logo, _hyperframes_root
                graphic_logo = _brand_logo(_hyperframes_root())
                if graphic_logo not in snapshots:
                    snapshots[graphic_logo] = _identity(graphic_logo)
                    from app.renderer import _record_media_asset
                    _record_media_asset(added_media, input_dir, graphic_logo, "portrait-card-logo", stock_artwork=True)
                background_piece = card.get("incoming") or next((item for item in mapping if item["sourceStart"] >= piece["sourceEnd"] - 0.025), None)
                if background_piece is None:
                    add_visual(piece, piece["sourceStart"], piece["sourceEnd"], None)
                    continue
                copy = _copy(state, document, card)
                background = _background(background_piece, source, work / f"card_{len(clips):04d}.jpg", run, log)
                card_length = (last - cursor_frame) / FPS
                graphic = render_highlight_graphics(work, title=copy["supporting"], hook=copy["headline"], contact=copy["contact"],
                    language=resolved_language,
                    kind="full-opening" if card["kind"] == "opening" else "transition", duration=card_length,
                    background_image=background, eyebrow="房产导览" if resolved_language.lower().startswith(("zh", "cmn")) else "PROPERTY TOUR", log=log)
                clip = work / f"visual_{len(clips):04d}.mp4"
                run(["ffmpeg", "-v", "error", "-y", "-i", str(graphic), "-vf", "fps=30,scale=1080:1920,setsar=1,format=yuv420p",
                     "-an", "-frames:v", str(last - cursor_frame), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(clip)], log)
                clips.append(clip)
                sections.append({"sourceStart": start + cursor_frame / FPS, "sourceEnd": start + last / FPS,
                    "outputStartFrame": cursor_frame, "outputEndFrame": last, "kind": "vertical-" + card["kind"],
                    "copy": copy, "backgroundFilename": background.name,
                    "graphicsDirectory": str(graphic.with_suffix("").relative_to(work)), "stages": ["verified-original-card", "vertical-hyperframes"]})
                cursor_frame = last
                continue
            path, offset, rate, _ = _visual_path(piece, source)
            if piece["kind"] == "image":
                add_visual(piece, piece["sourceStart"], piece["sourceEnd"], None)
            else:
                for group in analyze_framing(path, offset, (piece["sourceEnd"] - piece["sourceStart"]) * rate):
                    left = piece["sourceStart"] + group["start"] / rate
                    right = min(piece["sourceEnd"], left + group["duration"] / rate)
                    add_visual(piece, left, right, group.get("crop"))
        if not clips or cursor_frame != frames:
            raise RuntimeError("The portrait framing plan did not cover the complete selected video")
        playlist = work / "visuals.txt"
        playlist.write_text("\n".join("file '" + str(path).replace("'", "'\\''") + "'" for path in clips), "utf-8")
        staged = work / destination.name
        audio = next((stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"), None)
        audio_mode = "aac-stream-copy" if audio and audio.get("codec_name") == "aac" else "aac-encode" if audio else "none"
        command = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist)]
        if start:
            command += ["-ss", f"{start:.6f}"]
        command += ["-i", str(source), "-map", "0:v:0", "-map", "1:a?", "-t", f"{end - start:.6f}", "-c:v", "copy",
                    "-c:a", "copy" if audio_mode == "aac-stream-copy" else "aac", "-movflags", "+faststart", str(staged)]
        run(command, log)
        result = _probe(staged)
        video = next((stream for stream in result.get("streams", []) if stream.get("codec_type") == "video"), {})
        if (video.get("width"), video.get("height")) != (1080, 1920) or abs(float((result.get("format") or {}).get("duration") or 0) - (end - start)) > 0.09:
            raise RuntimeError("The portrait output failed dimensions or duration validation")
        if audio and not any(stream.get("codec_type") == "audio" for stream in result.get("streams", [])):
            raise RuntimeError("The portrait output lost the original audio")
        for path, snapshot in snapshots.items():
            if _identity(path) != snapshot:
                raise RuntimeError("A source, prepared proxy, or brand asset changed during portrait rendering")
        manifest = {"version": 1, "sourceFilename": source.name, "sourceIdentity": snapshots[source], "outputFilename": destination.name,
                    "outputSha256": _identity(staged)["sha256"], "layout": framing_mode, "width": 1080, "height": 1920,
                    "fps": FPS, "frameCount": frames, "sourceWindow": [start, end], "audioMode": audio_mode,
                    "workToken": work.name,
                    "nativeVisualsVerified": bool(mapping), "sections": sections,
                    "addedMedia": added_media,
                    "inputs": [{"filename": path.name, **value} for path, value in snapshots.items()]}
        staged_manifest = work / (destination.stem + ".framing.json")
        staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if any(_optional_identity(path) != identity for path, identity in output_baseline.items()):
            raise RuntimeError("The previous variant changed during rendering; the newer output was preserved")
        if before_publish:
            before_publish({destination: _identity(staged), destination.with_suffix(".framing.json"): _identity(staged_manifest)})
        _publish_bundle([(staged_manifest, destination.with_suffix(".framing.json")), (staged, destination)], work)
        return destination
    except Exception:
        (work / destination.name).unlink(missing_ok=True)
        raise
