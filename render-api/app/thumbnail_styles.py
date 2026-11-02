"""Project thumbnail styles shared by regeneration and completed renders."""
from __future__ import annotations

import os
import subprocess
import unicodedata
from pathlib import Path


STYLES = {"black-agent-card", "first-frame"}


def _run(command: list[str], log=None) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Thumbnail generation failed: {exc.stderr.strip()[-1200:]}") from exc
    if log:
        log("Thumbnail image generated")
    return result.stdout.strip()


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")


def _font() -> Path:
    # A lang constraint prevents fontconfig from silently returning a Latin-only
    # replacement when the requested Noto family is absent on a workstation.
    for family in ("Noto Sans CJK SC:lang=zh", "sans-serif:lang=zh", "DejaVu Sans", "Arial"):
        try:
            candidate = Path(_run(["fc-match", "--format=%{file}", family]))
            if candidate.is_file():
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    for candidate in (Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("A sans-serif font is required to create the thumbnail")


def _wrap(text: str, width: float, limit: int) -> list[str]:
    """Bound lines by displayed character width, including Chinese glyphs."""
    lines: list[str] = []
    line = ""
    length = 0.0
    for char in text.strip():
        size = 1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.57
        if (char == "\n" or length + size > width) and line:
            lines.append(line.strip())
            line, length = "", 0.0
        if char != "\n":
            line += char
            length += size
    if line.strip():
        lines.append(line.strip())
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = lines[-1].rstrip(" .,|·")[:-1] + "…"
    return lines


def _text_filter(work_dir: Path, name: str, text: str, font: Path, size: int, x: int, y: int, color: str = "white") -> str:
    content = work_dir / f"thumbnail-{name}.txt"
    content.write_text(text, encoding="utf-8")
    return (f"drawtext=fontfile='{_filter_path(font)}':textfile='{_filter_path(content)}':"
            f"fontsize={size}:fontcolor={color}:x={x}:y={y}:expansion=none")


def _portrait(input_dir: Path, state: dict) -> tuple[Path, bool] | None:
    requested = str(state.get("leaderImageName") or "")
    if requested:
        candidate = input_dir / "leader" / Path(requested).name
        if candidate.is_file():
            return candidate, False
    base = Path(__file__).resolve()
    candidates = [parent / "media" / "leader.png" for parent in base.parents]
    if os.environ.get("LEADER_TEMPLATE_PATH"):
        candidates.insert(0, Path(os.environ["LEADER_TEMPLATE_PATH"]))
    for candidate in candidates:
        if candidate.is_file():
            return candidate, True
    return None


def _first_source(input_dir: Path, output_dir: Path, scenes_doc: dict, state: dict) -> Path:
    output_name = Path(str(state.get("outputName") or "video.mp4")).name
    rendered = output_dir / output_name
    if rendered.is_file() and rendered.suffix.lower() in {".mov", ".mp4", ".m4v", ".webm"}:
        return rendered
    footage = scenes_doc.get("sourceFootage") or state.get("sourceFootage") or []
    if footage:
        item = footage[0]
        candidate = input_dir / "footage" / Path(str(item.get("filename") or item.get("name") or "")).name
        if candidate.is_file():
            return candidate
    for scene in scenes_doc.get("scenes") or []:
        images = scene.get("images") or []
        if images:
            candidate = input_dir / "images" / Path(str(images[0])).name
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("Add a source video or scene image before creating a first-frame thumbnail")


def generate_project_thumbnail(input_dir: Path, output_dir: Path, work_dir: Path, scenes_doc: dict, state: dict, style: str = "first-frame", log=None) -> Path:
    """Create the chosen 1280x720 JPEG without publishing or modifying project state."""
    style = str(style or "first-frame").strip().lower()
    if style not in STYLES:
        raise ValueError("Thumbnail style must be black-agent-card or first-frame")
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "thumbnail.jpg"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if style == "first-frame":
        source = _first_source(input_dir, output_dir, scenes_doc, state)
        command += ["-i", str(source), "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x090f1c,setsar=1"]
    else:
        font = _font()
        title = str(state.get("title") or state.get("youtubeTitle") or (scenes_doc.get("info") or {}).get("name") or "MediaStudio video").strip()
        parts = [part.strip() for part in title.replace("\n", "|").split("|") if part.strip()]
        main = "\n".join(_wrap(parts[0] if parts else title, 17.0, 3))
        detail = "\n".join(_wrap(" · ".join(parts[1:]), 23.0, 2))
        is_bible = state.get("workflow") == "bible-video"
        portrait = None if is_bible else _portrait(input_dir, state)
        text_x = 450 if portrait else 125
        command += ["-f", "lavfi", "-i", "color=c=0x090f1c:s=1280x720:d=0.1"]
        filters: list[str] = []
        label = "SCRIPTURE VIDEO" if is_bible else "REAL ESTATE VIDEO"
        filters.append(_text_filter(work_dir, "label", label, font, 17, text_x, 180, "0xd6ae61"))
        filters.append(_text_filter(work_dir, "title", main, font, 43, text_x, 236))
        main_lines = main.count("\n") + 1
        detail_y = 236 + main_lines * 56 + 14
        if detail:
            filters.append(_text_filter(work_dir, "detail", detail, font, 29, text_x, detail_y))
        rule_y = max(387, detail_y + (detail.count("\n") + 1) * 40 + 20)
        filters.append(f"drawbox=x={text_x}:y={rule_y}:w=570:h=3:color=0xd6ae61:t=fill")
        if not is_bible:
            binding = state.get("hyperframes") or {}
            right1 = str(binding.get("leadCardRight1") or "Brokerage: LeCrown Properties")
            right2 = str(binding.get("leadCardRight2") or "Broker: Jie Huang")
            # The brokerage remains one point larger than the broker credit.
            filters.append(_text_filter(work_dir, "brokerage", "\n".join(_wrap(right1, 29, 2)), font, 24, text_x, rule_y + 32))
            filters.append(_text_filter(work_dir, "broker", "\n".join(_wrap(right2, 30, 2)), font, 23, text_x, rule_y + 92))
            filters.append(_text_filter(work_dir, "phone", "832-798-3991", font, 20, 1040, 55, "0xd6ae61"))
        chain = ",".join(filters)
        if portrait:
            source, bundled = portrait
            command += ["-i", str(source)]
            crop = "crop=360:360:395:35," if bundled else "crop=min(iw\\,ih):min(iw\\,ih),"
            mask = "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow(W/2-2,2)),255,0)'"
            graph = f"[1:v]{crop}scale=246:246,format=rgba,{mask}[portrait];[0:v][portrait]overlay=105:245,{chain}[thumb]"
        else:
            graph = f"[0:v]{chain}[thumb]"
        command += ["-filter_complex", graph, "-map", "[thumb]"]
    command += ["-frames:v", "1", "-q:v", "3", "-threads", "1", str(target)]
    _run(command, log)
    if not target.is_file() or not target.stat().st_size:
        raise RuntimeError("Thumbnail generation did not produce an image")
    if target.stat().st_size > 2_000_000:
        raise RuntimeError("Generated thumbnail exceeds YouTube's 2 MB limit")
    return target
