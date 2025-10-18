"""Helpers for synthesizing narration via external text-to-speech services."""
from __future__ import annotations

import base64
import os
import time
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

    resolved_url = api_url or os.getenv("XTTS_API_URL") or "http://xtts:5002"
    if not resolved_url:
        raise TTSConfigurationError("XTTS_API_URL is not configured")

    resolved_key = api_key or os.getenv("XTTS_API_KEY")
    resolved_lang = language or os.getenv("XTTS_LANGUAGE")

    payload = {"text": text}
    if voice:
        payload["speaker"] = voice
        payload.setdefault("voice", voice)
    if resolved_lang:
        payload["language"] = resolved_lang

    headers = {"Content-Type": "application/json"}
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"

    url = resolved_url.rstrip("/") + "/api/tts"
    if log:
        preview = text.strip().splitlines()[0] if text.strip() else ""
        preview = (preview[:60] + "…") if len(preview) > 60 else preview
        log(
            "[xtts] POST %s voice=%r language=%r body_preview=%r"
            % (url, voice, resolved_lang, preview)
        )

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        message = f"xTTS request failed to reach {url}: {exc}"
        if log:
            log(f"[xtts] error: {message}")
        raise RuntimeError(message) from exc

    try:
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        snippet = response.text[:200] if hasattr(response, "text") else ""
        if log:
            log(
                f"[xtts] HTTP {response.status_code} error. Response snippet: {snippet!r}"
            )
        raise RuntimeError(
            f"xTTS request failed: {exc} (status={response.status_code}, body={snippet})"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()

    if content_type.startswith("audio/"):
        destination.write_bytes(response.content)
    else:
        data = response.json()
        audio_b64 = data.get("audio") or data.get("wav") or data.get("audio_base64")
        if not audio_b64:
            raise RuntimeError("xTTS response missing 'audio' field")
        audio_bytes = base64.b64decode(audio_b64)
        destination.write_bytes(audio_bytes)

    if log:
        log(f"[xtts] wrote {destination}")

    return destination


def synthesize_azure(
    text: str,
    destination: Path,
    *,
    voice: Optional[str] = None,
    language: Optional[str] = None,
    api_key: Optional[str] = None,
    region: Optional[str] = None,
    log: Optional[Logger] = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> Path:
    """Synthesize narration using the Azure Cognitive Services Speech API."""

    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise TTSConfigurationError(
            "azure-cognitiveservices-speech is required for Azure TTS support"
        ) from exc

    resolved_key = (
        api_key
        or os.getenv("AZURE_KEY")
        or os.getenv("AZURE_SPEECH_KEY")
        or os.getenv("AZURE_TTS_KEY")
    )
    resolved_region = (
        region
        or os.getenv("AZURE_REGION")
        or os.getenv("AZURE_SPEECH_REGION")
        or os.getenv("AZURE_TTS_REGION")
    )
    if not resolved_key or not resolved_region:
        raise TTSConfigurationError(
            "Azure Speech credentials are not configured. "
            "Set AZURE_KEY/AZURE_REGION or AZURE_SPEECH_KEY/AZURE_SPEECH_REGION."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    speech_config = speechsdk.SpeechConfig(
        subscription=resolved_key, region=resolved_region
    )

    resolved_voice = voice or os.getenv("AZURE_TTS_VOICE")
    if resolved_voice:
        speech_config.speech_synthesis_voice_name = resolved_voice
    if language:
        speech_config.speech_synthesis_language = language

    try:
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Riff48Khz16BitMonoPcm
        )
    except AttributeError:  # pragma: no cover - defensive when SDK changes
        if log:
            log("[azure-tts] unable to set output format; using SDK default")

    audio_config = speechsdk.audio.AudioOutputConfig(filename=str(destination))
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            preview = text.strip().splitlines()[0] if text.strip() else ""
            if log:
                log(
                    "[azure-tts] attempt %d voice=%r language=%r text=%r"
                    % (attempt, resolved_voice, language, preview[:60])
                )
            result = synthesizer.speak_text_async(text).get()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if log:
                log(f"[azure-tts] request failed: {exc}")
        else:
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                if log:
                    log(f"[azure-tts] wrote {destination}")
                return destination

            cancellation = speechsdk.CancellationDetails(result)
            message = (
                f"Speech synthesis canceled: reason={cancellation.reason} "
                f"details={cancellation.error_details}"
            )
            last_error = RuntimeError(message)
            if log:
                log(f"[azure-tts] {message}")

        if attempt < retries:
            time.sleep(backoff ** attempt)

    assert last_error is not None  # mypy appeasement; loop always sets on failure
    raise last_error
