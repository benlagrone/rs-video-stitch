"""Helpers for working with the shared storage volume."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

def _prepare_storage_dir(path: Path) -> Path | None:
    """Ensure *path* exists and is writable, returning it on success."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return None
        probe = path / ".write-test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
    except OSError:
        return None

    return path


def resolve_storage_root() -> Path:
    """Return a writable storage root for local or container execution."""

    env_root = os.getenv("RENDER_STORAGE")
    if env_root:
        expanded = Path(env_root).expanduser().resolve()
        prepared = _prepare_storage_dir(expanded)
        if prepared is None:
            raise RuntimeError(
                f"RENDER_STORAGE path '{expanded}' is not writable. "
                "Set it to a directory the process can create and modify."
            )
        return prepared

    candidates = [
        Path.home() / "Videos",
        Path.cwd() / "videos",
        Path(__file__).resolve().parents[2] / "videos",
    ]

    for candidate in candidates:
        prepared = _prepare_storage_dir(candidate)
        if prepared is not None:
            return prepared

    raise RuntimeError(
        "Unable to determine a writable storage directory. "
        "Set the RENDER_STORAGE environment variable to a writable path."
    )

ROOT = resolve_storage_root()
PROJECTS_ROOT = ROOT / "projects"
META_SUFFIX = ".meta.json"

_slug_pattern = re.compile(r"[^a-z0-9]+")


def _project_meta_path(pid: str) -> Path:
    return PROJECTS_ROOT / f"{pid}{META_SUFFIX}"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _slugify_name(name: str) -> str:
    slug = _slug_pattern.sub("-", name.lower()).strip("-")
    return slug or "project"


def _load_directory_from_meta(pid: str) -> Optional[Path]:
    meta_path = _project_meta_path(pid)
    if not meta_path.exists():
        return None

    data = _read_json(meta_path)
    if data is None:
        return None

    directory = data.get("directory")
    if not directory:
        return None

    return PROJECTS_ROOT / directory


def _directory_reserved(directory: str) -> bool:
    candidate = PROJECTS_ROOT / directory
    if candidate.exists():
        return True

    for meta_file in PROJECTS_ROOT.glob(f"*{META_SUFFIX}"):
        data = _read_json(meta_file)
        if data is None:
            continue
        if data.get("directory") == directory:
            return True
    return False


def _create_directory_name(pid: str, project_name: Optional[str]) -> Path:
    base = project_name or pid
    slug = _slugify_name(base)

    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

    for _ in range(10000):
        suffix = secrets.randbelow(10000)
        directory = f"{slug}-{suffix:04d}"
        if not _directory_reserved(directory):
            meta_path = _project_meta_path(pid)
            meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
            meta_data = {"directory": directory, "slug": slug, "name": project_name}
            meta_tmp.write_text(json.dumps(meta_data), encoding="utf-8")
            meta_tmp.replace(meta_path)
            return PROJECTS_ROOT / directory

    raise RuntimeError("Unable to allocate unique project directory name")


def _resolve_project_root(pid: str, project_name: Optional[str] = None) -> Path:
    existing = _load_directory_from_meta(pid)
    if existing is not None:
        return existing

    legacy = PROJECTS_ROOT / pid
    if legacy.exists():
        return legacy

    return _create_directory_name(pid, project_name)


def proj_root(pid: str) -> Path:
    meta = _load_directory_from_meta(pid)
    if meta is not None:
        return meta

    legacy = PROJECTS_ROOT / pid
    if legacy.exists():
        return legacy

    return legacy


def p_input(pid: str) -> Path:
    return proj_root(pid) / "input"


def p_work(pid: str) -> Path:
    return proj_root(pid) / "work"


def p_output(pid: str) -> Path:
    return proj_root(pid) / "output"


def p_state(pid: str) -> Path:
    return p_input(pid) / "project_state.json"


def p_versions(pid: str) -> Path:
    return p_input(pid) / "versions"


def p_asset_provenance(pid: str) -> Path:
    return p_input(pid) / "asset_provenance.json"


def p_source_cards(pid: str) -> Path:
    return p_input(pid) / "source_cards.json"


def logs_dir() -> Path:
    return ROOT / "logs"


