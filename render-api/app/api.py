"""FastAPI entrypoint for the render API."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import uuid
import time
import csv
import html
import re
from pathlib import Path
from typing import Iterator, Optional

import requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.models import Job, Project
from app.schemas import (
    ProjectSpec,
    ProjectStateRequest,
    BibleVideoRequest,
    SceneAnimationRequest,
    SceneAnimationBatchRequest,
    SceneAnimationPromptResponse,
    BibleTitleCardRequest,
    LeadCardGenerateRequest,
    LeadCardGenerateResponse,
    RenderRequest,
    RoomAnnotationRequest,
    ScriptEnhanceRequest,
    ScriptEnhanceResponse,
    YouTubeAuthCompleteRequest,
    YouTubeDescriptionRequest,
    YouTubeDescriptionResponse,
    YouTubeThumbnailRequest,
    YouTubeThumbnailResponse,
    YouTubeUploadRequest,
    YouTubeUploadResponse,
)
from app.storage import (
    ensure_dirs,
    job_log_path,
    list_asset_files,
    list_outputs,
    list_project_versions,
    list_projects,
    p_input,
    p_output,
    project_asset_path,
    read_project_version,
    read_project_state,
    save_project_state,
    save_project_version,
    save_scenes,
)
from app.worker import loop as worker_loop
from app.youtube_upload import (
    YouTubeUploadConfigurationError,
    complete_youtube_auth,
    complete_youtube_auth_from_callback_url,
    set_youtube_thumbnail,
    upload_video_to_youtube,
    youtube_auth_status,
    youtube_authorization_url,
)
from app.bible_workflow import (
    BIBLE_CAPTION_STYLE,
    BIBLE_CHANNEL_ICON,
    GOD_CHARACTER_DESIGN,
    capability_health,
    generate_scene_animation_prompt,
    scene_animation_context,
)
from app.art_styles import list_art_styles
from app.sfx_catalog import SfxCatalogError, list_sfx_catalog

ALLOW_ORIGINS = (
    os.getenv("ALLOW_ORIGINS", "").split(",")
    if os.getenv("ALLOW_ORIGINS")
    else ["*"]
)

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("app.request")

INLINE_WORKER = os.getenv("INLINE_WORKER", "1")
INLINE_WORKER_ENABLED = INLINE_WORKER.lower() not in {"0", "false", "off", "no"}
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://fortress.lan:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mixtral:latest")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
OLLAMA_CONNECT_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_CONNECT_TIMEOUT_SECONDS", "10"))
VOICE_GATEWAY_URL = os.getenv("VOICE_GATEWAY_URL", "http://100.100.97.30:8133")
ROOM_RENAMER_API_URL = os.getenv("ROOM_RENAMER_API_URL", "http://host.docker.internal:8000")
ROOM_RENAMER_TIMEOUT_SECONDS = float(os.getenv("ROOM_RENAMER_TIMEOUT_SECONDS", "45"))
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "]+",
    flags=re.UNICODE,
)
_worker_thread: threading.Thread | None = None
_worker_stop: threading.Event | None = None

app = FastAPI(title="Render API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

UI_DIST = Path(os.getenv("RENDER_UI_DIST", "/srv/render-ui"))
BRAND_MEDIA_DIR = Path(os.getenv("BRAND_MEDIA_DIR", "/srv/media/brand"))
if UI_DIST.exists():
    app.mount(
        "/media-studio/assets",
        StaticFiles(directory=str(UI_DIST / "assets")),
        name="media-studio-assets",
    )
if BRAND_MEDIA_DIR.exists():
    app.mount(
        "/media-studio/brand-assets",
        StaticFiles(directory=str(BRAND_MEDIA_DIR)),
        name="media-studio-brand-assets",
    )


@app.get("/media-studio")
@app.get("/media-studio/")
async def media_studio_ui() -> FileResponse:
    index_path = UI_DIST / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="render UI is not bundled")
    return FileResponse(index_path)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)

    start = time.perf_counter()
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        duration_ms = (time.perf_counter() - start) * 1000.0
        request_logger.exception(
            "%s %s %s %0.2fms %s",
            request.method,
            request.url.path,
            "500",
            duration_ms,
            request.client.host if request.client else "-",
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000.0
    client = request.client.host if request.client else "-"
    request_logger.info(
        "%s %s %s %0.2fms %s",
        request.method,
        request.url.path,
        status_code,
        duration_ms,
        client,
    )
    return response

def _start_inline_worker() -> None:
    global _worker_thread, _worker_stop

    if not INLINE_WORKER_ENABLED:
        return

    if _worker_thread and _worker_thread.is_alive():
        return

    _worker_stop = threading.Event()

    def _runner() -> None:
        try:
            logger.info("Inline worker thread starting")
            worker_loop(stop_event=_worker_stop)
        except Exception:  # noqa: BLE001
            logger.exception("Inline worker thread crashed")
        finally:
            logger.info("Inline worker thread exiting")

    _worker_thread = threading.Thread(target=_runner, name="inline-worker", daemon=True)
    _worker_thread.start()


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _start_inline_worker()


@app.on_event("shutdown")
def _shutdown() -> None:
    global _worker_thread, _worker_stop

    if _worker_stop is not None:
        _worker_stop.set()

    if _worker_thread is not None:
        _worker_thread.join(timeout=5)
        if _worker_thread.is_alive():
            logger.warning("Inline worker thread did not stop cleanly")

    _worker_thread = None
    _worker_stop = None


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> dict:
    return {"ok": True}


@app.get("/v1/voice-options")
async def voice_options() -> dict:
    try:
        response = requests.get(f"{VOICE_GATEWAY_URL.rstrip('/')}/control/api/voice", timeout=10)
        response.raise_for_status()
        snapshot = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Fortress Voice Gateway catalog request failed: {exc}") from exc

    providers = []
    for backend in snapshot.get("backends") or []:
        provider_id = str(backend.get("name") or "").strip()
        voices = [str(voice).strip() for voice in backend.get("voices") or [] if str(voice).strip()]
        if not provider_id or not voices:
            continue
        is_vibevoice = provider_id == "vibevoice"
        is_azure_voice = provider_id == "azure_voice"
        if is_vibevoice and "Carter" not in voices:
            voices.insert(0, "Carter")
        providers.append(
            {
                "id": provider_id,
                "label": "VibeVoice · Fortress GPU" if is_vibevoice else "Azure Speech · Fortress proxy",
                "ttsApi": "vibevoice-proxy" if is_vibevoice else "azure-proxy",
                "selectable": (is_vibevoice or is_azure_voice) and backend.get("ok") is not False,
                "voices": voices,
                "detail": str(backend.get("detail") or ""),
            }
        )

    providers.extend(
        [
            {
                "id": "flite",
                "label": "Built-in fallback voice",
                "ttsApi": "flite",
                "selectable": True,
                "voices": ["kal", "awb", "rms", "slt"],
                "detail": "Local CPU fallback",
            },
            {
                "id": "none",
                "label": "None / timed silence",
                "ttsApi": "none",
                "selectable": True,
                "voices": ["Timed silence"],
                "detail": "No narration synthesis",
            },
        ]
    )
    return {"providers": providers, "gateway": "fortress-lan:voice-gateway"}


@app.get("/v1/sfx/catalog")
async def sfx_catalog() -> dict:
    try:
        return list_sfx_catalog()
    except SfxCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/v1/bible/videos", status_code=202)
async def create_bible_video(req: BibleVideoRequest, db: Session = Depends(get_db)) -> dict:
    project_id = f"bible-{re.sub(r'[^a-z0-9]+', '-', req.passage.lower()).strip('-')[:48]}-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=project_id,
        last_output_name=req.outputName,
        voice=req.voice,
        language=req.language,
    )
    payload = req.model_dump(mode="json", by_alias=True)
    payload["workflow"] = "bible-video"
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    db.add(project)
    db.add(Job(id=job_id, project_id=project_id, status="QUEUED", payload=payload, progress=0.0, stage="QUEUED"))
    db.commit()
    return {"projectId": project_id, "jobId": job_id, "status": "QUEUED"}


@app.post(
    "/v1/projects/{pid}/scenes/{scene_index}/animation-prompt",
    response_model=SceneAnimationPromptResponse,
)
async def scene_animation_prompt(pid: str, scene_index: int) -> SceneAnimationPromptResponse:
    try:
        prompt = generate_scene_animation_prompt(pid, scene_index)
    except (FileNotFoundError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Motion prompt generation failed: {exc}") from exc
    return SceneAnimationPromptResponse(projectId=pid, sceneIndex=scene_index, prompt=prompt)


@app.post("/v1/projects/{pid}/scenes/{scene_index}/animate", status_code=202)
async def animate_scene(
    pid: str,
    scene_index: int,
    req: SceneAnimationRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        scene_animation_context(pid, scene_index)
    except (FileNotFoundError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
        db.add(project)
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    db.add(
        Job(
            id=job_id,
            project_id=pid,
            status="QUEUED",
            payload={
                "workflow": "scene-animation",
                "sceneIndex": scene_index,
                "prompt": req.prompt.strip(),
            },
            progress=0.0,
            stage="QUEUED",
        )
    )
    db.commit()
    return {"projectId": pid, "sceneIndex": scene_index, "jobId": job_id, "status": "QUEUED"}


@app.post("/v1/projects/{pid}/scenes/animate-all", status_code=202)
async def animate_all_scenes(
    pid: str,
    req: SceneAnimationBatchRequest,
    db: Session = Depends(get_db),
) -> dict:
    scenes_path = p_input(pid) / "scenes.json"
    if not scenes_path.exists():
        raise HTTPException(status_code=404, detail=f"Project {pid} has no scenes.json")
    try:
        document = json.loads(scenes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Project {pid} has invalid scene data") from exc

    scenes = document.get("scenes") or []
    if not scenes:
        raise HTTPException(status_code=404, detail=f"Project {pid} has no scenes")

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
        db.add(project)

    jobs = []
    skipped = []
    for scene_index, scene in enumerate(scenes, start=1):
        try:
            scene_animation_context(pid, scene_index)
        except (FileNotFoundError, IndexError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        has_motion = bool(((scene.get("timeline") or [{}])[0]).get("video"))
        if has_motion and not req.includeAnimated:
            skipped.append(scene_index)
            continue
        job_id = f"j_{uuid.uuid4().hex[:12]}"
        db.add(
            Job(
                id=job_id,
                project_id=pid,
                status="QUEUED",
                payload={
                    "workflow": "scene-animation",
                    "sceneIndex": scene_index,
                    "prompt": str(req.prompts.get(scene_index, "")).strip(),
                },
                progress=0.0,
                stage="QUEUED",
            )
        )
        jobs.append({"sceneIndex": scene_index, "jobId": job_id, "status": "QUEUED"})

    db.commit()
    return {
        "projectId": pid,
        "status": "QUEUED" if jobs else "NOTHING_TO_QUEUE",
        "queuedCount": len(jobs),
        "skippedSceneIndexes": skipped,
        "jobs": jobs,
    }


@app.post("/v1/projects/{pid}/bible-title-card", status_code=202)
async def regenerate_bible_title_card(
    pid: str,
    req: BibleTitleCardRequest,
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, pid)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not (p_input(pid) / "scenes.json").exists():
        raise HTTPException(status_code=404, detail="project has no scenes")
    state = read_project_state(pid) or {}
    render_options = dict(state.get("renderOptions") or {})
    render_options.update({
        "introEnabled": True,
        "introTitle": state.get("passage") or state.get("title") or pid,
        "introBackgroundImage": "bible-title-card.png",
        "introLeaderEnabled": False,
        "thumbnailEnabled": True,
        "logoEnabled": True,
        "logoImage": BIBLE_CHANNEL_ICON,
        "logoCorner": "bottom-right",
        "logoMargin": 28,
        "scriptureCaptionEnabled": True,
        "titleStyle": dict(render_options.get("titleStyle") or BIBLE_CAPTION_STYLE),
    })
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    db.add(Job(
        id=job_id,
        project_id=pid,
        status="QUEUED",
        payload={
            "workflow": "bible-title-card",
            "visualStyle": req.visualStyle,
            "regenerateImage": req.regenerateImage,
            "renderVideo": req.renderVideo,
            "renderOptions": render_options,
            "outputName": state.get("outputName") or project.last_output_name or "video.mp4",
        },
        progress=0.0,
        stage="QUEUED",
    ))
    db.commit()
    return {"projectId": pid, "jobId": job_id, "status": "QUEUED"}


@app.get("/v1/bible/health")
async def bible_health() -> dict:
    return capability_health()


@app.get("/v1/bible/styles")
async def bible_styles() -> dict:
    styles = list_art_styles()
    return {"styles": styles, "count": len(styles)}


@app.get("/v1/bible/character-policy")
async def bible_character_policy() -> dict:
    return {"god": GOD_CHARACTER_DESIGN}


@app.get("/v1/bible/fonts")
async def bible_fonts() -> dict:
    fonts = [
        {"id": name, "name": name, "family": family}
        for family, names in (
            ("EB Garamond", ("EB Garamond", "EB Garamond Medium", "EB Garamond SemiBold", "EB Garamond Bold", "EB Garamond Italic")),
            ("Cinzel", ("Cinzel", "Cinzel Medium", "Cinzel SemiBold", "Cinzel Bold", "Cinzel ExtraBold", "Cinzel Black")),
        )
        for name in names
    ]
    return {"fonts": fonts, "count": len(fonts)}


@app.get("/v1/brand-assets")
async def brand_assets() -> dict:
    files = []
    if BRAND_MEDIA_DIR.exists():
        for item in sorted(BRAND_MEDIA_DIR.iterdir()):
            if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                files.append(
                    {
                        "name": item.name,
                        "url": f"/media-studio/brand-assets/{item.name}",
                    }
                )
    return {"files": files}


@app.get("/v1/youtube/auth/status")
async def youtube_status(profile: str = "english") -> dict:
    try:
        return youtube_auth_status(profile)
    except YouTubeUploadConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/youtube/auth/start")
async def youtube_auth_start(profile: str = "english") -> dict:
    try:
        return youtube_authorization_url(profile)
    except YouTubeUploadConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/youtube/auth/complete")
async def youtube_auth_complete(req: YouTubeAuthCompleteRequest, profile: str = "english") -> dict:
    try:
        completed_profile = complete_youtube_auth_from_callback_url(req.callbackUrl, profile=profile)
    except YouTubeUploadConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return youtube_auth_status(completed_profile)


@app.get("/v1/youtube/auth/callback")
async def youtube_auth_callback(request: Request, code: str = "", state: str = "") -> HTMLResponse:
    host = (request.url.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return HTMLResponse(_youtube_manual_callback_page(str(request.url)))

    try:
        completed_profile = complete_youtube_auth(code, state)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            "<h1>YouTube authorization failed</h1>"
            f"<p>{html.escape(str(exc))}</p>"
            "<p>You can close this tab and return to MediaStudio.</p>",
            status_code=400,
        )
    return HTMLResponse(
        f"<h1>{html.escape(completed_profile.title())} YouTube authorization complete</h1>"
        "<p>You can close this tab and return to MediaStudio.</p>"
    )


def _youtube_manual_callback_page(callback_url: str) -> str:
    escaped_url = html.escape(callback_url, quote=True)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow">
    <title>YouTube authorization callback</title>
    <style>
      :root {{
        color-scheme: dark;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #0f141b;
        color: #eef4f8;
      }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 32px;
      }}
      main {{
        width: min(760px, 100%);
        border: 1px solid #2a3948;
        border-radius: 12px;
        background: #151b23;
        padding: 28px;
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 28px;
      }}
      p {{
        color: #aebdca;
        line-height: 1.5;
      }}
      textarea {{
        box-sizing: border-box;
        width: 100%;
        min-height: 150px;
        margin: 12px 0;
        padding: 14px;
        border-radius: 10px;
        border: 1px solid #3a5264;
        background: #0b1016;
        color: #eef4f8;
        font: 14px ui-monospace, SFMono-Regular, Menlo, monospace;
      }}
      button {{
        border: 1px solid #65aeba;
        border-radius: 9px;
        background: #1d3a42;
        color: #d9fbff;
        padding: 11px 16px;
        font-weight: 700;
        cursor: pointer;
      }}
      .status {{
        display: inline-block;
        margin-left: 10px;
        color: #86d4dd;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Copy this YouTube callback URL</h1>
      <p>Return to MediaStudio on Fortress Sextant, paste this full URL into the YouTube auth box, then click Save YouTube auth.</p>
      <textarea id="callback-url" readonly>{escaped_url}</textarea>
      <button type="button" onclick="copyUrl()">Copy URL</button>
      <span id="copy-status" class="status"></span>
    </main>
    <script>
      const textArea = document.getElementById('callback-url');
      textArea.focus();
      textArea.select();
      async function copyUrl() {{
        textArea.select();
        try {{
          await navigator.clipboard.writeText(textArea.value);
          document.getElementById('copy-status').textContent = 'Copied';
        }} catch (error) {{
          document.getElementById('copy-status').textContent = 'Select the text and copy it manually';
        }}
      }}
    </script>
  </body>
</html>"""


