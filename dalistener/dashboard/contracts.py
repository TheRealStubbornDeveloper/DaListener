from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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
    transcription_model: str = "gpt-realtime-whisper"
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
    extension_audio_url: str


class ExtensionHello(BaseModel):
    type: str = "start"
    token: str
    tab_id: int
    title: str
    url: str = ""
    browser: str = "Chromium"
    sample_rate: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=2)


class ExtensionAck(BaseModel):
    type: str = "started"
    meeting_id: str
    transcription_provider: str = "openai"
    transcription_model: str
