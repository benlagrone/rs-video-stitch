"""Server-side Grok image edit and vision calls for listing staging."""
from __future__ import annotations

import base64
import json
import os

import requests

from app.grok_video_client import api_base, api_key


class GrokImageError(RuntimeError):
    pass


def vision_model() -> str:
    return os.getenv("XAI_VISION_MODEL", "grok-4-fast").strip() or "grok-4-fast"


def image_model() -> str:
    return os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-2.0").strip() or "grok-imagine-image-2.0"


def complete_json(messages: list[dict], session=requests, *, timeout: float = 180) -> dict:
    response = session.post(
        f"{api_base()}/chat/completions",
        headers=_headers(),
        json={
            "model": vision_model(),
            "messages": messages,
            "temperature": 0.2,
        },
        timeout=timeout,
    )
    _raise_for_status(response, "xAI could not read the listing photos")
    return _payload_from_chat(response.json() or {})


def edit_image(prompt: str, data_uri: str, session=requests, *, timeout: float = 180) -> bytes:
    response = session.post(
        f"{api_base()}/images/edits",
        headers=_headers(),
        json={
            "model": image_model(),
            "prompt": prompt,
            "image": {"url": data_uri, "type": "image_url"},
            "resolution": "2k",
            "quality": "medium",
            "n": 1,
            "response_format": "b64_json",
        },
        timeout=timeout,
    )
    _raise_for_status(response, "xAI could not stage the photo")
    return _image_bytes(response.json() or {}, session)


def _payload_from_chat(payload: dict) -> dict:
    choices = payload.get("choices") or []
    message = (choices[0] or {}).get("message") if choices else {}
    content = (message or {}).get("content")
    if isinstance(content, list):
        text = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    else:
        text = str(content or "")
    fenced = text.strip()
    if fenced.startswith("```"):
        fenced = fenced.split("\n", 1)[-1]
        if fenced.endswith("```"):
            fenced = fenced[: fenced.rfind("```")]
    try:
        parsed = json.loads(fenced.strip())
    except json.JSONDecodeError as exc:
        raise GrokImageError("xAI did not return a staging plan") from exc
    if not isinstance(parsed, dict):
        raise GrokImageError("xAI did not return a staging plan")
    return parsed


def _image_bytes(payload: dict, session) -> bytes:
    data = payload.get("data") or []
    item = data[0] if data else payload
    if not isinstance(item, dict):
        raise GrokImageError("xAI returned no staged photo")
    encoded = item.get("b64_json") or item.get("base64")
    if encoded:
        try:
            image = base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            raise GrokImageError("xAI returned an unreadable staged photo") from exc
        if not image:
            raise GrokImageError("xAI returned an empty staged photo")
        return image
    url = str(item.get("url") or "").strip()
    if not url:
        raise GrokImageError("xAI returned no staged photo")
    response = session.get(url, timeout=120)
    _raise_for_status(response, "xAI staged-photo download failed")
    if not response.content:
        raise GrokImageError("xAI returned an empty staged photo")
    return response.content


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
    raise GrokImageError(f"{message} ({response.status_code}): {detail}")