def _room_context(room_info: list[dict]) -> str:
    lines = []
    for item in room_info:
        filename = str(item.get("filename") or "").strip()
        header = str(item.get("header") or "").strip()
        room_description = str(item.get("roomDescription") or item.get("description") or "").strip()
        label = str(item.get("label") or "").strip()
        parts = []
        if header:
            parts.append(f"header: {header}")
        if label:
            parts.append(f"room label: {label}")
        if room_description:
            parts.append(f"room notes: {room_description}")
        if filename and parts:
            lines.append(f"- {filename}: " + "; ".join(parts))
    return "\n".join(lines)


def _script_enhance_prompt(script: str, target_seconds: int, room_info: Optional[list[dict]] = None) -> str:
    target_words = max(1, round(target_seconds * 2.45))
    room_context = _room_context(room_info or [])
    room_context_block = (
        "\nUse these image and room notes to make the narration specific without listing filenames:\n"
        f"{room_context}\n"
        if room_context
        else ""
    )
    return (
        "Rewrite the narration script for a real-estate or listing-style video.\n"
        f"Target spoken length: {target_seconds} seconds, about {target_words} words.\n"
        "Keep the result suitable for text-to-speech narration.\n"
        "Preserve scene breaks with blank lines when the input has multiple scenes.\n"
        "When room notes are provided, weave the useful details naturally into the script.\n"
        "Do not include headings, markdown, notes, timing labels, or explanations.\n"
        "Return only the rewritten script.\n\n"
        f"{room_context_block}"
        f"Input script:\n{script.strip()}"
    )


