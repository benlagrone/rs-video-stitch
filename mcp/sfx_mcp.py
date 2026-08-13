#!/usr/bin/env python3
"""No-attribution sound-effects catalog MCP for Codex and Fortress Sextant."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urlparse


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mediastudio-sfx"
SERVER_VERSION = "0.1.0"

POLICY_NAME = "no-attribution-only"
CATALOG_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 250 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".ogg", ".wav"}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "pixabay": {
        "display_name": "Pixabay Sound Effects",
        "license_name": "Pixabay Content License",
        "license_url": "https://pixabay.com/service/license-summary/",
        "terms_url": "https://pixabay.com/service/terms/",
        "allowed_hosts": {"pixabay.com", "www.pixabay.com"},
        "required_path_prefix": "/sound-effects/",
        "attribution_required": False,
        "commercial_use": True,
        "social_media_use": True,
        "acquisition": "manual-single-item-download",
    },
    "mixkit": {
        "display_name": "Mixkit Sound Effects",
        "license_name": "Mixkit Sound Effects Free License",
        "license_url": "https://mixkit.co/license/",
        "terms_url": "https://mixkit.co/terms/",
        "allowed_hosts": {"mixkit.co", "www.mixkit.co"},
        "required_path_prefix": "/free-sound-effects/",
        "attribution_required": False,
        "commercial_use": True,
        "social_media_use": True,
        "acquisition": "manual-single-item-download",
    },
}


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def library_root() -> Path:
    configured = os.environ.get("SFX_LIBRARY_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Videos" / "MediaStudio" / "sfx-library").resolve()


def projects_root() -> Path:
    configured = os.environ.get("SFX_PROJECTS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    render_storage = os.environ.get("RENDER_STORAGE", "").strip()
    if render_storage:
        return (Path(render_storage).expanduser() / "projects").resolve()
    return (Path.home() / "Videos" / "MediaStudio" / "projects").resolve()


def import_roots() -> List[Path]:
    configured = os.environ.get("SFX_IMPORT_ROOTS", "").strip()
    if configured:
        candidates = [item for item in configured.split(os.pathsep) if item.strip()]
    else:
        candidates = [str(Path.home() / "Downloads"), str(library_root() / "inbox")]
    return [Path(item).expanduser().resolve() for item in candidates]


def max_asset_bytes() -> int:
    raw = os.environ.get("SFX_MAX_ASSET_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise McpError(-32603, "SFX_MAX_ASSET_BYTES must be an integer") from exc
    if value < 1:
        raise McpError(-32603, "SFX_MAX_ASSET_BYTES must be positive")
    return value


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def require_importable_path(raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise McpError(-32602, "source_path must be a non-empty string")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise McpError(-32602, f"source_path is not a file: {path}")
    if not any(is_within(path, root) for root in import_roots()):
        allowed = [str(root) for root in import_roots()]
        raise McpError(-32602, "source_path is outside SFX_IMPORT_ROOTS", {"allowed_roots": allowed})
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise McpError(-32602, f"unsupported audio extension: {path.suffix.lower()}")
    size = path.stat().st_size
    if size < 4 or size > max_asset_bytes():
        raise McpError(-32602, f"audio size must be between 4 and {max_asset_bytes()} bytes")
    validate_audio_header(path)
    return path


def validate_audio_header(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(12)
    suffix = path.suffix.lower()
    valid = False
    if suffix == ".wav":
        valid = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    elif suffix == ".flac":
        valid = header.startswith(b"fLaC")
    elif suffix == ".ogg":
        valid = header.startswith(b"OggS")
    elif suffix == ".mp3":
        valid = header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    if not valid:
        raise McpError(-32602, f"file header does not match {suffix} audio")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(value: str, suffix: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    if not stem:
        stem = "sound-effect"
    return f"{stem[:80]}{suffix.lower()}"


def validate_provider(provider: Any, source_url: Any) -> Dict[str, Any]:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise McpError(-32602, "provider must be one of: pixabay, mixkit")
    if not isinstance(source_url, str) or not source_url.strip():
        raise McpError(-32602, "source_url must be a non-empty string")
    parsed = urlparse(source_url)
    definition = PROVIDERS[provider]
    if parsed.scheme != "https" or parsed.hostname not in definition["allowed_hosts"]:
        raise McpError(-32602, f"source_url is not an allowed {provider} HTTPS URL")
    if not parsed.path.startswith(definition["required_path_prefix"]):
        raise McpError(-32602, f"source_url must point into {definition['required_path_prefix']}")
    if definition["attribution_required"]:
        raise McpError(-32603, f"provider violates {POLICY_NAME}: {provider}")
    return definition


def empty_catalog() -> Dict[str, Any]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "policy": POLICY_NAME,
        "assets": [],
    }


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpError(-32603, f"unable to read JSON state: {path}") from exc
    if not isinstance(data, dict):
        raise McpError(-32603, f"JSON state must be an object: {path}")
    return data


def read_catalog() -> Dict[str, Any]:
    catalog = read_json(library_root() / "catalog.json", empty_catalog())
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION or catalog.get("policy") != POLICY_NAME:
        raise McpError(-32603, "unsupported or unsafe SFX catalog policy")
    if not isinstance(catalog.get("assets"), list):
        raise McpError(-32603, "SFX catalog assets must be an array")
    return catalog


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def tool_text(data: Any) -> str:
    return data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=True)


def tool_result(data: Any, is_error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": tool_text(data)}],
        "isError": is_error,
    }
    if isinstance(data, (dict, list)):
        result["structuredContent"] = data
    return result


def require_object(arguments: Any) -> Dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise McpError(-32602, "Tool arguments must be an object")
    return arguments


def policy_tool(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    return tool_result(
        {
            "policy": POLICY_NAME,
            "rule": "Only assets that require no attribution and permit commercial social-media video use are accepted.",
            "providers": {
                name: {
                    key: value
                    for key, value in definition.items()
                    if key not in {"allowed_hosts", "required_path_prefix"}
                }
                for name, definition in PROVIDERS.items()
            },
            "rejected": [
                "Creative Commons Attribution (CC BY)",
                "Creative Commons Attribution-NonCommercial (CC BY-NC)",
                "personal-use-only assets",
                "ripped or unattributed third-party audio",
                "automated provider scraping or bulk downloading",
            ],
        }
    )


def search_links_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise McpError(-32602, "query must be a non-empty string")
    normalized = " ".join(query.split())
    pixabay_query = quote(normalized, safe="")
    mixkit_slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return tool_result(
        {
            "query": normalized,
            "policy": POLICY_NAME,
            "results": [
                {
                    "provider": "pixabay",
                    "url": f"https://pixabay.com/sound-effects/search/{pixabay_query}/",
                    "instruction": "Choose and download one sound manually; do not use automated scraping.",
                },
                {
                    "provider": "mixkit",
                    "url": f"https://mixkit.co/free-sound-effects/{mixkit_slug}/",
                    "fallback_url": "https://mixkit.co/free-sound-effects/",
                    "instruction": "Use the category URL when available, otherwise search the free SFX page manually.",
                },
            ],
        }
    )


def import_asset_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source_path = require_importable_path(arguments.get("source_path"))
    provider = arguments.get("provider")
    source_url = arguments.get("source_url")
    definition = validate_provider(provider, source_url)
    title = arguments.get("title")
    if not isinstance(title, str) or not title.strip():
        raise McpError(-32602, "title must be a non-empty string")
    raw_tags = arguments.get("tags", [])
    if not isinstance(raw_tags, list) or any(not isinstance(item, str) for item in raw_tags):
        raise McpError(-32602, "tags must be an array of strings")
    tags = sorted({item.strip().lower() for item in raw_tags if item.strip()})

    digest = sha256_file(source_path)
    asset_id = f"sfx-{digest[:16]}"
    catalog = read_catalog()
    for existing in catalog["assets"]:
        if existing.get("sha256") == digest:
            return tool_result({"asset": existing, "already_present": True})

    root = library_root()
    asset_dir = root / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{digest[:16]}-{safe_filename(title, source_path.suffix)}"
    destination = (asset_dir / stored_filename).resolve()
    if not is_within(destination, asset_dir.resolve()):
        raise McpError(-32603, "refusing unsafe asset destination")
    if destination.exists() and sha256_file(destination) != digest:
        raise McpError(-32603, f"refusing to overwrite a different catalog asset: {destination}")
    shutil.copy2(source_path, destination)
    if sha256_file(destination) != digest:
        destination.unlink(missing_ok=True)
        raise McpError(-32603, "asset hash changed during import")

    asset = {
        "id": asset_id,
        "title": title.strip(),
        "provider": provider,
        "source_url": source_url.strip(),
        "license_name": definition["license_name"],
        "license_url": definition["license_url"],
        "attribution_required": False,
        "commercial_use": True,
        "social_media_use": True,
        "acquisition": definition["acquisition"],
        "original_filename": source_path.name,
        "stored_filename": stored_filename,
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "tags": tags,
        "imported_at": utc_now(),
    }
    catalog["assets"].append(asset)
    catalog["assets"].sort(key=lambda item: (str(item.get("title", "")).lower(), str(item.get("id", ""))))
    atomic_write_json(root / "catalog.json", catalog)
    return tool_result({"asset": asset, "already_present": False})


def list_assets_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = arguments.get("query", "")
    provider = arguments.get("provider")
    if not isinstance(query, str):
        raise McpError(-32602, "query must be a string")
    if provider is not None and provider not in PROVIDERS:
        raise McpError(-32602, "provider must be pixabay or mixkit")
    terms = [term for term in query.lower().split() if term]
    matches = []
    for asset in read_catalog()["assets"]:
        if provider is not None and asset.get("provider") != provider:
            continue
        haystack = " ".join(
            [str(asset.get("title", "")), " ".join(asset.get("tags", [])), str(asset.get("original_filename", ""))]
        ).lower()
        if all(term in haystack for term in terms):
            matches.append(asset)
    return tool_result({"policy": POLICY_NAME, "assets": matches, "count": len(matches)})


def find_asset(asset_id: Any) -> Dict[str, Any]:
    if not isinstance(asset_id, str) or not SAFE_IDENTIFIER.fullmatch(asset_id):
        raise McpError(-32602, "asset_id is invalid")
    for asset in read_catalog()["assets"]:
        if asset.get("id") == asset_id:
            return asset
    raise McpError(-32002, f"asset not found: {asset_id}")


def asset_path(asset: Dict[str, Any]) -> Path:
    root = (library_root() / "assets").resolve()
    path = (root / str(asset.get("stored_filename", ""))).resolve()
    if not is_within(path, root) or not path.is_file():
        raise McpError(-32603, f"catalog asset file is missing or unsafe: {asset.get('id')}")
    return path


def verify_asset_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    asset = find_asset(arguments.get("asset_id"))
    path = asset_path(asset)
    actual = sha256_file(path)
    expected = asset.get("sha256")
    return tool_result(
        {
            "asset_id": asset["id"],
            "ok": actual == expected,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "path": str(path),
            "policy": POLICY_NAME,
        },
        is_error=actual != expected,
    )


def stage_asset_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    asset = find_asset(arguments.get("asset_id"))
    project_id = arguments.get("project_id")
    if not isinstance(project_id, str) or not SAFE_IDENTIFIER.fullmatch(project_id):
        raise McpError(-32602, "project_id must contain only letters, numbers, dots, underscores, and hyphens")
    source = asset_path(asset)
    requested_filename = arguments.get("filename")
    if requested_filename is None:
        filename = safe_filename(str(asset.get("title", "sound-effect")), source.suffix)
    elif isinstance(requested_filename, str) and Path(requested_filename).name == requested_filename:
        filename = safe_filename(Path(requested_filename).stem, source.suffix)
    else:
        raise McpError(-32602, "filename must be a simple filename")

    root = projects_root()
    destination_dir = (root / project_id / "input" / "sound-effects").resolve()
    if not is_within(destination_dir, root):
        raise McpError(-32603, "refusing unsafe project destination")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if destination.exists() and sha256_file(destination) != asset["sha256"]:
        raise McpError(-32603, f"refusing to overwrite a different project asset: {destination}")
    shutil.copy2(source, destination)
    if sha256_file(destination) != asset["sha256"]:
        destination.unlink(missing_ok=True)
        raise McpError(-32603, "asset hash changed while staging")

    provenance_path = destination_dir / "sfx_provenance.json"
    provenance = read_json(provenance_path, {"schema_version": 1, "policy": POLICY_NAME, "assets": {}})
    if provenance.get("policy") != POLICY_NAME or not isinstance(provenance.get("assets"), dict):
        raise McpError(-32603, "unsafe project SFX provenance policy")
    provenance["assets"][filename] = {
        "asset_id": asset["id"],
        "title": asset["title"],
        "provider": asset["provider"],
        "source_url": asset["source_url"],
        "license_name": asset["license_name"],
        "license_url": asset["license_url"],
        "attribution_required": False,
        "sha256": asset["sha256"],
        "staged_at": utc_now(),
    }
    atomic_write_json(provenance_path, provenance)
    return tool_result(
        {
            "asset_id": asset["id"],
            "project_id": project_id,
            "path": str(destination),
            "provenance_path": str(provenance_path),
            "policy": POLICY_NAME,
        }
    )


TOOLS: Dict[str, Dict[str, Any]] = {
    "sfx_policy": {
        "handler": policy_tool,
        "title": "Read No-Attribution SFX Policy",
        "description": "Return the fail-closed provider and licensing policy.",
        "read_only": True,
        "idempotent": True,
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "sfx_search_links": {
        "handler": search_links_tool,
        "title": "Build Approved SFX Search Links",
        "description": "Build manual single-item search links for approved no-attribution providers without scraping them.",
        "read_only": True,
        "idempotent": True,
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "sfx_import_asset": {
        "handler": import_asset_tool,
        "title": "Import Approved Sound Effect",
        "description": "Copy one manually downloaded Pixabay or Mixkit sound into the governed SFX catalog with provenance.",
        "read_only": False,
        "idempotent": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "provider": {"type": "string", "enum": ["pixabay", "mixkit"]},
                "source_url": {"type": "string"},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["source_path", "provider", "source_url", "title"],
            "additionalProperties": False,
        },
    },
    "sfx_list_assets": {
        "handler": list_assets_tool,
        "title": "List Sound Effects",
        "description": "List governed no-attribution sound effects, optionally filtered by text and provider.",
        "read_only": True,
        "idempotent": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "provider": {"type": "string", "enum": ["pixabay", "mixkit"]},
            },
            "additionalProperties": False,
        },
    },
    "sfx_verify_asset": {
        "handler": verify_asset_tool,
        "title": "Verify Sound Effect",
        "description": "Re-hash a governed sound effect and compare it with recorded provenance.",
        "read_only": True,
        "idempotent": True,
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
    },
    "sfx_stage_asset": {
        "handler": stage_asset_tool,
        "title": "Stage Sound Effect for Project",
        "description": "Copy a governed sound effect into a MediaStudio project's input/sound-effects directory with provenance.",
        "read_only": False,
        "idempotent": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "project_id": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["asset_id", "project_id"],
            "additionalProperties": False,
        },
    },
}


def tool_payload(name: str, definition: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "title": definition["title"],
        "description": definition["description"],
        "inputSchema": definition["inputSchema"],
        "annotations": {
            "readOnlyHint": definition["read_only"],
            "destructiveHint": False,
            "idempotentHint": definition["idempotent"],
        },
    }


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested_version = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = requested_version if requested_version == PROTOCOL_VERSION else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "MediaStudio No-Attribution SFX", "version": SERVER_VERSION},
                "instructions": (
                    "Use only Pixabay and Mixkit sound effects downloaded as individual items. "
                    "Never accept attribution-required, noncommercial, ripped, or unverified audio. "
                    "Search links are manual because provider terms prohibit scraping or mass downloads."
                ),
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [tool_payload(name, definition) for name, definition in TOOLS.items()]},
        }
    if method == "tools/call":
        if not isinstance(params, dict):
            raise McpError(-32602, "tools/call requires params")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            raise McpError(-32602, f"Unknown tool: {name}")
        arguments = require_object(params.get("arguments"))
        return {"jsonrpc": "2.0", "id": request_id, "result": TOOLS[name]["handler"](arguments)}
    raise McpError(-32601, f"Method not found: {method}")


def error_response(request_id: Any, error: McpError) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": error.code, "message": error.message},
    }
    if error.data is not None:
        payload["error"]["data"] = error.data
    return payload


def write_message(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def process_message(raw_line: str) -> Iterable[Dict[str, Any]]:
    try:
        message = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        yield error_response(None, McpError(-32700, "Parse error", str(exc)))
        return
    messages = message if isinstance(message, list) else [message]
    for item in messages:
        if not isinstance(item, dict):
            yield error_response(None, McpError(-32600, "Invalid request"))
            continue
        try:
            response = handle_request(item)
            if response is not None:
                yield response
        except McpError as exc:
            yield error_response(item.get("id"), exc)


def main() -> int:
    stderr(f"{SERVER_NAME} MCP server listening on stdio")
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        for response in process_message(raw_line):
            write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
