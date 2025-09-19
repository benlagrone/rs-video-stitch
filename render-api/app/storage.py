"""Helpers for working with the shared storage volume."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, List

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


def proj_root(pid: str) -> Path:
    return ROOT / "projects" / pid


def p_input(pid: str) -> Path:
    return proj_root(pid) / "input"


def p_work(pid: str) -> Path:
    return proj_root(pid) / "work"


def p_output(pid: str) -> Path:
    return proj_root(pid) / "output"


def logs_dir() -> Path:
    return ROOT / "logs"


def ensure_dirs(pid: str) -> None:
    for directory in (proj_root(pid), p_input(pid), p_work(pid), p_output(pid), logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def save_scenes(pid: str, content: str) -> Path:
    ensure_dirs(pid)
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
