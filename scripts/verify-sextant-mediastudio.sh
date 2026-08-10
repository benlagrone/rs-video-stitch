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

printf 'Full MediaStudio is healthy on %s with migrated real-estate and Bible projects.\n' "$expected_host"