def ensure_dirs(pid: str, project_name: Optional[str] = None) -> Path:
    root = _resolve_project_root(pid, project_name=project_name)
    for directory in (root, root / "input", root / "work", root / "output", logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return root


def save_scenes(pid: str, content: str, project_name: Optional[str] = None) -> Path:
    ensure_dirs(pid, project_name=project_name)
    target = p_input(pid) / "scenes.json"
    target.write_text(content, encoding="utf-8")
    return target


def save_project_state(pid: str, state: Dict[str, Any], project_name: Optional[str] = None) -> Path:
    ensure_dirs(pid, project_name=project_name)
    target = p_state(pid)
    target.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return target


def read_project_state(pid: str) -> Optional[Dict[str, Any]]:
    return _read_json(p_state(pid))


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_asset_provenance(pid: str) -> Dict[str, Any]:
    data = _read_json(p_asset_provenance(pid))
    return data if isinstance(data, dict) else {}


def save_asset_provenance(
    pid: str,
    provenance: Dict[str, Any],
    project_name: Optional[str] = None,
) -> Path:
    ensure_dirs(pid, project_name=project_name)
    return _write_json(p_asset_provenance(pid), provenance)


def upsert_asset_provenance(
    pid: str,
    filename: str,
    selection: Dict[str, Any],
    project_name: Optional[str] = None,
) -> Dict[str, Any]:
    safe_name = Path(filename).name
    provenance = read_asset_provenance(pid)
    next_selection = dict(selection)
    next_selection["filename"] = safe_name
    provenance[safe_name] = next_selection
    save_asset_provenance(pid, provenance, project_name=project_name)
    return next_selection


def read_source_cards(pid: str) -> List[Dict[str, Any]]:
    data = _read_json(p_source_cards(pid))
    return data if isinstance(data, list) else []


def save_source_cards(
    pid: str,
    cards: List[Dict[str, Any]],
    project_name: Optional[str] = None,
) -> Path:
    ensure_dirs(pid, project_name=project_name)
    return _write_json(p_source_cards(pid), cards)


def save_project_version(pid: str, state: Dict[str, Any], label: str = "update") -> Optional[Path]:
    if not state:
        return None

    ensure_dirs(pid, project_name=str(state.get("title") or pid))
    versions_dir = p_versions(pid)
    versions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = float(state.get("updatedAt") or time.time())
    version_id = f"{int(timestamp * 1000)}-{_slugify_name(label)}"
    target = versions_dir / f"{version_id}.json"
    suffix = 1
    while target.exists():
        target = versions_dir / f"{version_id}-{suffix}.json"
        suffix += 1

    payload = {
        "versionId": target.stem,
        "label": label,
        "createdAt": time.time(),
        "state": state,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def read_project_version(pid: str, version_id: str) -> Optional[Dict[str, Any]]:
    safe_id = Path(version_id).stem
    return _read_json(p_versions(pid) / f"{safe_id}.json")


def list_project_versions(pid: str) -> List[Dict[str, Any]]:
    versions_dir = p_versions(pid)
    if not versions_dir.exists():
        return []

    versions = []
    seen_signatures = set()
    for item in sorted(versions_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        payload = _read_json(item)
        if not isinstance(payload, dict):
            continue
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        label = payload.get("label") or "version"
        if label == "restore":
            continue
        signature = json.dumps(
            {
                "title": state.get("title"),
                "script": state.get("script"),
                "outputName": state.get("outputName"),
                "images": state.get("images") or [],
                "removedImages": state.get("removedImages") or [],
                "imageHeaders": state.get("imageHeaders") or {},
                "imageRoomInfo": state.get("imageRoomInfo") or {},
            },
            sort_keys=True,
            default=str,
        )
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        versions.append(
            {
                "versionId": payload.get("versionId") or item.stem,
                "label": label,
                "createdAt": payload.get("createdAt") or item.stat().st_mtime,
                "updatedAt": state.get("updatedAt"),
                "title": state.get("title"),
                "imageCount": len(state.get("images") or []),
                "removedImageCount": len(state.get("removedImages") or []),
                "outputName": state.get("outputName"),
            }
        )

    return sorted(versions, key=lambda item: item.get("createdAt") or 0, reverse=True)


def read_project_meta(pid: str) -> Dict[str, Any]:
    meta = _read_json(_project_meta_path(pid)) or {}
    return meta if isinstance(meta, dict) else {}


def list_asset_files(pid: str, subdir: str) -> List[str]:
    asset_dir = p_input(pid) / subdir
    if not asset_dir.exists():
        return []
    return [item.name for item in sorted(asset_dir.iterdir()) if item.is_file()]


def project_asset_path(pid: str, subdir: str, filename: str) -> Path:
    safe_name = Path(filename).name
    return p_input(pid) / subdir / safe_name


def _discover_project_ids() -> List[str]:
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    ids = set()
    mapped_directories = set()
    for meta_file in PROJECTS_ROOT.glob(f"*{META_SUFFIX}"):
        project_id = meta_file.name[: -len(META_SUFFIX)]
        ids.add(project_id)
        meta = _read_json(meta_file) or {}
        directory = meta.get("directory")
        if directory:
            mapped_directories.add(directory)
    for item in PROJECTS_ROOT.iterdir():
        if item.is_dir() and item.name not in mapped_directories:
            ids.add(item.name)
    return sorted(ids)


def list_projects() -> List[Dict[str, Any]]:
    projects = []
    for pid in _discover_project_ids():
        root = proj_root(pid)
        state = read_project_state(pid) or {}
        meta = read_project_meta(pid)
        scenes_path = p_input(pid) / "scenes.json"
        outputs = list_outputs(pid)

        timestamps = [
            path.stat().st_mtime
            for path in (p_state(pid), scenes_path, root)
            if path.exists()
        ]
        updated_at = max(timestamps) if timestamps else None
        title = (
            state.get("title")
            or meta.get("name")
            or state.get("projectId")
            or pid
        )

        projects.append(
            {
                "projectId": pid,
                "title": title,
                "directory": root.name,
                "updatedAt": updated_at,
                "imageCount": len(list_asset_files(pid, "images")),
                "versionCount": len(list_project_versions(pid)),
                "outputs": outputs,
                "latestOutput": outputs[-1] if outputs else None,
                "hasState": bool(state),
                "hasScenes": scenes_path.exists(),
            }
        )

    return sorted(projects, key=lambda item: item.get("updatedAt") or 0, reverse=True)


def list_outputs(pid: str) -> List[str]:
    output_dir = p_output(pid)
    if not output_dir.exists():
        return []
    return [item.name for item in sorted(output_dir.iterdir()) if item.is_file()]


def reset_workdir(pid: str) -> None:
    work = p_work(pid)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)


def artifact_entries(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def job_log_path(job_id: str) -> Path:
    logs_dir().mkdir(parents=True, exist_ok=True)
    return logs_dir() / f"{job_id}.log"
