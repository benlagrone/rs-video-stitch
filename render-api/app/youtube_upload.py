"""YouTube upload integration adapted from MediaStudio/videoEdit/upload.py."""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://localhost:8082/v1/youtube/auth/callback"
DEFAULT_PROFILE = "english"
YOUTUBE_CHANNEL_PROFILES = {
    "english": {
        "label": "LeCrown Properties",
        "expectedChannelId": "UC_grGcaDW3AUszA8_MVGyJg",
        "handle": "@lecrownproperties",
        "fallbackIconUrl": "/media-studio/brand-assets/logo3.png",
    },
    "mandarin": {
        "label": "皇冠物业",
        "expectedChannelId": "UCn0cWV7cyNxzfXhUKgPcWeQ",
        "handle": "@皇冠物业",
        "fallbackIconUrl": "/media-studio/brand-assets/logo3.png",
    },
    "bible": {
        "label": "Animal Safari Kids",
        "expectedChannelId": "UCU1T3KZjLceczyfHr2aqpeQ",
        "handle": "",
        "fallbackIconUrl": "/media-studio/brand-assets/animal-safari-kids.png",
    },
}
YOUTUBE_PROFILES = {profile: details["label"] for profile, details in YOUTUBE_CHANNEL_PROFILES.items()}
_CHANNEL_CATALOG_TTL_SECONDS = 300
_channel_catalog_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
_channel_catalog_lock = threading.Lock()


class YouTubeUploadConfigurationError(RuntimeError):
    """Raised when the server is missing non-interactive YouTube credentials."""


def normalize_youtube_profile(profile: str = DEFAULT_PROFILE) -> str:
    normalized = str(profile or DEFAULT_PROFILE).strip().lower()
    if normalized not in YOUTUBE_PROFILES:
        raise YouTubeUploadConfigurationError(f"Unknown YouTube profile: {profile}")
    return normalized


def _profile_env_name(profile: str, suffix: str) -> str:
    return f"YOUTUBE_{normalize_youtube_profile(profile).upper()}_{suffix}"


def _token_file_path(profile: str = DEFAULT_PROFILE) -> Path:
    profile = normalize_youtube_profile(profile)
    base = Path(os.getenv("YOUTUBE_TOKEN_FILE", "/videos/youtube_token.json"))
    if profile == DEFAULT_PROFILE:
        return base
    default_path = base.with_name(f"youtube_token_{profile}.json")
    return Path(os.getenv(_profile_env_name(profile, "TOKEN_FILE"), str(default_path)))


def _state_file_path(profile: str = DEFAULT_PROFILE) -> Path:
    profile = normalize_youtube_profile(profile)
    base = Path(os.getenv("YOUTUBE_STATE_FILE", "/videos/youtube_oauth_state.txt"))
    if profile == DEFAULT_PROFILE:
        return base
    default_path = base.with_name(f"youtube_oauth_state_{profile}.txt")
    return Path(os.getenv(_profile_env_name(profile, "STATE_FILE"), str(default_path)))


def _read_state(profile: str = DEFAULT_PROFILE) -> dict[str, str]:
    state_file = _state_file_path(profile)
    if not state_file.exists():
        return {}
    raw = state_file.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"state": raw, "redirect_uri": youtube_redirect_uri(), "manual_callback": "false"}
    return {key: str(value) for key, value in parsed.items()}


def _write_state(*, profile: str, state: str, redirect_uri: str, manual_callback: bool) -> None:
    state_file = _state_file_path(profile)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "manual_callback": manual_callback,
            }
        ),
        encoding="utf-8",
    )


def _google_modules() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise YouTubeUploadConfigurationError(
            "YouTube upload dependencies are not installed in this environment."
        ) from exc
    return {
        "GoogleAuthRequest": GoogleAuthRequest,
        "Credentials": Credentials,
        "Flow": Flow,
        "build": build,
        "HttpError": HttpError,
        "MediaFileUpload": MediaFileUpload,
    }


def _save_credentials(token_file: Path, credentials: Any) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")


