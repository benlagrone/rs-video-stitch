"""Sextant-owned admission client for shared Phronesis GPU workloads."""
from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import requests


OPTIMIZATION_MCP_URL = os.getenv("FORTRESS_OPTIMIZATION_MCP_URL", "").rstrip("/")
OPTIMIZATION_MCP_TOKEN = os.getenv("FORTRESS_OPTIMIZATION_MCP_TOKEN", "").strip()
MEDIASTUDIO_RUNTIME_HOST = os.getenv("MEDIASTUDIO_RUNTIME_HOST", "").strip().lower()
ADMISSION_REQUIRED = os.getenv(
    "FORTRESS_GPU_ADMISSION_REQUIRED",
    "1" if MEDIASTUDIO_RUNTIME_HOST == "fortress.sextant" else "0",
).lower() not in {"0", "false", "off", "no"}
ADMISSION_WAIT_SECONDS = float(os.getenv("FORTRESS_GPU_ADMISSION_WAIT_SECONDS", "900"))
ADMISSION_HEARTBEAT_SECONDS = float(os.getenv("FORTRESS_GPU_ADMISSION_HEARTBEAT_SECONDS", "30"))


class GpuAdmissionError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPTIMIZATION_MCP_TOKEN}"}


def _configured() -> bool:
    return bool(OPTIMIZATION_MCP_URL and OPTIMIZATION_MCP_TOKEN)


def _post(path: str, payload: dict, *, timeout: float = 15):
    return requests.post(
        f"{OPTIMIZATION_MCP_URL}{path}",
        headers=_headers(),
        json=payload,
        timeout=timeout,
    )


def _acquire(
    workload_class: str,
    *,
    workload_id: str,
    vram_required_mb: int,
    duration_slots: int,
    priority: int,
) -> str:
    deadline = time.monotonic() + ADMISSION_WAIT_SECONDS
    payload = {
        "owner": "fortress.sextant:mediastudio",
        "workload_id": workload_id,
        "workload_class": workload_class,
        "duration_slots": duration_slots,
        "vram_required_mb": vram_required_mb,
        "priority": priority,
    }
    while True:
        try:
            response = _post("/internal/v1/phronesis/admission/acquire", payload)
        except requests.RequestException as exc:
            raise GpuAdmissionError(f"Sextant GPU admission is unavailable: {exc}") from exc
        if response.status_code == 200:
            lease_id = str(response.json().get("leaseId") or "")
            if not lease_id:
                raise GpuAdmissionError("Sextant admitted the workload without a lease ID")
            return lease_id
        if response.status_code != 409 or time.monotonic() >= deadline:
            detail = response.text[:500]
            raise GpuAdmissionError(f"Sextant rejected the Phronesis GPU workload ({response.status_code}): {detail}")
        try:
            retry_after = float(response.json().get("retryAfterSeconds") or 2)
        except (TypeError, ValueError, requests.JSONDecodeError):
            retry_after = 2
        time.sleep(max(0.25, min(30, retry_after)))


def _heartbeat(lease_id: str, stop: threading.Event) -> None:
    while not stop.wait(max(5, ADMISSION_HEARTBEAT_SECONDS)):
        try:
            response = _post(
                "/internal/v1/phronesis/admission/heartbeat",
                {"lease_id": lease_id},
            )
            if response.status_code != 200:
                return
        except requests.RequestException:
            return


@contextmanager
def admit_gpu(
    workload_class: str,
    *,
    workload_id: str | None = None,
    vram_required_mb: int,
    duration_slots: int = 1,
    priority: int = 1,
) -> Iterator[str | None]:
    """Hold one Sextant GPU lease for the complete provider operation."""
    if not _configured():
        if ADMISSION_REQUIRED:
            raise GpuAdmissionError("Sextant GPU admission is required but not configured")
        yield None
        return

    resolved_id = workload_id or f"mediastudio-{workload_class}-{uuid.uuid4().hex}"
    lease_id = _acquire(
        workload_class,
        workload_id=resolved_id,
        vram_required_mb=vram_required_mb,
        duration_slots=duration_slots,
        priority=priority,
    )
    stop = threading.Event()
    thread = threading.Thread(target=_heartbeat, args=(lease_id, stop), daemon=True)
    thread.start()
    try:
        yield lease_id
    finally:
        stop.set()
        thread.join(timeout=1)
        try:
            _post("/internal/v1/phronesis/admission/release", {"lease_id": lease_id})
        except requests.RequestException:
            pass


def governed_post(
    session,
    url: str,
    *,
    workload_class: str,
    vram_required_mb: int,
    duration_slots: int = 1,
    priority: int = 1,
    **request_kwargs,
):
    """Submit one synchronous provider request while holding its GPU lease."""
    with admit_gpu(
        workload_class,
        vram_required_mb=vram_required_mb,
        duration_slots=duration_slots,
        priority=priority,
    ):
        return session.post(url, **request_kwargs)
