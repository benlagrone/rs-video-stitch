#!/usr/bin/env bash
set -euo pipefail

expected_host="fortress-sextant"
actual_host=$(hostname -s)
if [[ "$actual_host" != "$expected_host" ]]; then
  printf 'Refusing validation on %s; MediaStudio is locked to %s.\n' "$actual_host" "$expected_host" >&2
  exit 65
fi

base_url="http://127.0.0.1:8082"
curl --fail --silent --show-error "$base_url/healthz" >/dev/null
curl --fail --silent --show-error "$base_url/media-studio" >/dev/null

docker network inspect fortress-workload-control-net >/dev/null
for container in mediastudio-api mediastudio-worker; do
  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -qx 'FORTRESS_GPU_ADMISSION_REQUIRED=1'
  docker inspect "$container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
    | grep -qx 'fortress-workload-control-net'
done
docker inspect mediastudio-worker --format '{{json .Config.Env}}' | python3 -c '
import json, sys
names = {entry.partition("=")[0] for entry in json.load(sys.stdin)}
assert "TIKTOK_CLIENT_SECRET" not in names, "TikTok client credentials must stay in the API container"
'
docker inspect mediastudio-api --format '{{json .Mounts}}' | python3 -c '
import json, sys
assert any(mount.get("Destination") == "/videos" and mount.get("Type") in {"bind", "volume"} and mount.get("Source") for mount in json.load(sys.stdin)), "TikTok credentials require the persistent /videos mount"
'

docker exec -e REQUIRE_TIKTOK_AUTH="${REQUIRE_TIKTOK_AUTH:-0}" -i mediastudio-api python - <<'TIKTOK_PY'
import json
import os
from pathlib import Path
import urllib.request

settings = ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REDIRECT_URI")
assert all(key in os.environ for key in settings), "TikTok settings are not injected into the API container"
assert os.environ.get("TIKTOK_TOKEN_FILE") == "/videos/tiktok_token.json", "TikTok token storage must persist under /videos"
assert os.environ.get("TIKTOK_STATE_FILE") == "/videos/tiktok_oauth_state.json", "TikTok state storage must persist under /videos"
for key in ("TIKTOK_TOKEN_FILE", "TIKTOK_STATE_FILE"):
    path = Path(os.environ[key])
    if path.exists():
        assert path.stat().st_mode & 0o077 == 0, "TikTok credential/state files must be private to their owner"
with urllib.request.urlopen("http://127.0.0.1:8080/v1/tiktok/auth/status", timeout=65) as response:
    status = json.load(response)
assert isinstance(status, dict) and type(status.get("configured")) is bool and type(status.get("authenticated")) is bool, "TikTok status must expose configured/authenticated booleans"
assert status["configured"] == all(os.environ[key].strip() for key in settings), "TikTok status does not match API environment configuration"