def _credentials_from_refresh_env(profile: str = DEFAULT_PROFILE) -> Optional[Any]:
    google = _google_modules()
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    profile = normalize_youtube_profile(profile)
    refresh_token = os.getenv(
        _profile_env_name(profile, "REFRESH_TOKEN"),
        os.getenv("YOUTUBE_REFRESH_TOKEN") if profile == DEFAULT_PROFILE else None,
    )
    token_uri = os.getenv("YOUTUBE_TOKEN_URI", DEFAULT_TOKEN_URI)

    if not (client_id and client_secret and refresh_token):
        return None

    credentials = google["Credentials"](
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    credentials.refresh(google["GoogleAuthRequest"]())
    return credentials


def _credentials_from_token_file(token_file: Path) -> Optional[Any]:
    if not token_file.exists():
        return None
    google = _google_modules()
    # Preserve the scopes already granted to older tokens. Passing the newly
    # expanded scope list here makes Google reject refreshes with invalid_scope;
    # the expanded list belongs on new consent flows, not legacy token loads.
    credentials = google["Credentials"].from_authorized_user_file(str(token_file))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(google["GoogleAuthRequest"]())
        _save_credentials(token_file, credentials)
    return credentials


def _token_granted_scopes(profile: str = DEFAULT_PROFILE) -> set[str]:
    token_file = _token_file_path(profile)
    if not token_file.exists():
        return set()
    try:
        payload = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scopes = payload.get("scopes") or []
    return {str(scope) for scope in scopes}


def authenticate_youtube(profile: str = DEFAULT_PROFILE):
    profile = normalize_youtube_profile(profile)
    token_file = _token_file_path(profile)
    credentials = _credentials_from_refresh_env(profile)
    if credentials:
        _save_credentials(token_file, credentials)
    else:
        credentials = _credentials_from_token_file(token_file)

    if not credentials:
        raise YouTubeUploadConfigurationError(
            "No non-interactive YouTube credentials available. "
            "Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, and YOUTUBE_REFRESH_TOKEN."
        )

    return _google_modules()["build"]("youtube", "v3", credentials=credentials)


def youtube_redirect_uri() -> str:
    return os.getenv("YOUTUBE_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def youtube_local_redirect_uri() -> str:
    explicit = os.getenv("YOUTUBE_LOCAL_REDIRECT_URI")
    if explicit:
        return explicit
    parsed = urlparse(youtube_redirect_uri())
    return urlunparse(("http", "localhost:8082", parsed.path, "", "", ""))


def _client_config() -> Optional[dict]:
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": os.getenv("YOUTUBE_TOKEN_URI", DEFAULT_TOKEN_URI),
            "redirect_uris": [youtube_redirect_uri()],
        }
    }


def _client_secret_file_config() -> Optional[dict]:
    client_secret_file = os.getenv("YOUTUBE_CLIENT_SECRET_FILE")
    if not client_secret_file:
        return None
    path = Path(client_secret_file)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _oauth_client_kind(config: Optional[dict]) -> str:
    if not config:
        return "env"
    if "web" in config:
        return "web"
    if "installed" in config:
        return "installed"
    return "unknown"


def _redirect_for_client_config(config: Optional[dict]) -> str:
    if _oauth_client_kind(config) != "installed":
        return youtube_redirect_uri()
    return youtube_local_redirect_uri()


