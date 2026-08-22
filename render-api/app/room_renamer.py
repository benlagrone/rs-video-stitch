"""Server-side adapter for the Fortress Room Renamer capability."""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Iterable

import requests


GENERIC_HEADER_PATTERN = re.compile(
    r"^(?:(?:property|listing|real estate)\s+)?photo\s*\d+$|^房(?:产|源)?照片\s*\d+$",
    flags=re.IGNORECASE,
)

ROOM_TITLES = {
    "aerial_view": {"en": "Aerial Property View", "zh": "房产鸟瞰图"},
    "back_yard": {"en": "Back Yard", "zh": "后院"},
    "bathroom": {"en": "Bathroom", "zh": "浴室"},
    "bedroom": {"en": "Bedroom", "zh": "卧室"},
    "breakfast_room": {"en": "Breakfast Area", "zh": "早餐区"},
    "den": {"en": "Den", "zh": "家庭休闲室"},
    "dining_room": {"en": "Dining Room", "zh": "餐厅"},
    "exterior": {"en": "Property Exterior", "zh": "住宅外观"},
    "foyer": {"en": "Foyer", "zh": "门厅"},
    "front_yard": {"en": "Front Yard", "zh": "前院"},
    "garage": {"en": "Garage", "zh": "车库"},
    "gym": {"en": "Fitness Center", "zh": "健身中心"},
    "hallway": {"en": "Hallway", "zh": "走廊"},
    "kitchen": {"en": "Kitchen", "zh": "厨房"},
    "laundry_room": {"en": "Laundry Room", "zh": "洗衣房"},
    "living_room": {"en": "Living Room", "zh": "客厅"},
    "master_bathroom": {"en": "Primary Bathroom", "zh": "主卧浴室"},
    "master_bedroom": {"en": "Primary Bedroom", "zh": "主卧室"},
    "office": {"en": "Home Office", "zh": "家庭办公室"},
    "playroom": {"en": "Game Room", "zh": "娱乐室"},
    "pool": {"en": "Swimming Pool", "zh": "游泳池"},
    "stairway": {"en": "Stairway", "zh": "楼梯"},
    "street_view": {"en": "Front Exterior", "zh": "住宅正面"},
    "unknown": {"en": "Property View", "zh": "房屋展示"},
}


class RoomRenamerError(RuntimeError):
    """Raised when the protected Room Renamer capability is unavailable."""


def canonical_room_label(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower().replace(" ", "_")).strip("_")


def is_generic_header(value: str) -> bool:
    return not str(value or "").strip() or bool(GENERIC_HEADER_PATTERN.fullmatch(str(value).strip()))


def localized_room_title(label: str, language: str) -> str:
    canonical = canonical_room_label(label) or "unknown"
    locale = "zh" if str(language or "").lower().startswith("zh") else "en"
    titles = ROOM_TITLES.get(canonical)
    if titles:
        return titles[locale]
    words = canonical.replace("_", " ").strip()
    if locale == "zh":
        return ROOM_TITLES["unknown"]["zh"]
    return words.title() or ROOM_TITLES["unknown"]["en"]


class RoomRenamerClient:
    def __init__(self, base_url: str, *, token: str = "", timeout_seconds: float = 90) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def classify(self, images: Iterable[tuple[str, Path]]) -> dict:
        opened = []
        files = []
        try:
            for filename, path in images:
                handle = path.open("rb")
                opened.append(handle)
                media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                files.append(("files", (filename, handle, media_type)))
            if not files:
                return {"model": "", "mode": "", "results": []}
            response = requests.post(
                f"{self.base_url}/v1/classify",
                files=files,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get("results"), list):
                raise ValueError("Room Renamer response did not contain results.")
            return payload
        except (OSError, requests.RequestException, ValueError) as exc:
            raise RoomRenamerError(f"Room Renamer failed at {self.base_url}: {exc}") from exc
        finally:
            for handle in opened:
                handle.close()

    def submit_correction(
        self,
        *,
        image_path: Path,
        filename: str,
        corrected_label: str,
        predicted_label: str,
        project_id: str,
        language: str,
    ) -> dict:
        try:
            with image_path.open("rb") as handle:
                response = requests.post(
                    f"{self.base_url}/v1/corrections",
                    files={"file": (filename, handle, mimetypes.guess_type(filename)[0] or "application/octet-stream")},
                    data={
                        "corrected_label": corrected_label,
                        "predicted_label": predicted_label,
                        "project_id": project_id,
                        "source_filename": filename,
                        "language": language,
                    },
                    headers=self._headers(),
                    timeout=self.timeout_seconds,
                )
            response.raise_for_status()
            return response.json()
        except (OSError, requests.RequestException, ValueError) as exc:
            raise RoomRenamerError(f"Room Renamer correction failed at {self.base_url}: {exc}") from exc