def _extract_ollama_response(payload: dict) -> str:
    response = str(payload.get("response") or "").strip()
    if not response:
        message = payload.get("message")
        if isinstance(message, dict):
            response = str(message.get("content") or "").strip()
    return response


def _ollama_generate(
    prompt: str,
    *,
    temperature: float = 0.55,
    num_predict: Optional[int] = None,
) -> str:
    url = OLLAMA_BASE_URL.rstrip("/") + "/api/generate"
    options = {
        "temperature": temperature,
    }
    if num_predict is not None:
        options["num_predict"] = num_predict

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=(OLLAMA_CONNECT_TIMEOUT_SECONDS, OLLAMA_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama request failed for {OLLAMA_MODEL} at {OLLAMA_BASE_URL}: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Ollama returned invalid JSON") from exc

    result = _extract_ollama_response(data)
    if not result:
        raise HTTPException(status_code=502, detail="Ollama returned an empty response")
    return result


def _lead_card_prompt(req: LeadCardGenerateRequest) -> str:
    current_lines = [str(line).strip() for line in req.currentLines if str(line).strip()]
    current_block = f"\nCurrent draft lines:\n{json.dumps(current_lines)}\n" if current_lines else ""
    return (
        "Create a concise three-line leader card for this video.\n"
        "Line 1 is the primary title, maximum 42 characters.\n"
        "Line 2 is a descriptive subtitle, maximum 60 characters.\n"
        "Line 3 is useful context such as translation, location, or a factual callout, maximum 60 characters.\n"
        "Use only facts supported by the supplied title and script. Do not invent names, claims, prices, or theology.\n"
        "For scripture, preserve the passage reference and translation when available.\n"
        "Return JSON only in exactly this shape: {\"lines\":[\"line 1\",\"line 2\",\"line 3\"]}.\n"
        f"\nVideo title:\n{req.title.strip()}\n"
        f"{current_block}"
        f"Source script:\n{req.script.strip()[:6000]}"
    )


def _parse_lead_card_lines(value: str) -> list[str]:
    raw = value.strip()
    json_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
            lines = payload.get("lines") if isinstance(payload, dict) else None
            if isinstance(lines, list):
                cleaned = [str(line).strip()[:60].rstrip() for line in lines if str(line).strip()]
                if len(cleaned) >= 3:
                    return cleaned[:3]
        except (TypeError, ValueError):
            pass

    cleaned = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)]|line\s*\d+\s*:)\s*", "", line, flags=re.IGNORECASE).strip()
        if line and not line.startswith("```"):
            cleaned.append(line[:60].rstrip())
    if len(cleaned) < 3:
        raise HTTPException(status_code=502, detail="Ollama did not return three usable lead-card lines")
    return cleaned[:3]


