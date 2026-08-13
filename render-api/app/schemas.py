"""Pydantic schemas for API requests and responses."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, conlist


class SceneTimelineEntry(BaseModel):
    image: str
    duration: Optional[float] = None
    header: Optional[str] = None

    class Config:
        extra = "allow"


class Scene(BaseModel):
    title: str
    description: Optional[str] = None
    VO: str = Field(alias="VO")
    images: conlist(str, min_length=1)
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


class ProjectStateRequest(BaseModel):
    state: Dict[str, Any] = Field(default_factory=dict)


class ProviderRegistryItem(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: str = Field(default="metadata")
    requires_key: bool = False
    supports_import: bool = False
    rights_filter: Optional[str] = None
    status: str = Field(default="enabled")


class ProviderRegistryResponse(BaseModel):
    providers: List[ProviderRegistryItem] = Field(default_factory=list)


class AssetCandidate(BaseModel):
    provider: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    title: str = ""
    creator: str = ""
    date: str = ""
    media_type: str = Field(default="image")
    thumbnail_url: Optional[str] = None
    image_url: Optional[str] = None
    source_url: Optional[str] = None
    license: str = ""
    rights_status: str = "unknown"
    attribution: str = ""
    dimensions: Dict[str, Any] = Field(default_factory=dict)
    query: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class AssetSearchResponse(BaseModel):
    provider: str
    query: Dict[str, Any] = Field(default_factory=dict)
    candidates: List[AssetCandidate] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ExternalAssetSelection(BaseModel):
    filename: str = Field(min_length=1)
    candidate: AssetCandidate
    selected_at: Optional[str] = None
    review_status: str = "approved"


class SourceCard(BaseModel):
    provider: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    title: str = ""
    creator: str = ""
    source_url: Optional[str] = None
    record_type: str = "metadata"
    summary: str = ""
    rights_status: str = "metadata_only"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


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
    introEnabled: bool = Field(default=False)
    introTitle: Optional[str] = None
    introLeaderImage: Optional[str] = None
    introDuration: float = Field(default=1.0, gt=0)
    thumbnailEnabled: bool = Field(default=True)
    logoEnabled: bool = Field(default=True)
    logoImage: Optional[str] = None
    logoCorner: str = Field(default="top-right")
    logoMargin: int = Field(default=24, ge=0)

    class Config:
        extra = "allow"


class RenderRequest(BaseModel):
    outputName: str = Field(default="video.mp4")
    renderOptions: RenderOptions


class BibleVideoRequest(BaseModel):
    passage: str = Field(min_length=3, max_length=120)
    translation: str = Field(default="kjv", min_length=2, max_length=20)
    mode: str = Field(default="still", pattern="^(still|motion)$")
    visualStyle: str = Field(default="cinematic natural light", min_length=3, max_length=160)
    voice: str = Field(default="en-US-AdamMultilingualNeural", min_length=1, max_length=160)
    language: str = Field(default="en-US", min_length=2, max_length=32)
    ttsApi: str = Field(default="voice-gateway", min_length=2, max_length=64)
    outputName: str = Field(default="video.mp4", min_length=5, max_length=128)
    renderOptions: RenderOptions = Field(default_factory=RenderOptions)


class ScriptEnhanceRequest(BaseModel):
    script: str = Field(min_length=1)
    targetSeconds: int = Field(ge=5, le=3600)
    roomInfo: List[Dict[str, Any]] = Field(default_factory=list)


class ScriptEnhanceResponse(BaseModel):
    script: str
    targetSeconds: int
    model: str


class LeadCardGenerateRequest(BaseModel):
    title: str = ""
    script: str = ""
    currentLines: List[str] = Field(default_factory=list)


class LeadCardGenerateResponse(BaseModel):
    lines: conlist(str, min_length=3, max_length=3)
    model: str


class YouTubeDescriptionRequest(BaseModel):
    title: str = ""
    script: str = ""
    currentDescription: str = ""
    roomInfo: List[Dict[str, Any]] = Field(default_factory=list)


class YouTubeDescriptionResponse(BaseModel):
    description: str
    model: str


class YouTubeUploadRequest(BaseModel):
    filename: str = Field(default="video.mp4")
    title: str = Field(min_length=1)
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    categoryId: str = Field(default="22")
    privacyStatus: str = Field(default="private")
    madeForKids: bool = Field(default=False)
    profile: Literal["english", "mandarin"] = Field(default="english")


class YouTubeUploadResponse(BaseModel):
    videoId: str
    url: str


class YouTubeAuthCompleteRequest(BaseModel):
    callbackUrl: str = Field(min_length=1)


class RoomAnnotation(BaseModel):
    filename: str = Field(min_length=1)
    header: str = ""
    roomDescription: str = ""
    label: str = ""
    confidence: Optional[float] = None
    source: str = "manual"


class RoomAnnotationRequest(BaseModel):
    annotations: List[RoomAnnotation] = Field(default_factory=list)
