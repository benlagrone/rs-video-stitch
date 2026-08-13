"""Read the no-attribution sound-effects catalog for the MediaStudio UI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = 1
POLICY_NAME = "no-attribution-only"
ALLOWED_PROVIDERS = {
    "pixabay": "Pixabay Sound Effects",
    "mixkit": "Mixkit Sound Effects",
}


class SfxCatalogError(RuntimeError):
    """Raised when the catalog cannot be safely exposed."""


def library_root() -> Path:
    configured = os.getenv("SFX_LIBRARY_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(os.getenv("RENDER_STORAGE", "/videos")) / "sfx-library").resolve()


def _empty_catalog(status: str = "empty") -> dict[str, Any]:
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "policy": POLICY_NAME,
        "status": status,
        "owner": "fortress.sextant:mediastudio-sfx-catalog",
        "providers": [
            {"id": provider_id, "label": label, "attributionRequired": False}
            for provider_id, label in ALLOWED_PROVIDERS.items()
        ],
        "count": 0,
        "availableCount": 0,
        "assets": [],
    }


def list_sfx_catalog() -> dict[str, Any]:
    root = library_root()
    catalog_path = root / "catalog.json"
    if not catalog_path.exists():
        return _empty_catalog()

    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SfxCatalogError("sound-effects catalog is unreadable") from exc

    if not isinstance(catalog, dict):
        raise SfxCatalogError("sound-effects catalog must be a JSON object")
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION or catalog.get("policy") != POLICY_NAME:
        raise SfxCatalogError("sound-effects catalog has an unsupported or unsafe policy")
    if not isinstance(catalog.get("assets"), list):
        raise SfxCatalogError("sound-effects catalog assets must be an array")

    assets = []
    assets_dir = (root / "assets").resolve()
    for raw_asset in catalog["assets"]:
        if not isinstance(raw_asset, dict):
            continue
        provider = raw_asset.get("provider")
        if provider not in ALLOWED_PROVIDERS:
            continue
        if raw_asset.get("attribution_required") is not False:
            continue
        if raw_asset.get("commercial_use") is not True or raw_asset.get("social_media_use") is not True:
            continue

        stored_filename = str(raw_asset.get("stored_filename") or "")
        stored_path = (assets_dir / stored_filename).resolve()
        try:
            stored_path.relative_to(assets_dir)
        except ValueError:
            available = False
        else:
            available = bool(stored_filename) and stored_path.is_file()

        assets.append(
            {
                "id": str(raw_asset.get("id") or ""),
                "title": str(raw_asset.get("title") or "Untitled sound effect"),
                "provider": provider,
                "providerLabel": ALLOWED_PROVIDERS[provider],
                "sourceUrl": str(raw_asset.get("source_url") or ""),
                "licenseName": str(raw_asset.get("license_name") or ""),
                "licenseUrl": str(raw_asset.get("license_url") or ""),
                "attributionRequired": False,
                "commercialUse": True,
                "socialMediaUse": True,
                "format": Path(stored_filename).suffix.lower().lstrip("."),
                "bytes": raw_asset.get("bytes") if isinstance(raw_asset.get("bytes"), int) else None,
                "tags": [str(tag) for tag in raw_asset.get("tags", []) if isinstance(tag, str)],
                "importedAt": str(raw_asset.get("imported_at") or ""),
                "available": available,
            }
        )

    assets.sort(key=lambda asset: (asset["title"].lower(), asset["id"]))
    result = _empty_catalog("ready" if assets else "empty")
    result["assets"] = assets
    result["count"] = len(assets)
    result["availableCount"] = sum(1 for asset in assets if asset["available"])
    return result
