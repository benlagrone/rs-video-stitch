"""Pydantic schemas for API requests and responses."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, Field, conlist


class SceneTimelineEntry(BaseModel):
    image: str
    duration: float

    class Config:
        extra = "allow"


class Scene(BaseModel):
    title: str
    description: Optional[str] = None
    VO: str = Field(alias="VO")
    images: conlist(str, min_length=1, max_length=3)
    timeline: Optional[List[SceneTimelineEntry]] = None
    duration: Optional[float] = None

    class Config:
        populate_by_name = True
        extra = "allow"


class VideoSettings(BaseModel):
    voice: Optional[str] = None
    language: Optional[str] = Field(default=None, alias="lang")
    api: Optional[str] = Field(default=None, validation_alias=AliasChoices("api", "tts_api"))

    class Config:
        populate_by_name = True


class ProjectSpec(BaseModel):
    info: Optional[Dict[str, Any]] = None
    scenes: List[Scene]
    video: Optional[VideoSettings] = Field(
        default=None,
        alias="vid",
        validation_alias=AliasChoices("vid", "video"),
    )


class TitleStyle(BaseModel):
    fontFamily: Optional[str] = None
    fill: Optional[str] = None
    outline: Optional[str] = None
    fontSize: Optional[float] = None
    position: Optional[str] = None

    class Config:
        extra = "allow"


class RenderOptions(BaseModel):
    fps: int = Field(default=30, ge=1)
    minShot: float = Field(default=2.5, gt=0)
    maxShot: float = Field(default=8.0, gt=0)
    xfade: float = Field(default=0.5, ge=0.0)
    crf: int = Field(default=18, ge=0, le=51)
    preset: str = Field(default="medium")
    tts: Optional[str] = None
    ttsLanguage: Optional[str] = None
    ttsApi: Optional[str] = Field(
        default=None,
        alias="ttsApi",
        validation_alias=AliasChoices("ttsApi", "tts_api"),
    )
    voiceDir: Optional[str] = None
    music: Optional[str] = None
    ducking: bool = Field(default=False)
    titleStyle: Optional[TitleStyle] = None

    class Config:
        extra = "allow"


class RenderRequest(BaseModel):
    outputName: str = Field(default="video.mp4")
    renderOptions: RenderOptions
