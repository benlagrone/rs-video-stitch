"""Helpers for synthesizing narration via an external xTTS service."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Optional, Protocol

import requests


class Logger(Protocol):
    def __call__(self, message: str) -> None:  # pragma: no cover - typing helper
        ...


class TTSConfigurationError(RuntimeError):
    """Raised when TTS configuration is incomplete."""


def synthesize_xtts(
    text: str,
    destination: Path,
    *,
    voice: Optional[str] = None,
    language: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    log: Optional[Logger] = None,
    timeout: float = 60.0,
) -> Path:
    """Call an xTTS-compatible HTTP endpoint and write the resulting WAV file.

    Parameters
    ----------
    text: str
        Script that should be spoken.
    destination: Path
        File path where the WAV output should be written.
    voice: Optional[str]
        Voice identifier understood by the remote service.
    language: Optional[str]
        Language code for synthesis (defaults to service setting).
    api_url: Optional[str]
        Base URL of the xTTS service. Falls back to the ``XTTS_API_URL`` env.
    api_key: Optional[str]
        Optional bearer/API key. Falls back to ``XTTS_API_KEY`` env.
    log: Optional[Logger]
        Optional logger for debug output.
    timeout: float
        Request timeout in seconds.
    """

    resolved_url = (api_url or os.getenv("XTTS_API_URL"))
    if not resolved_url:
        raise TTSConfigurationError("XTTS_API_URL is not configured")

    resolved_key = api_key or os.getenv("XTTS_API_KEY")
    resolved_lang = language or os.getenv("XTTS_LANGUAGE")

    payload = {"text": text}
    if voice:
        payload["speaker"] = voice
    if resolved_lang:
        payload["language"] = resolved_lang

    headers = {"Content-Type": "application/json"}
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"

    url = resolved_url.rstrip("/") + "/api/tts"
    if log:
        log(f"[xtts] POST {url} voice={voice!r} language={resolved_lang!r}")

    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"xTTS request failed: {exc} ({response.text[:200]})") from exc

    data = response.json()
    audio_b64 = data.get("audio") or data.get("wav") or data.get("audio_base64")
    if not audio_b64:
        raise RuntimeError("xTTS response missing 'audio' field")

    audio_bytes = base64.b64decode(audio_b64)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_bytes)

    if log:
        log(f"[xtts] wrote {destination}")

    return destination