def _is_loopback_redirect(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    hostname = (parsed.hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _loopback_bridge_enabled() -> bool:
    return os.getenv("YOUTUBE_LOOPBACK_BRIDGE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _manual_callback_required(config: Optional[dict], redirect_uri: str) -> bool:
    return (
        _oauth_client_kind(config) == "installed"
        and _is_loopback_redirect(redirect_uri)
        and not _loopback_bridge_enabled()
    )


def _auth_flow(redirect_uri: Optional[str] = None) -> Any:
    google = _google_modules()
    client_secret_file = os.getenv("YOUTUBE_CLIENT_SECRET_FILE")
    if client_secret_file and Path(client_secret_file).exists():
        flow = google["Flow"].from_client_secrets_file(client_secret_file, scopes=SCOPES)
    else:
        config = _client_config()
        if not config:
            raise YouTubeUploadConfigurationError(
                "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET, or set YOUTUBE_CLIENT_SECRET_FILE."
            )
        flow = google["Flow"].from_client_config(config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri or youtube_redirect_uri()
    return flow


def youtube_auth_status(profile: str = DEFAULT_PROFILE) -> dict:
    profile = normalize_youtube_profile(profile)
    config = _client_secret_file_config() or _client_config()
    redirect_uri = _redirect_for_client_config(config)
    manual_callback = _manual_callback_required(config, redirect_uri)
    configured = bool(
        config
        or _token_file_path(profile).exists()
        or (
            os.getenv("YOUTUBE_CLIENT_ID")
            and os.getenv("YOUTUBE_CLIENT_SECRET")
            and (
                os.getenv(_profile_env_name(profile, "REFRESH_TOKEN"))
                or (profile == DEFAULT_PROFILE and os.getenv("YOUTUBE_REFRESH_TOKEN"))
            )
        )
    )
    try:
        credentials = _credentials_from_token_file(_token_file_path(profile)) or _credentials_from_refresh_env(profile)
        return {
            "profile": profile,
            "label": YOUTUBE_PROFILES[profile],
            "configured": configured,
            "authenticated": bool(credentials),
            "metadataAuthorized": "https://www.googleapis.com/auth/youtube.force-ssl" in _token_granted_scopes(profile),
            "redirectUri": redirect_uri,
            "serverCallbackUri": youtube_redirect_uri(),
            "manualCallback": manual_callback,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "profile": profile,
            "label": YOUTUBE_PROFILES[profile],
            "configured": configured,
            "authenticated": False,
            "metadataAuthorized": False,
            "redirectUri": redirect_uri,
            "serverCallbackUri": youtube_redirect_uri(),
            "manualCallback": manual_callback,
            "error": str(exc),
        }


def youtube_channel_catalog(*, force: bool = False) -> list[dict[str, Any]]:
    """Return safe channel identities for every selectable server-side OAuth profile."""
    global _channel_catalog_cache
    now = time.monotonic()
    cached_at, cached_channels = _channel_catalog_cache
    if not force and cached_channels and now - cached_at < _CHANNEL_CATALOG_TTL_SECONDS:
        return cached_channels

    with _channel_catalog_lock:
        cached_at, cached_channels = _channel_catalog_cache
        if not force and cached_channels and now - cached_at < _CHANNEL_CATALOG_TTL_SECONDS:
            return cached_channels

        channels = []
        for profile, configured_identity in YOUTUBE_CHANNEL_PROFILES.items():
            status = youtube_auth_status(profile)
            channel = {
                "profile": profile,
                "label": configured_identity["label"],
                "channelName": configured_identity["label"],
                "channelId": "",
                "handle": configured_identity["handle"],
                "iconUrl": configured_identity["fallbackIconUrl"],
                "fallbackIconUrl": configured_identity["fallbackIconUrl"],
                "expectedChannelId": configured_identity["expectedChannelId"],
                "matchesExpectedChannel": None,
                **status,
            }
            if status.get("authenticated") and status.get("metadataAuthorized"):
                try:
                    response = authenticate_youtube(profile).channels().list(part="snippet", mine=True).execute()
                    item = (response.get("items") or [None])[0]
                    if item:
                        snippet = item.get("snippet") or {}
                        thumbnails = snippet.get("thumbnails") or {}
                        thumbnail = thumbnails.get("default") or thumbnails.get("medium") or {}
                        channel_id = str(item.get("id") or "")
                        channel.update(
                            {
                                "channelName": str(snippet.get("title") or configured_identity["label"]),
                                "channelId": channel_id,
                                "handle": str(snippet.get("customUrl") or configured_identity["handle"]),
                                "iconUrl": str(thumbnail.get("url") or configured_identity["fallbackIconUrl"]),
                                "matchesExpectedChannel": channel_id == configured_identity["expectedChannelId"],
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    channel["identityError"] = str(exc)
            channels.append(channel)

        _channel_catalog_cache = (now, channels)
        return channels


def youtube_authorization_url(profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    profile = normalize_youtube_profile(profile)
    config = _client_secret_file_config() or _client_config()
    redirect_uri = _redirect_for_client_config(config)
    manual_callback = _manual_callback_required(config, redirect_uri)
    flow = _auth_flow(redirect_uri=redirect_uri)
    state = f"{profile}.{uuid.uuid4().hex}"
    _write_state(profile=profile, state=state, redirect_uri=redirect_uri, manual_callback=manual_callback)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return {
        "profile": profile,
        "label": YOUTUBE_PROFILES[profile],
        "authUrl": authorization_url,
        "redirectUri": redirect_uri,
        "manualCallback": manual_callback,
    }


def _profile_from_state(state: str) -> str:
    prefix, separator, _ = state.partition(".")
    return normalize_youtube_profile(prefix) if separator else DEFAULT_PROFILE


def complete_youtube_auth(code: str, state: str, profile: Optional[str] = None) -> str:
    profile = normalize_youtube_profile(profile or _profile_from_state(state))
    state_payload = _read_state(profile)
    expected_state = state_payload.get("state", "")
    if not expected_state or state != expected_state:
        raise YouTubeUploadConfigurationError("YouTube OAuth state did not match")
    flow = _auth_flow(redirect_uri=state_payload.get("redirect_uri") or youtube_redirect_uri())
    flow.fetch_token(code=code)
    _save_credentials(_token_file_path(profile), flow.credentials)
    return profile


def complete_youtube_auth_from_callback_url(callback_url: str, profile: Optional[str] = None) -> str:
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    error = (params.get("error") or [""])[0]
    if error:
        raise YouTubeUploadConfigurationError(f"YouTube OAuth returned an error: {error}")
    if not code or not state:
        raise YouTubeUploadConfigurationError("Paste the full YouTube callback URL containing code and state")
    return complete_youtube_auth(code=code, state=state, profile=profile)


def upload_video_to_youtube(
    *,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = "22",
    privacy_status: str = "private",
    made_for_kids: bool = False,
    profile: str = DEFAULT_PROFILE,
) -> str:
    if not video_path.exists() or not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    google = _google_modules()
    youtube = authenticate_youtube(profile)
    request_body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(category_id or "22"),
        },
        "status": {
            "privacyStatus": privacy_status or "private",
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }

    mime_type = "video/mpeg" if video_path.suffix.lower() == ".mpg" else "video/mp4"
    media = google["MediaFileUpload"](
        str(video_path),
        mimetype=mime_type,
        resumable=True,
        chunksize=1024 * 1024 * 5,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=request_body,
        media_body=media,
    )

    response = None
    retries = 0
    max_retries = 10

    while response is None and retries < max_retries:
        try:
            _, response = request.next_chunk(num_retries=5)
        except socket.timeout:
            retries += 1
            time.sleep(5)
        except google["HttpError"] as exc:
            if exc.resp.status in [500, 502, 503, 504]:
                retries += 1
                time.sleep(5)
            else:
                raise

    if not response:
        raise RuntimeError("Upload failed after maximum retries")

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload response did not include a video id")
    return str(video_id)


def update_youtube_video_metadata(
    *,
    video_id: str,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = "22",
    privacy_status: str = "private",
    made_for_kids: bool = False,
    profile: str = DEFAULT_PROFILE,
) -> None:
    """Update metadata for an existing video using the selected channel profile."""
    if not video_id.strip():
        raise ValueError("YouTube video id is required")

    youtube = authenticate_youtube(profile)
    youtube.videos().update(
        part="snippet,status",
        body={
            "id": video_id.strip(),
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": str(category_id or "22"),
            },
            "status": {
                "privacyStatus": privacy_status or "private",
                "selfDeclaredMadeForKids": bool(made_for_kids),
            },
        },
    ).execute()


def set_youtube_thumbnail(
    *,
    video_id: str,
    thumbnail_path: Path,
    profile: str = DEFAULT_PROFILE,
) -> None:
    """Set or replace a video's custom thumbnail using the selected channel profile."""
    if not video_id.strip():
        raise ValueError("YouTube video id is required")
    if not thumbnail_path.exists() or not thumbnail_path.is_file():
        raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_path}")
    if thumbnail_path.stat().st_size > 2_000_000:
        raise ValueError("YouTube thumbnail must be 2 MB or smaller")

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    mime_type = mime_types.get(thumbnail_path.suffix.lower())
    if not mime_type:
        raise ValueError("YouTube thumbnail must be a JPEG or PNG image")

    google = _google_modules()
    youtube = authenticate_youtube(profile)
    media = google["MediaFileUpload"](
        str(thumbnail_path),
        mimetype=mime_type,
        resumable=False,
    )
    youtube.thumbnails().set(videoId=video_id.strip(), media_body=media).execute()