@app.post("/v1/lead-card/generate", response_model=LeadCardGenerateResponse)
async def generate_lead_card(req: LeadCardGenerateRequest) -> LeadCardGenerateResponse:
    if not f"{req.title} {req.script}".strip():
        raise HTTPException(status_code=400, detail="Add a project title or script before generating a lead card")
    result = _ollama_generate(_lead_card_prompt(req), temperature=0.35)
    return LeadCardGenerateResponse(lines=_parse_lead_card_lines(result), model=OLLAMA_MODEL)


@app.post("/v1/script/enhance", response_model=ScriptEnhanceResponse)
async def enhance_script(req: ScriptEnhanceRequest) -> ScriptEnhanceResponse:
    prompt = _script_enhance_prompt(req.script, req.targetSeconds, req.roomInfo)
    enhanced = _ollama_generate(prompt, temperature=0.55)

    return ScriptEnhanceResponse(
        script=enhanced,
        targetSeconds=req.targetSeconds,
        model=OLLAMA_MODEL,
    )


def _youtube_description_prompt(req: YouTubeDescriptionRequest) -> str:
    room_context = _room_context(req.roomInfo or [])
    room_context_block = (
        "\nUseful image and room notes:\n"
        f"{room_context}\n"
        if room_context
        else ""
    )
    current_block = (
        "\nCurrent draft description:\n"
        f"{req.currentDescription.strip()}\n"
        if req.currentDescription.strip()
        else ""
    )
    return (
        "Write a polished YouTube video description for a real-estate or commercial property listing video.\n"
        "Use a professional, broker-friendly tone. Make it useful for viewers and searchable on YouTube.\n"
        "Write in the same primary language as the source title and narration. If the source is Chinese, respond in Simplified Chinese.\n"
        "Include a concise opening summary, notable property details, and a clear call to contact the listing broker or schedule a showing.\n"
        "Do not invent prices, phone numbers, URLs, MLS IDs, or broker names.\n"
        "Do not use markdown headings, hashtags, emoji, or bullet lists unless the source text explicitly requires them.\n"
        "Keep it between 120 and 220 words.\n"
        "Return only the description text.\n\n"
        f"Video title:\n{req.title.strip() or 'Listing video'}\n"
        f"{current_block}"
        f"{room_context_block}"
        f"Source narration or property description:\n{req.script.strip()}"
    )


