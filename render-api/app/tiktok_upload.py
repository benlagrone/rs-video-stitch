"""TikTok Content Posting API integration for server-side MediaStudio exports."""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests

TIKTOK_AUTHORIZATION_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
TIKTOK_VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_SCOPES = ("user.info.basic", "user.info.profile", "video.publish")
TIKTOK_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024
TIKTOK_MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024


class TikTokUploadConfigurationError(RuntimeError):
    """Raised when TikTok OAuth or Direct Post is not configured correctly."""


class TikTokUploadTransferError(RuntimeError):
    """Retain an initialized publish id when media transfer fails."""

    def __init__(self, message: str, *, publish_id: str):
        super().__init__(message)
        self.publish_id = publish_id
        self.status = "UPLOAD_FAILED"


def _token_file_path() -> Path:
    return Path(os.getenv("TIKTOK_TOKEN_FILE", "/videos/tiktok_token.json"))


def _state_file_path() -> Path:
    return Path(os.getenv("TIKTOK_STATE_FILE", "/videos/tiktok_oauth_state.json"))


def _client_key() -> str:
    return os.getenv("TIKTOK_CLIENT_KEY", "").strip()


def _client_secret() -> str:
    return os.getenv("TIKTOK_CLIENT_SECRET", "").strip()


def tiktok_redirect_uri() -> str:
    return os.getenv("TIKTOK_REDIRECT_URI", "").strip()


