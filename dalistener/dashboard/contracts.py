from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .sources import CaptureCategory


class MeetingStatus(StrEnum):
    PREPARING = "preparing"
    LIVE = "live"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


class MeetingSummary(BaseModel):
    id: str
    title: str
    browser: str = "Chromium"
    tab_id: int | None = None
    status: MeetingStatus
    transcription_provider: str = "openai"
    provider_reason: str | None = None
    fallback_active: bool = False
    compute_device: str = "cloud"
    openai_audio_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    transcription_delay_seconds: list[float] | None = None
    measured_transcription_lag_seconds: float | None = None
    intelligence_delay_seconds: list[float] | None = None
    transcription_model: str = "gpt-realtime-whisper"
    capture_category: CaptureCategory = CaptureCategory.OTHER
    site_domain: str = ""
    service_label: str = "Browser tab"
    started_at: datetime
    ended_at: datetime | None = None
    event_count: int = 0
    last_error: str | None = None


class TranscriptPayload(BaseModel):
    utterance_id: str
    source_id: str
    text: str
    start_ms: int
    end_ms: int
    revision: int
    stability: str
    detected_language: str | None = None
    language_probability: float | None = None


class OpenAIStatus(BaseModel):
    configured: bool
    active_streams: int
    transcription_model: str
    intelligence_model: str
    status: str
    message: str


class DashboardEvent(BaseModel):
    sequence: int
    event_type: str
    meeting_id: str | None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any]


class BootstrapResponse(BaseModel):
    meetings: list[MeetingSummary]
    openai: OpenAIStatus
    browser_audio_token: str
    provider_mode: str = "auto"
    pricing: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    local_model: dict[str, Any] = Field(default_factory=dict)


class BrowserCaptureHello(BaseModel):
    type: str = "start"
    title: str = Field(min_length=1, max_length=500)
    browser: str = Field(default="Chromium", max_length=1000)
    sample_rate: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=2)


class BrowserCaptureAck(BaseModel):
    type: str = "started"
    meeting_id: str
    title: str
    transcription_provider: str = "openai"
    transcription_model: str
