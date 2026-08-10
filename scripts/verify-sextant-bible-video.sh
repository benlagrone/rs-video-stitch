#!/usr/bin/env bash
set -euo pipefail

expected_host="fortress-sextant"
actual_host=$(hostname -s)
if [[ "$actual_host" != "$expected_host" ]]; then
  printf 'Refusing validation on %s; MediaStudio is locked to %s.\n' "$actual_host" "$expected_host" >&2
  exit 65
fi

curl --fail --silent --show-error http://127.0.0.1:8082/healthz >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8082/media-studio >/dev/null
health=$(curl --fail --silent --show-error http://127.0.0.1:8082/v1/bible/health)
python3 -c '
import json, sys
health = json.loads(sys.argv[1])
required = ("sextant", "mediastudio", "image", "motion")
failed = [name for name in required if not health.get(name, {}).get("ok")]
if failed:
    raise SystemExit("Unhealthy Bible Video Studio capabilities: " + ", ".join(failed))
' "$health"

printf 'Bible Video Studio is healthy on %s.\n' "$expected_host"
