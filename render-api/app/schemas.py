"""Pydantic schemas for API requests and responses."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, conlist


class Scene(BaseModel):
    title: str
    description: Optional[str] = None
    VO: str = Field(alias="VO")
    images: conlist(str, min_length=1, max_length=3)

    class Config:
        populate_by_name = True


class ProjectSpec(BaseModel):
    info: Optional[Dict[str, Any]] = None
    scenes: List[Scene]


class RenderOptions(BaseModel):
    fps: int = Field(default=30, ge=1)
    minShot: float = Field(default=2.5, gt=0)
    maxShot: float = Field(default=8.0, gt=0)
    xfade: float = Field(default=0.5, ge=0.0)
    crf: int = Field(default=18, ge=0, le=51)
    preset: str = Field(default="medium")
    tts: Optional[str] = None
    voiceDir: Optional[str] = None
    music: Optional[str] = None
    ducking: bool = Field(default=False)


class RenderRequest(BaseModel):
    outputName: str = Field(default="video.mp4")
    renderOptions: RenderOptions