def _youtube_description_is_complete(description: str, source_text: str) -> bool:
    description = description.strip()
    if re.search(r"[\u3400-\u9fff]", source_text):
        return len(description) >= 180
    return len(description.split()) >= 80


def _generate_youtube_description(req: YouTubeDescriptionRequest) -> str:
    prompt = _youtube_description_prompt(req)
    source_text = f"{req.title} {req.script} {req.currentDescription}".strip()
    description = EMOJI_PATTERN.sub(
        "",
        _ollama_generate(prompt, temperature=0.45, num_predict=512),
    ).strip()
    if _youtube_description_is_complete(description, source_text):
        return description

    retry_prompt = (
        f"{prompt}\n\n"
        "The previous response was incomplete. Write the complete description now, "
        "following every instruction above and ending with the call to action."
    )
    description = EMOJI_PATTERN.sub(
        "",
        _ollama_generate(retry_prompt, temperature=0.35, num_predict=512),
    ).strip()
    if not _youtube_description_is_complete(description, source_text):
        raise HTTPException(
            status_code=502,
            detail="AI returned an incomplete YouTube description after retrying",
        )
    return description


@app.post("/v1/youtube/description/enhance", response_model=YouTubeDescriptionResponse)
async def enhance_youtube_description(req: YouTubeDescriptionRequest) -> YouTubeDescriptionResponse:
    source_text = f"{req.script} {req.currentDescription}".strip()
    if not source_text:
        raise HTTPException(status_code=400, detail="Add script or description text before generating a YouTube description")
    description = _generate_youtube_description(req)
    return YouTubeDescriptionResponse(description=description, model=OLLAMA_MODEL)


def _guess_room_label(filename: str) -> str:
    normalized = filename.lower().replace("-", "_").replace(" ", "_")
    candidates = [
        "kitchen",
        "living_room",
        "dining_room",
        "breakfast_room",
        "primary_bedroom",
        "master_bedroom",
        "bedroom",
        "primary_bathroom",
        "master_bathroom",
        "bathroom",
        "office",
        "laundry_room",
        "garage",
        "pool",
        "back_yard",
        "front_yard",
        "aerial_view",
        "exterior",
    ]
    for label in candidates:
        if label in normalized or label.replace("_", "") in normalized:
            return label
    if "bath" in normalized:
        return "bathroom"
    if "bed" in normalized:
        return "bedroom"
    if "yard" in normalized:
        return "yard"
    return ""


def _annotations_csv_path(pid: str) -> Path:
    return p_input(pid) / "room_annotations.csv"


def _write_room_annotations(pid: str, req: RoomAnnotationRequest) -> dict:
    ensure_dirs(pid)
    csv_path = _annotations_csv_path(pid)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["filename", "header", "roomDescription", "label", "confidence", "source"],
        )
        writer.writeheader()
        for annotation in req.annotations:
            writer.writerow(annotation.model_dump(mode="json"))
    return {"projectId": pid, "count": len(req.annotations), "path": str(csv_path)}


@app.post("/v1/projects/{pid}/room-annotations")
async def save_room_annotations(pid: str, req: RoomAnnotationRequest) -> dict:
    return _write_room_annotations(pid, req)