def _configured() -> bool:
    return bool(_client_key() and _client_secret() and tiktok_redirect_uri())


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TikTokUploadConfigurationError(f"TikTok credential file is invalid: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _token_error(response: requests.Response, action: str) -> TikTokUploadConfigurationError:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return TikTokUploadConfigurationError(f"TikTok {action} failed: {payload}")


def _exchange_token(payload: dict[str, str]) -> dict[str, Any]:
    response = requests.post(
        TIKTOK_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not response.ok:
        raise _token_error(response, "token exchange")
    result = response.json()
    if not result.get("access_token"):
        raise TikTokUploadConfigurationError("TikTok token exchange did not return an access token")
    result["obtained_at"] = time.time()
    _save_json(_token_file_path(), result)
    return result


def _access_token() -> str:
    token = _load_json(_token_file_path())
    access_token = str(token.get("access_token") or "")
    expires_at = float(token.get("obtained_at") or 0) + float(token.get("expires_in") or 0)
    if access_token and (not expires_at or time.time() < expires_at - 60):
        return access_token
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        raise TikTokUploadConfigurationError("No active TikTok connection. Connect TikTok before publishing.")
    if not _configured():
        raise TikTokUploadConfigurationError("TikTok OAuth client settings are incomplete on the server.")
    return str(_exchange_token({
        "client_key": _client_key(),
        "client_secret": _client_secret(),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })["access_token"])


def _api_post(url: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    response = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"},
        timeout=timeout,
    )
    if not response.ok:
        raise TikTokUploadConfigurationError(f"TikTok request failed: {response.text}")
    result = response.json()
    error = result.get("error") or {}
    if error.get("code") not in (None, "ok", 0):
        raise TikTokUploadConfigurationError(f"TikTok request failed: {error.get('message') or error}")
    return result.get("data") or {}


def tiktok_creator_info() -> dict[str, Any]:
    data = _api_post(TIKTOK_CREATOR_INFO_URL, {})
    return {
        "username": str(data.get("creator_username") or ""),
        "nickname": str(data.get("creator_nickname") or ""),
        "avatarUrl": str(data.get("creator_avatar_url") or ""),
        "privacyLevelOptions": list(data.get("privacy_level_options") or []),
        "commentDisabled": bool(data.get("comment_disabled")),
        "duetDisabled": bool(data.get("duet_disabled")),
        "stitchDisabled": bool(data.get("stitch_disabled")),
        "maxVideoPostDurationSec": data.get("max_video_post_duration_sec"),
    }


def tiktok_auth_status() -> dict[str, Any]:
    token = _load_json(_token_file_path())
    authenticated = bool(token.get("access_token") or token.get("refresh_token"))
    status: dict[str, Any] = {
        "configured": _configured(),
        "authenticated": authenticated,
        "redirectUri": tiktok_redirect_uri(),
    }
    if authenticated:
        try:
            status["creator"] = tiktok_creator_info()
        except Exception as exc:  # noqa: BLE001
            status["authenticated"] = False
            status["error"] = str(exc)
    return status


def tiktok_authorization_url() -> dict[str, str]:
    if not _configured():
        raise TikTokUploadConfigurationError(
            "Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and TIKTOK_REDIRECT_URI on the MediaStudio server."
        )
    state = uuid.uuid4().hex
    _save_json(_state_file_path(), {"state": state})
    query = urlencode({
        "client_key": _client_key(),
        "response_type": "code",
        "scope": ",".join(TIKTOK_SCOPES),
        "redirect_uri": tiktok_redirect_uri(),
        "state": state,
    })
    return {"authUrl": f"{TIKTOK_AUTHORIZATION_URL}?{query}"}


def complete_tiktok_auth(code: str, state: str) -> None:
    expected_state = str(_load_json(_state_file_path()).get("state") or "")
    if not expected_state or state != expected_state:
        raise TikTokUploadConfigurationError("TikTok OAuth state did not match")
    if not _configured():
        raise TikTokUploadConfigurationError("TikTok OAuth client settings are incomplete on the server.")
    _exchange_token({
        "client_key": _client_key(),
        "client_secret": _client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": tiktok_redirect_uri(),
    })


def _video_duration(video_path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TikTokUploadConfigurationError("Could not read the video's duration before TikTok upload.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TikTokUploadConfigurationError("The video must have a valid positive duration before TikTok upload.")
    return duration


def publish_video_to_tiktok(
    *, video_path: Path, caption: str, privacy_level: str, is_aigc: bool,
    allow_comment: bool = False, allow_duet: bool = False,
    allow_stitch: bool = False, brand_organic: bool = False,
    expected_username: str = "",
) -> dict[str, str]:
    if not video_path.exists() or not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    size = video_path.stat().st_size
    if size == 0:
        raise TikTokUploadConfigurationError("The selected video is empty.")
    if size > TIKTOK_MAX_VIDEO_SIZE:
        raise TikTokUploadConfigurationError("The selected video exceeds TikTok's 4 GB upload limit.")
    duration = _video_duration(video_path)
    creator = tiktok_creator_info()
    username = str(creator.get("username") or "").lstrip("@")
    if expected_username and username.casefold() != expected_username.lstrip("@").casefold():
        raise TikTokUploadConfigurationError("The connected TikTok account changed. Review the current account before publishing.")
    allowed_privacy = creator["privacyLevelOptions"]
    if privacy_level not in allowed_privacy:
        raise TikTokUploadConfigurationError(
            f"TikTok does not allow {privacy_level} for this account. Choose one of: {', '.join(allowed_privacy) or 'none'}"
        )
    try:
        max_duration = float(creator.get("maxVideoPostDurationSec"))
    except (TypeError, ValueError) as exc:
        raise TikTokUploadConfigurationError("TikTok did not return this account's video duration limit.") from exc
    if not math.isfinite(max_duration) or max_duration <= 0:
        raise TikTokUploadConfigurationError("TikTok has not enabled video posting for this account.")
    if duration > max_duration:
        raise TikTokUploadConfigurationError(
            f"This video is {duration:.1f} seconds; TikTok allows up to {max_duration:g} seconds for this account."
        )

    chunk_size = min(TIKTOK_UPLOAD_CHUNK_SIZE, size)
    # TikTok requires floor division and merges trailing bytes into the final
    # chunk, rather than sending a small remainder as an additional chunk.
    total_chunks = max(1, size // chunk_size)
    initialized = _api_post(TIKTOK_VIDEO_INIT_URL, {
        "post_info": {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_duet": not allow_duet or bool(creator["duetDisabled"]),
            "disable_comment": not allow_comment or bool(creator["commentDisabled"]),
            "disable_stitch": not allow_stitch or bool(creator["stitchDisabled"]),
            "brand_organic_toggle": brand_organic,
            "is_aigc": is_aigc,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }, timeout=60)
    upload_url = str(initialized.get("upload_url") or "")
    publish_id = str(initialized.get("publish_id") or "")
    if not upload_url or not publish_id:
        if publish_id:
            raise TikTokUploadTransferError("TikTok did not return an upload URL.", publish_id=publish_id)
        raise TikTokUploadConfigurationError("TikTok did not return an upload URL and publish id")

    try:
        with video_path.open("rb") as video:
            offset = 0
            for index in range(total_chunks):
                bytes_to_read = size - offset if index == total_chunks - 1 else chunk_size
                chunk = video.read(bytes_to_read)
                if len(chunk) != bytes_to_read:
                    raise TikTokUploadTransferError("Could not read the complete MediaStudio video for TikTok.", publish_id=publish_id)
                end = offset + len(chunk) - 1
                response = requests.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    timeout=180,
                )
                if not response.ok:
                    raise TikTokUploadTransferError(f"TikTok media upload failed: {response.text}", publish_id=publish_id)
                offset = end + 1
    except (OSError, requests.RequestException) as exc:
        # requests errors can include the signed upload URL. Keep that URL out
        # of browser errors while retaining the id for checking this same post.
        raise TikTokUploadTransferError("TikTok media transfer failed. Check this publish id before retrying.", publish_id=publish_id) from exc
    profile_url = f"https://www.tiktok.com/@{quote(username, safe='')}" if username else ""
    return {"publishId": publish_id, "status": "PROCESSING", "profileUrl": profile_url}


def tiktok_publish_status(publish_id: str) -> str:
    data = _api_post(TIKTOK_STATUS_URL, {"publish_id": publish_id})
    return str(data.get("status") or "PROCESSING")