def check_public_fields(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            assert normalized not in {"clientkey", "clientsecret", "accesstoken", "refreshtoken", "authorization", "token"}, "TikTok public status exposes a credential field"
            check_public_fields(child)
    elif isinstance(value, list):
        for child in value:
            check_public_fields(child)

check_public_fields(status)
private_values = [os.environ[key].strip() for key in settings[:2]]
token_path = Path(os.environ["TIKTOK_TOKEN_FILE"])
if token_path.exists():
    tokens = json.loads(token_path.read_text("utf-8"))
    private_values.extend(str(tokens.get(key) or "") for key in ("access_token", "refresh_token"))
public_json = json.dumps(status)
assert not any(value and value in public_json for value in private_values), "TikTok public status exposes a credential value"

if os.environ.get("REQUIRE_TIKTOK_AUTH") == "1":
    assert status["configured"], "TikTok OAuth is not configured"
    assert status["authenticated"], "TikTok account is not connected"
    username = str((status.get("creator") or {}).get("username") or "").lstrip("@").casefold()
    assert username == "lecrown_properties", "TikTok is connected to the wrong account"
    print("TikTok publishing acceptance passed for @lecrown_properties.")
elif not status["configured"]:
    print("TikTok is unconfigured: service healthy, publishing not connected.")
elif not status["authenticated"]:
    print("TikTok is configured; account connection is pending.")
else:
    print("TikTok is connected; run with REQUIRE_TIKTOK_AUTH=1 to verify the LeCrown account.")
TIKTOK_PY

docker exec -i mediastudio-api python - <<'PY'
import json
import os
import time
from urllib.error import URLError
import urllib.request

assert os.environ["OLLAMA_BASE_URL"] == "http://host.docker.internal:18134", "MediaStudio must use the managed private Ollama transport"
for attempt in range(3):
    try:
        with urllib.request.urlopen(os.environ["OLLAMA_BASE_URL"] + "/api/tags", timeout=5) as response:
            assert isinstance(json.load(response).get("models"), list), "Ollama model inventory is unavailable"
        break
    except (TimeoutError, URLError):
        if attempt == 2:
            raise
        time.sleep(1)

request = urllib.request.Request(
    os.environ["FORTRESS_OPTIMIZATION_MCP_URL"] + "/internal/v1/phronesis/admission",
    headers={"Authorization": f"Bearer {os.environ['FORTRESS_OPTIMIZATION_MCP_TOKEN']}"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    admission = json.load(response)
assert admission["status"] in {"idle", "leased"}
PY

sfx_catalog=$(curl --fail --silent --show-error "$base_url/v1/sfx/catalog")
python3 -c '
import json, sys
catalog = json.loads(sys.argv[1])
if catalog.get("policy") != "no-attribution-only":
    raise SystemExit("MediaStudio SFX catalog is not enforcing no-attribution-only")
if not isinstance(catalog.get("assets"), list):
    raise SystemExit("MediaStudio SFX catalog does not expose an asset list")
if catalog.get("owner") != "fortress.sextant:mediastudio-sfx-catalog":
    raise SystemExit("MediaStudio SFX catalog is not owned by fortress.sextant")
' "$sfx_catalog"

projects=$(curl --fail --silent --show-error "$base_url/v1/projects")
python3 -c '
import json, sys
projects = json.loads(sys.argv[1]).get("projects", [])
ids = {project.get("projectId") for project in projects}
required = {"p-jhng01-61859197", "p-jhng01-active-listings-20260810"}
missing = sorted(required - ids)
if missing:
    raise SystemExit("Missing migrated MediaStudio projects: " + ", ".join(missing))
if not any(project_id and project_id.startswith("bible-") for project_id in ids):
    raise SystemExit("No Bible project found after full MediaStudio migration")
' "$projects"

project=$(curl --fail --silent --show-error "$base_url/v1/projects/p-jhng01-61859197")
python3 -c '
import json, sys
project = json.loads(sys.argv[1])
outputs = set(project.get("outputs", []))
required = {"7131-harmony-cove-english-v4.mp4", "thumbnail.jpg"}
missing = sorted(required - outputs)
if missing:
    raise SystemExit("Missing migrated real-estate outputs: " + ", ".join(missing))
if len(project.get("assets", {}).get("images", [])) < 14:
    raise SystemExit("Migrated real-estate project is missing listing images")
' "$project"

youtube=$(curl --fail --silent --show-error "$base_url/v1/youtube/auth/status")
python3 -c '
import json, sys
status = json.loads(sys.argv[1])
if not status.get("authenticated"):
    raise SystemExit("Sextant MediaStudio YouTube upload authorization is not active")
' "$youtube"

youtube_channels=$(curl --fail --silent --show-error "$base_url/v1/youtube/channels?force=true")
python3 -c '
import json, sys
channels = {item.get("profile"): item for item in json.loads(sys.argv[1]).get("channels", [])}
expected = {
    "english": "LeCrown Properties",
    "mandarin": "皇冠物业",
    "animals": "Animals",
}
missing = sorted(set(expected) - set(channels))
if missing:
    raise SystemExit("Missing YouTube publishing profiles: " + ", ".join(missing))
for profile, name in expected.items():
    channel = channels[profile]
    if channel.get("channelName") != name:
        raise SystemExit("Unexpected %s YouTube channel: %s" % (profile, channel.get("channelName")))
    if not channel.get("iconUrl"):
        raise SystemExit(f"Missing {profile} YouTube channel icon")
for profile in ("english", "mandarin"):
    if channels[profile].get("matchesExpectedChannel") is not True:
        raise SystemExit(f"{profile} YouTube profile is connected to the wrong account")
' "$youtube_channels"

printf 'Full MediaStudio is healthy on %s with migrated real-estate and Bible projects.\n' "$expected_host"