@app.get("/v1/projects")
async def projects() -> dict:
    return {"projects": list_projects()}


def _asset_url(pid: str, subdir: str, filename: str) -> str:
    return (
        f"/v1/projects/{pid}/assets/{subdir}/{filename}"
    )


def _state_from_scenes(pid: str, scenes: Optional[dict]) -> dict:
    if not scenes:
        return {}

    scene_items = scenes.get("scenes") or []
    image_names = []
    image_headers = {}
    image_room_info = {}
    script_parts = []

    for scene in scene_items:
        voiceover = str(scene.get("VO") or scene.get("description") or "").strip()
        if voiceover:
            script_parts.append(voiceover)

        scene_title = str(scene.get("title") or "").strip()
        for image_name in scene.get("images") or []:
            if image_name not in image_names:
                image_names.append(image_name)
            if scene_title and not image_headers.get(image_name):
                image_headers[image_name] = scene_title

        for entry in scene.get("timeline") or []:
            image_name = entry.get("image")
            if not image_name:
                continue
            if image_name not in image_names:
                image_names.append(image_name)
            header = str(entry.get("header") or "").strip()
            if header:
                image_headers[image_name] = header
            image_room_info[image_name] = {
                "roomDescription": str(entry.get("roomDescription") or "").strip(),
                "label": str(entry.get("roomLabel") or "").strip(),
                "confidence": entry.get("confidence"),
                "source": "scenes-json",
            }

    outputs = list_outputs(pid)
    output_name = next(
        (name for name in reversed(outputs) if name.lower().endswith((".mp4", ".mov", ".mpg", ".mpeg"))),
        "video.mp4",
    )
    video = scenes.get("vid") or scenes.get("video") or {}
    info = scenes.get("info") or {}
    intro_title = str(info.get("name") or pid)

    return {
        "schemaVersion": 1,
        "title": info.get("name") or pid,
        "projectId": pid,
        "script": "\n\n".join(script_parts),
        "targetSeconds": 60,
        "outputName": output_name,
        "images": [{"name": image_name} for image_name in image_names],
        "imageHeaders": image_headers,
        "imageRoomInfo": image_room_info,
        "useIntro": True,
        "introTitle": intro_title,
        "introLines": [intro_title, "", ""],
        "leaderImageName": None,
        "useLogo": True,
        "logoChoice": "logophone.png",
        "logoImageName": None,
        "logoCorner": "top-right",
        "logoMargin": 24,
        "voice": video.get("voice") or "",
        "language": video.get("lang") or video.get("language") or "en-US",
        "ttsApi": video.get("api") or video.get("ttsApi") or "voice-gateway",
        "youtubeTitle": info.get("name") or "",
        "youtubeDescription": "",
        "youtubeTags": "",
        "youtubePrivacy": "private",
        "stateSource": "scenes.json",
        "removedImages": [],
    }


