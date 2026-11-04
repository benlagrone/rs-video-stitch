#!/usr/bin/env bash
set -euo pipefail

[[ "$(hostname -s)" == "fortress-sextant" ]] || {
  printf 'Video caption validation must run on fortress-sextant.\n' >&2
  exit 65
}

docker exec -i mediastudio-api python - <<'PY'
import json
import subprocess
import urllib.request

from app.video_captions import _gateway, _provider_profile

with urllib.request.urlopen("http://127.0.0.1:8080/openapi.json", timeout=10) as response:
    schemas = json.load(response)["components"]["schemas"]
assert schemas["RenderOptions"]["properties"]["captionsEnabled"]["type"] == "boolean"
assert "captionsEnabled" in schemas["TikTokVariantRequest"]["properties"]
filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=True).stdout
assert " ass " in filters, "FFmpeg must include the ASS subtitle filter"
font = subprocess.run(["fc-match", "--format=%{family}", "Noto Sans CJK SC"], capture_output=True, text=True, check=True).stdout
assert "Noto Sans CJK" in font, "CJK-capable subtitle font is unavailable"
base, _endpoint, headers = _gateway()
profile = _provider_profile(base, headers)
assert "whisper" in profile["engine"].lower() and profile["model"], "Whisper model metadata is missing"
print(json.dumps({"captionSchema": "ok", "subtitleFilter": "ok", "CJKFont": "ok", "Whisper": profile}))
PY

printf 'Video-caption schema, renderer dependencies, and Whisper gateway are healthy.\n'
