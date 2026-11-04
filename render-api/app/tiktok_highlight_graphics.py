"""Editable, image-backed vertical HyperFrames cards for TikTok highlights.

Each call stages an isolated composition next to its rendered clip. This avoids
sharing mutable HyperFrames assets between worker jobs and retains the HTML for
later edits without touching the landscape composition project.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable


_KIND_STYLES = {
    "hook": "#contacts, #outro-action { display: none; }",
    "transition": "#contacts, #outro-action { display: none; } #headline { top: 510px; } #headline-text { font-size: 76px; } #supporting { top: 1020px; }",
    "outro": "#eyebrow, #accent, #supporting { display: none; } #headline { top: 440px; } #headline-text { font-size: 58px; line-height: 1.15; }",
}


def _hyperframes_root() -> Path:
    configured = Path(os.getenv("HYPERFRAMES_PROJECT_DIR", "/srv/hyperframes"))
    if configured.is_dir():
        return configured
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "hyperframes"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("The packaged HyperFrames composition project is missing")


def _brand_logo(root: Path) -> Path:
    candidates = [root.parent / "media" / "brand" / "logo3.png"]
    candidates.extend(parent / "media" / "brand" / "logo3.png" for parent in Path(__file__).resolve().parents)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("The LeCrown Properties logo is missing from the media package")


def _copy_text(value: object, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _contact_copy(contact: dict) -> dict[str, str]:
    brokerage = _copy_text(contact.get("brokerage") or "LeCrown Properties", 60)
    broker = _copy_text(contact.get("broker") or contact.get("name") or "Jie Huang", 60)
    emails = contact.get("emails") or contact.get("email") or ""
    if isinstance(emails, (list, tuple)):
        emails = "\n".join(_copy_text(value, 100) for value in emails[:2] if _copy_text(value, 100))
    else:
        emails = str(emails).strip()[:205]
    return {
        "brand": brokerage,
        "brokerage": _copy_text(contact.get("right1") or f"Brokerage: {brokerage}", 90),
        "broker": _copy_text(contact.get("right2") or f"Broker: {broker}", 90),
        "phone": _copy_text(contact.get("phone") or "832-798-3991", 40),
        "email": emails,
    }


def _run(command: list[str], log: Callable[[str], None] | None = None) -> None:
    environment = os.environ.copy()
    if Path(command[0]).name == "hyperframes" and not environment.get("HYPERFRAMES_BROWSER_PATH"):
        browser = environment.get("PUPPETEER_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("google-chrome")
        if browser and Path(browser).is_file():
            # HyperFrames uses its own browser selector; the image already ships
            # Chromium, so rendering must not try to download another browser.
            environment["HYPERFRAMES_BROWSER_PATH"] = browser
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, env=environment)
    if log and result.stdout:
        log(result.stdout[-3500:])
    if result.returncode:
        raise RuntimeError(f"Highlight graphics render failed: {(result.stderr or result.stdout)[-2500:]}")


def prepare_highlight_graphics(
    output_dir: Path,
    *,
    title: str,
    hook: str,
    contact: dict,
    language: str = "en",
    kind: str = "hook",
    duration: float = 2.0,
    background_image: Path | None = None,
) -> Path:
    """Create a self-contained, editable HyperFrames project; do not render it."""
    if kind not in {"hook", "transition", "outro"}:
        raise ValueError("Highlight graphics kind must be hook, transition, or outro")
    if not math.isfinite(float(duration)) or not 0.1 <= float(duration) <= 10:
        raise ValueError("Highlight graphics duration must be between 0.1 and 10 seconds")
    root = _hyperframes_root()
    template = root / "tiktok-highlight" / "index.html"
    card_template = root / "tiktok-highlight" / "compositions" / "card.html"
    gsap = root / "tiktok-highlight" / "assets" / "gsap.min.js"
    if not template.is_file() or not card_template.is_file() or not gsap.is_file():
        raise RuntimeError("The vertical TikTok HyperFrames template is not packaged")
    logo = _brand_logo(root)
    background = Path(background_image) if background_image is not None else logo
    if not background.is_file():
        raise FileNotFoundError(f"Highlight background image not found: {background.name}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    project = output_dir / f"hyperframes-{kind}-{uuid.uuid4().hex[:10]}"
    assets = project / "assets"
    assets.mkdir(parents=True)
    shutil.copy2(gsap, assets / "gsap.min.js")
    shutil.copy2(logo, assets / "logo.png")
    # Chromium decodes the source bytes irrespective of the stable local name.
    shutil.copy2(background, assets / "background.jpg")
    chinese = language.lower().startswith(("zh", "cmn"))
    variables = {
        **_contact_copy(dict(contact or {})),
        "eyebrow": "房产亮点" if chinese else "PROPERTY HIGHLIGHT",
        "headline": _copy_text(title if kind == "outro" else hook or title, 150),
        "supporting": _copy_text(title if hook else "", 160),
        "action": "预约私人看房" if chinese else "Arrange a private tour",
    }
    declared = [{"id": key, "type": "string", "label": key.replace("_", " ").title(), "default": value} for key, value in variables.items()]
    replacements = {
        "__LANGUAGE__": "zh-CN" if chinese else "en",
        "__VARIABLES__": html.escape(json.dumps(declared, ensure_ascii=False), quote=True),
        "__VARIABLE_VALUES__": html.escape(json.dumps(variables, ensure_ascii=False), quote=True),
        "__DURATION__": f"{float(duration):.6f}",
        "__KIND__": kind,
        "__KIND_STYLES__": _KIND_STYLES[kind],
        **{f"__{key.upper()}__": html.escape(value, quote=True) for key, value in variables.items()},
    }
    composition = re.sub(r"__[A-Z_]+__", lambda match: replacements[match.group()], template.read_text(encoding="utf-8"))
    (project / "index.html").write_text(composition, encoding="utf-8")
    (project / "compositions").mkdir()
    card = re.sub(r"__[A-Z_]+__", lambda match: replacements[match.group()], card_template.read_text(encoding="utf-8"))
    (project / "compositions" / "card.html").write_text(card, encoding="utf-8")
    (project / "variables.json").write_text(json.dumps(variables, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "provenance.json").write_text(json.dumps({
        "renderer": "hyperframes", "kind": kind, "duration": float(duration),
        "width": 1080, "height": 1920, "backgroundFilename": background.name,
    }, indent=2), encoding="utf-8")
    return project


def render_highlight_graphics(
    output_dir: Path,
    *,
    title: str,
    hook: str,
    contact: dict,
    language: str = "en",
    kind: str = "hook",
    duration: float = 2.0,
    background_image: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Render a 1080×1920 HyperFrames card with a silent stereo AAC track."""
    project = prepare_highlight_graphics(
        output_dir, title=title, hook=hook, contact=contact, language=language,
        kind=kind, duration=duration, background_image=background_image,
    )
    binary = _hyperframes_root() / "node_modules" / ".bin" / "hyperframes"
    if not binary.is_file():
        raise RuntimeError("The pinned HyperFrames renderer is not installed")
    silent_video = project / "graphics.silent.mp4"
    destination = project.with_suffix(".mp4")
    if log:
        log(f"Rendering vertical HyperFrames {kind} ({duration:g}s)")
    _run([
        str(binary), "render", str(project), "--output", str(silent_video),
        "--fps", "30", "--quality", "standard", "--workers", "1",
        "--low-memory-mode", "--no-browser-gpu", "--resolution", "portrait",
    ], log)
    if not silent_video.is_file() or not silent_video.stat().st_size:
        raise RuntimeError("HyperFrames did not create the vertical highlight clip")
    _run([
        "ffmpeg", "-v", "error", "-y", "-i", str(silent_video),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", "fps=30,scale=1080:1920,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", str(duration),
        "-movflags", "+faststart", str(destination),
    ], log)
    if not destination.is_file() or not destination.stat().st_size:
        raise RuntimeError("The vertical HyperFrames clip is empty")
    return destination