def _load_scenes(pid: str) -> Optional[dict]:
    scenes_path = p_input(pid) / "scenes.json"
    if not scenes_path.exists():
        return None
    try:
        return json.loads(scenes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@app.get("/v1/projects/{pid}")
async def get_project(pid: str) -> dict:
    ensure_dirs(pid)
    state = read_project_state(pid) or {}
    assets = {
        subdir: [
            {
                "name": filename,
                "url": _asset_url(pid, subdir, filename),
            }
            for filename in list_asset_files(pid, subdir)
        ]
        for subdir in ("images", "motion", "leader", "logo", "voiceovers")
    }
    scenes = _load_scenes(pid)
    if not state:
        state = _state_from_scenes(pid, scenes)

    return {
        "projectId": pid,
        "state": state,
        "assets": assets,
        "scenes": scenes,
        "outputs": list_outputs(pid),
        "versions": list_project_versions(pid),
    }


@app.get("/v1/projects/{pid}/versions")
async def get_project_versions(pid: str) -> dict:
    active = read_project_state(pid) or _state_from_scenes(pid, _load_scenes(pid))
    return {
        "projectId": pid,
        "active": {
            "label": "active",
            "updatedAt": active.get("updatedAt"),
            "title": active.get("title"),
            "imageCount": len(active.get("images") or []),
            "removedImageCount": len(active.get("removedImages") or []),
            "outputName": active.get("outputName"),
        } if active else None,
        "versions": list_project_versions(pid),
    }


@app.put("/v1/projects/{pid}/state")
async def put_project_state(
    pid: str,
    req: ProjectStateRequest,
) -> dict:
    state = dict(req.state or {})
    previous = read_project_state(pid) or _state_from_scenes(pid, _load_scenes(pid))
    if previous:
        save_project_version(pid, previous, label="before-update")
    state["projectId"] = pid
    state["updatedAt"] = time.time()
    project_name = str(state.get("title") or pid)
    save_project_state(pid, state, project_name=project_name)
    save_project_version(pid, state, label="update")
    return {"projectId": pid, "ok": True, "state": state}


@app.post("/v1/projects/{pid}/versions/{version_id}/restore")
async def restore_project_version(pid: str, version_id: str) -> dict:
    payload = read_project_version(pid, version_id)
    if not payload or not isinstance(payload.get("state"), dict):
        raise HTTPException(status_code=404, detail="version not found")

    previous = read_project_state(pid) or _state_from_scenes(pid, _load_scenes(pid))
    if previous:
        save_project_version(pid, previous, label="before-restore")

    state = dict(payload["state"])
    state["projectId"] = pid
    state["restoredFromVersion"] = version_id
    state["updatedAt"] = time.time()
    save_project_state(pid, state, project_name=str(state.get("title") or pid))
    save_project_version(pid, state, label="restore")
    return {"projectId": pid, "ok": True, "state": state}


@app.get("/v1/projects/{pid}/assets/{subdir}/{filename}")
async def get_project_asset(pid: str, subdir: str, filename: str) -> FileResponse:
    allowed = {"images", "motion", "leader", "logo", "voiceovers"}
    if subdir not in allowed:
        raise HTTPException(status_code=400, detail="Invalid subdir")

    target = project_asset_path(pid, subdir, filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="asset not found")

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


@app.post("/v1/projects/{pid}/room-classify")
async def classify_rooms(
    pid: str,
    files: list[UploadFile] = File(...),
    threshold: Optional[float] = Form(None),
) -> dict:
    results = []
    service_url = ROOM_RENAMER_API_URL.rstrip("/") + "/classify"
    for upload in files:
        data = await upload.read()
        if not upload.filename:
            continue
        label = ""
        confidence = None
        source = "filename-fallback"
        error = None
        if data:
            try:
                request_kwargs = {
                    "files": {"file": (upload.filename, data, upload.content_type or "application/octet-stream")},
                    "timeout": ROOM_RENAMER_TIMEOUT_SECONDS,
                }
                if threshold is not None:
                    request_kwargs["params"] = {"threshold": threshold}
                response = requests.post(service_url, **request_kwargs)
                response.raise_for_status()
                payload = response.json()
                label = str(payload.get("label") or "")
                confidence_value = payload.get("confidence")
                confidence = float(confidence_value) if confidence_value is not None else None
                source = "room-renamer"
            except Exception as exc:  # noqa: BLE001
                label = _guess_room_label(upload.filename)
                confidence = 0.0 if label else None
                error = str(exc)
        results.append(
            {
                "filename": upload.filename,
                "label": label,
                "confidence": confidence,
                "source": source,
                **({"error": error} if error else {}),
            }
        )

    _write_room_annotations(
        pid,
        RoomAnnotationRequest(
            annotations=[
                {
                    "filename": item["filename"],
                    "label": item.get("label") or "",
                    "confidence": item.get("confidence"),
                    "source": item.get("source") or "room-renamer",
                }
                for item in results
            ]
        ),
    )
    return {"projectId": pid, "results": results}


@app.put("/v1/projects/{pid}/scenes")
async def upsert_scenes(
    pid: str,
    spec: ProjectSpec,
    db: Session = Depends(get_db),
) -> dict:
    project_name = None
    if spec.info and isinstance(spec.info, dict):
        name_value = spec.info.get("name")
        if name_value is not None:
            project_name = str(name_value)

    payload = json.dumps(spec.model_dump(mode="json", by_alias=True), indent=2)
    save_scenes(pid, payload, project_name=project_name)

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
    if spec.video:
        project.voice = spec.video.voice
        project.language = spec.video.language

    db.add(project)
    db.commit()

    return {"projectId": pid, "ok": True}


@app.post("/v1/projects/{pid}/assets")
async def upload_assets(
    pid: str,
    files: list[UploadFile] = File(...),
    subdir: str = Form("images"),
) -> dict:
    allowed = {"images", "leader", "logo", "voiceovers"}
    if subdir not in allowed:
        raise HTTPException(status_code=400, detail="Invalid subdir")

    ensure_dirs(pid)
    dest = p_input(pid) / subdir
    dest.mkdir(parents=True, exist_ok=True)

    count = 0
    for upload in files:
        data = await upload.read()
        if not upload.filename:
            continue
        target = dest / upload.filename
        target.write_bytes(data)
        count += 1

    return {"projectId": pid, "count": count, "subdir": subdir}


@app.post("/v1/projects/{pid}/render")
async def render(
    pid: str,
    req: RenderRequest,
    db: Session = Depends(get_db),
) -> dict:
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    payload = req.model_dump(mode="json", by_alias=True)

    project = db.get(Project, pid)
    if project is None:
        project = Project(id=pid)
    project.last_output_name = req.outputName

    render_opts = payload.setdefault("renderOptions", {})
    if project.voice and not render_opts.get("tts"):
        render_opts["tts"] = project.voice
    if project.language and not render_opts.get("ttsLanguage"):
        render_opts["ttsLanguage"] = project.language

    db.add(project)

    job = Job(
        id=job_id,
        project_id=pid,
        status="QUEUED",
        payload=payload,
        progress=0.0,
        stage="QUEUED",
    )
    db.add(job)
    db.commit()

    return {"jobId": job_id}


@app.post("/v1/projects/{pid}/youtube/upload", response_model=YouTubeUploadResponse)
async def youtube_upload(
    pid: str,
    req: YouTubeUploadRequest,
) -> YouTubeUploadResponse:
    target = p_output(pid) / req.filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="video not found")
    try:
        video_id = upload_video_to_youtube(
            video_path=target,
            title=req.title,
            description=req.description,
            tags=req.tags,
            category_id=req.categoryId,
            privacy_status=req.privacyStatus,
            made_for_kids=req.madeForKids,
            profile=req.profile,
        )
    except YouTubeUploadConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"YouTube upload failed: {exc}") from exc

    url = f"https://youtu.be/{video_id}"
    state = read_project_state(pid) or {}
    upload_state = {
        "videoId": video_id,
        "url": url,
        "profile": req.profile,
        "uploadedAt": time.time(),
        "thumbnailApplied": False,
        "thumbnailFilename": None,
        "thumbnailError": None,
    }
    state["youtubeUpload"] = upload_state
    state["updatedAt"] = time.time()
    save_project_state(pid, state, project_name=str(state.get("title") or pid))

    thumbnail_path = p_output(pid) / "thumbnail.jpg"
    try:
        set_youtube_thumbnail(
            video_id=video_id,
            thumbnail_path=thumbnail_path,
            profile=req.profile,
        )
        upload_state["thumbnailApplied"] = True
        upload_state["thumbnailFilename"] = thumbnail_path.name
    except Exception as exc:  # noqa: BLE001
        # The video already exists. Return it as a successful upload and expose a
        # retryable thumbnail error instead of encouraging a duplicate upload.
        upload_state["thumbnailError"] = _youtube_thumbnail_error(exc)

    state["youtubeUpload"] = upload_state
    state["updatedAt"] = time.time()
    save_project_state(pid, state, project_name=str(state.get("title") or pid))
    return YouTubeUploadResponse(
        videoId=video_id,
        url=url,
        thumbnailApplied=bool(upload_state["thumbnailApplied"]),
        thumbnailFilename=upload_state["thumbnailFilename"],
        thumbnailError=upload_state["thumbnailError"],
    )


