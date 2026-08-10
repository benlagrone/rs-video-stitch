"""YouTube upload integration adapted from MediaStudio/videoEdit/upload.py."""
from __future__ import annotations

import os
import socket
import time
import uuid
import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://fortress.lan:8082/v1/youtube/auth/callback"


class YouTubeUploadConfigurationError(RuntimeError):
    """Raised when the server is missing non-interactive YouTube credentials."""


def _token_file_path() -> Path:
    return Path(os.getenv("YOUTUBE_TOKEN_FILE", "/videos/youtube_token.json"))


def _state_file_path() -> Path:
    return Path(os.getenv("YOUTUBE_STATE_FILE", "/videos/youtube_oauth_state.txt"))


def _read_state() -> dict[str, str]:
    state_file = _state_file_path()
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


def _write_state(*, state: str, redirect_uri: str, manual_callback: bool) -> None:
    state_file = _state_file_path()
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


def _credentials_from_refresh_env() -> Optional[Any]:
    google = _google_modules()
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN")
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
    credentials = google["Credentials"].from_authorized_user_file(str(token_file), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(google["GoogleAuthRequest"]())
        _save_credentials(token_file, credentials)
    return credentials


def authenticate_youtube():
    token_file = _token_file_path()
    credentials = _credentials_from_refresh_env()
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


def youtube_auth_status() -> dict:
    config = _client_secret_file_config() or _client_config()
    redirect_uri = _redirect_for_client_config(config)
    manual_callback = _oauth_client_kind(config) == "installed" and _is_loopback_redirect(redirect_uri)
    try:
        credentials = _credentials_from_token_file(_token_file_path()) or _credentials_from_refresh_env()
        return {
            "configured": True,
            "authenticated": bool(credentials),
            "redirectUri": redirect_uri,
            "serverCallbackUri": youtube_redirect_uri(),
            "manualCallback": manual_callback,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": True,
            "authenticated": False,
            "redirectUri": redirect_uri,
            "serverCallbackUri": youtube_redirect_uri(),
            "manualCallback": manual_callback,
            "error": str(exc),
        }


def youtube_authorization_url() -> dict[str, Any]:
    config = _client_secret_file_config() or _client_config()
    redirect_uri = _redirect_for_client_config(config)
    manual_callback = _oauth_client_kind(config) == "installed" and _is_loopback_redirect(redirect_uri)
    flow = _auth_flow(redirect_uri=redirect_uri)
    state = uuid.uuid4().hex
    _write_state(state=state, redirect_uri=redirect_uri, manual_callback=manual_callback)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return {
        "authUrl": authorization_url,
        "redirectUri": redirect_uri,
        "manualCallback": manual_callback,
    }


def complete_youtube_auth(code: str, state: str) -> None:
    state_payload = _read_state()
    expected_state = state_payload.get("state", "")
    if not expected_state or state != expected_state:
        raise YouTubeUploadConfigurationError("YouTube OAuth state did not match")
    flow = _auth_flow(redirect_uri=state_payload.get("redirect_uri") or youtube_redirect_uri())
    flow.fetch_token(code=code)
    _save_credentials(_token_file_path(), flow.credentials)


def complete_youtube_auth_from_callback_url(callback_url: str) -> None:
    parsed = urlparse(callback_url)
    params = parse_qs(parsed.query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    error = (params.get("error") or [""])[0]
    if error:
        raise YouTubeUploadConfigurationError(f"YouTube OAuth returned an error: {error}")
    if not code or not state:
        raise YouTubeUploadConfigurationError("Paste the full YouTube callback URL containing code and state")
    complete_youtube_auth(code=code, state=state)


def upload_video_to_youtube(
    *,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = "22",
    privacy_status: str = "private",
    made_for_kids: bool = False,
) -> str:
    if not video_path.exists() or not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    google = _google_modules()
    youtube = authenticate_youtube()
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
