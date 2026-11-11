"""Server-side client for the xAI Grok Imagine video API."""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests


class GrokVideoError(RuntimeError):
    pass


def api_base() -> str:
    return os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")


def api_key() -> str:
    key = os.getenv("XAI_API_KEY", "").strip()
    if not key:
        raise GrokVideoError("XAI_API_KEY is not configured on the MediaStudio server")
    return key


def submit_video(body: dict, session=requests, *, timeout: float = 60) -> str:
    response = session.post(
        f"{api_base()}/videos/generations",
        headers=_headers(),
        json=body,
        timeout=timeout,
    )
    _raise_for_status(response, "xAI rejected the video request")
    request_id = str((response.json() or {}).get("request_id") or "").strip()
    if not request_id:
        raise GrokVideoError("xAI did not return a video request id")
    return request_id


def wait_for_video(
    request_id: str,
    session=requests,
    *,
    timeout: float = 900,
    poll_seconds: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        response = session.get(
            f"{api_base()}/videos/{request_id}",
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=30,
        )
        _raise_for_status(response, "xAI video status check failed")
        payload = response.json() or {}
        status = str(payload.get("status") or "")
        if status == "done":
            return payload
        if status == "failed":
            detail = payload.get("error") or payload.get("message") or "video generation failed"
            raise GrokVideoError(f"xAI video generation failed: {detail}")
        if time.monotonic() >= deadline:
            raise GrokVideoError(f"xAI video {request_id} was still {status or 'pending'} when the wait ended")
        time.sleep(max(0.2, poll_seconds))


def download_video(url: str, destination: Path, session=requests) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(url, timeout=120)
    _raise_for_status(response, "xAI video download failed")
    if not response.content:
        raise GrokVideoError("xAI returned an empty video")
    destination.write_bytes(response.content)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }


def _raise_for_status(response, message: str) -> None:
    if 200 <= response.status_code < 300:
        return
    detail = ""
    try:
        payload = response.json()
        detail = str(payload.get("error") or payload.get("message") or payload)[:500]
    except (AttributeError, ValueError, TypeError):
        detail = str(getattr(response, "text", ""))[:500]
    raise GrokVideoError(f"{message} ({response.status_code}): {detail}")