@app.post("/v1/projects/{pid}/youtube/thumbnail", response_model=YouTubeThumbnailResponse)
async def youtube_thumbnail(
    pid: str,
    req: YouTubeThumbnailRequest,
) -> YouTubeThumbnailResponse:
    state = read_project_state(pid) or {}
    upload_state = dict(state.get("youtubeUpload") or {})
    video_id = req.videoId.strip() or str(upload_state.get("videoId") or "").strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="YouTube video id is required")

    thumbnail_path = p_output(pid) / Path(req.filename).name
    if not thumbnail_path.exists():
        raise HTTPException(status_code=404, detail="thumbnail not found")
    try:
        set_youtube_thumbnail(
            video_id=video_id,
            thumbnail_path=thumbnail_path,
            profile=req.profile,
        )
    except YouTubeUploadConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_youtube_thumbnail_error(exc)) from exc

    url = f"https://youtu.be/{video_id}"
    upload_state.update(
        {
            "videoId": video_id,
            "url": url,
            "profile": req.profile,
            "thumbnailApplied": True,
            "thumbnailFilename": thumbnail_path.name,
            "thumbnailError": None,
            "thumbnailAppliedAt": time.time(),
        }
    )
    state["youtubeUpload"] = upload_state
    state["updatedAt"] = time.time()
    save_project_state(pid, state, project_name=str(state.get("title") or pid))
    return YouTubeThumbnailResponse(
        videoId=video_id,
        url=url,
        thumbnailFilename=thumbnail_path.name,
    )


def _youtube_thumbnail_error(exc: Exception) -> str:
    message = str(exc)
    if "doesn't have permissions to upload and set custom video thumbnails" in message:
        return (
            "This YouTube channel has not enabled custom thumbnails. In YouTube Studio, "
            "open Settings > Channel > Feature eligibility, verify the channel for "
            "Intermediate features, then click Apply thumbnail again."
        )
    return f"YouTube thumbnail failed: {message}"


def _tail_logs(job_id: str, limit_bytes: int = 4096) -> str:
    path = job_log_path(job_id)
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) <= limit_bytes:
        return data.decode("utf-8", errors="ignore")
    return data[-limit_bytes:].decode("utf-8", errors="ignore")


@app.get("/v1/jobs/{job_id}")
async def job_status(
    job_id: str,
    db: Session = Depends(get_db),
) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "jobId": job.id,
        "projectId": job.project_id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "etaSeconds": None,
        "error": job.error,
        "logs": _tail_logs(job_id),
    }


@app.get("/v1/projects/{pid}/outputs")
async def outputs(pid: str) -> dict:
    return {"projectId": pid, "files": list_outputs(pid)}


def _parse_byte_range(range_header: str, file_size: int) -> Optional[tuple[int, int]]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or file_size <= 0:
        return None

    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        return None

    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    else:
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            return None
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    if start >= file_size or end < start:
        return None
    return start, min(end, file_size - 1)


def _iter_file_range(path: Path, start: int, end: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/v1/projects/{pid}/outputs/video")
async def download_video(
    pid: str,
    request: Request,
    filename: Optional[str] = None,
    db: Session = Depends(get_db),
):
    project = db.get(Project, pid)
    preferred = filename or (project.last_output_name if project else None) or "video.mp4"
    target = p_output(pid) / preferred
    if not target.exists():
        raise HTTPException(status_code=404, detail="video not found")

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    file_size = target.stat().st_size
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{preferred}"',
    }
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(target, media_type=media_type, headers=base_headers)

    byte_range = _parse_byte_range(range_header, file_size)
    if byte_range is None:
        return StreamingResponse(
            iter(()),
            status_code=416,
            headers={
                **base_headers,
                "Content-Range": f"bytes */{file_size}",
            },
            media_type=media_type,
        )

    start, end = byte_range
    content_length = end - start + 1
    return StreamingResponse(
        _iter_file_range(target, start, end),
        status_code=206,
        headers={
            **base_headers,
            "Content-Length": str(content_length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
        },
        media_type=media_type,
    )
