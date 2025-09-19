"""Helpers for working with the shared storage volume."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Iterable, List, Optional

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


def _slugify_name(name: str) -> str:
    slug = _slug_pattern.sub("-", name.lower()).strip("-")
    return slug or "project"


def _load_directory_from_meta(pid: str) -> Optional[Path]:
    meta_path = _project_meta_path(pid)
    if not meta_path.exists():
        return None

    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
