from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..models import CaptureMode, CaptureSelection, SourceKind, Stability, TranscriptEvent, TranscriptionLanguage
from ..storage import SessionStore, TranscriptExporter
from .contracts import MeetingStatus, MeetingSummary, OpenAIStatus
from .events import EventHub
from .openai_realtime import OpenAIRealtimeTranscriber
from .settings import OpenAISettingsStore
from .intelligence import OpenAIIntelligenceService, watched_name


@dataclass(slots=True)
class BrowserMeetingRuntime:
    summary: MeetingSummary
    transcriber: OpenAIRealtimeTranscriber


class BrowserMeetingManager:
    """Owns one independent OpenAI transcription session per captured tab."""

    def __init__(self, data_dir: Path, hub: EventHub, settings_store: OpenAISettingsStore):
        self.data_dir = data_dir
        self.hub = hub
        self.settings_store = settings_store
        self.store = SessionStore(data_dir / "sessions.db")
        self.exporter = TranscriptExporter(self.store)
        self.meetings: dict[str, BrowserMeetingRuntime] = {}
        self.intelligence = OpenAIIntelligenceService(settings_store, hub, self.transcript)

    def openai_status(self) -> OpenAIStatus:
        settings = self.settings_store.load()
        active = sum(m.summary.status in (MeetingStatus.PREPARING, MeetingStatus.LIVE) for m in self.meetings.values())
        configured = bool(settings.api_key)
        return OpenAIStatus(
            configured=configured,
            active_streams=active,
            transcription_model=settings.transcription_model,
            intelligence_model=settings.intelligence_model,
            status="ready" if configured else "missing-key",
            message=(
                "Each tab uses an independent OpenAI Realtime session; account rate limits apply."
                if configured else "Add an OpenAI API key before capturing a meeting."
            ),
        )

    async def start_browser_meeting(self, title: str, tab_id: int, browser: str, sample_rate: int) -> BrowserMeetingRuntime:
        settings = self.settings_store.load()
        if not settings.api_key:
            raise RuntimeError("OpenAI is not configured. Add an API key in DaListener first.")
        meeting_id = str(uuid.uuid4())
        summary = MeetingSummary(
            id=meeting_id,
            title=title or f"Browser tab {tab_id}",
            browser=browser,
            tab_id=tab_id,
            status=MeetingStatus.PREPARING,
            transcription_model=settings.transcription_model,
            started_at=datetime.now(timezone.utc),
        )
        transcriber = OpenAIRealtimeTranscriber(
            api_key=settings.api_key,
            model=settings.transcription_model,
            input_sample_rate=sample_rate,
            on_transcript=lambda payload: self._on_transcript(meeting_id, payload),
            on_status=lambda status, message: self._on_status(meeting_id, status, message),
        )
        runtime = BrowserMeetingRuntime(summary=summary, transcriber=transcriber)
        self.meetings[meeting_id] = runtime
        selection = CaptureSelection(
            mode=CaptureMode.SYSTEM,
            language=TranscriptionLanguage.ENGLISH,
            follow_default_output=False,
        )
        self.store.start_session(meeting_id, selection, settings.transcription_model)
        self.hub.publish("meeting.updated", meeting_id, summary.model_dump(mode="json"))
        self.hub.publish("openai.updated", None, self.openai_status().model_dump(mode="json"))
        try:
            await transcriber.start()
        except Exception as exc:
            self.store.abort_session(meeting_id)
            summary.status = MeetingStatus.ERROR
            summary.last_error = str(exc)
            self.hub.publish("meeting.updated", meeting_id, summary.model_dump(mode="json"))
            raise
        summary.status = MeetingStatus.LIVE
        self.hub.publish("meeting.updated", meeting_id, summary.model_dump(mode="json"))
        return runtime

    def accept_pcm(self, meeting_id: str, pcm: bytes) -> None:
        runtime = self.meetings.get(meeting_id)
        if runtime and runtime.summary.status == MeetingStatus.LIVE and len(pcm) % 4 == 0:
            runtime.transcriber.accept(pcm)

    async def _on_status(self, meeting_id: str, status: str, message: str) -> None:
        runtime = self.meetings.get(meeting_id)
        if not runtime:
            return
        if status == "error":
            runtime.summary.status = MeetingStatus.ERROR
            runtime.summary.last_error = message
            self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
        self.hub.publish("transcription.status", meeting_id, {"status": status, "message": message})

    async def _on_transcript(self, meeting_id: str, payload: dict) -> None:
        event = TranscriptEvent(
            session_id=meeting_id,
            source_id=SourceKind.SYSTEM,
            utterance_id=payload["utterance_id"],
            text=payload["text"],
            start_ms=payload["start_ms"],
            end_ms=payload["end_ms"],
            revision=payload["revision"],
            stability=Stability(payload["stability"]),
            detected_language="en",
        )
        if not self.store.save_event(event):
            return
        runtime = self.meetings.get(meeting_id)
        if runtime and event.stability == Stability.FINAL:
            runtime.summary.event_count += 1
            mentioned = watched_name(event.text)
            if mentioned:
                self.hub.publish("mention.created", meeting_id, {
                    "name": mentioned, "text": event.text, "start_ms": event.start_ms,
                })
            self.intelligence.transcript_finalized(meeting_id)
        self.hub.publish("transcript.upserted", meeting_id, {
            "utterance_id": event.utterance_id,
            "source_id": event.source_id.value,
            "text": event.text,
            "start_ms": event.start_ms,
            "end_ms": event.end_ms,
            "revision": event.revision,
            "stability": event.stability.value,
            "detected_language": event.detected_language,
        })

    async def stop(self, meeting_id: str) -> None:
        runtime = self.meetings.get(meeting_id)
        if not runtime or runtime.summary.status == MeetingStatus.ENDED:
            return
        await runtime.transcriber.close()
        self.store.end_session(meeting_id)
        runtime.summary.status = MeetingStatus.ENDED
        runtime.summary.ended_at = datetime.now(timezone.utc)
        transcript_dir = self.data_dir / "Transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        path = transcript_dir / f"DaListener-{runtime.summary.started_at:%Y-%m-%d-%H%M%S}-{meeting_id[:8]}.txt"
        self.exporter.export(meeting_id, path)
        self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
        self.hub.publish("meeting.saved", meeting_id, {"transcript_path": str(path)})
        self.hub.publish("openai.updated", None, self.openai_status().model_dump(mode="json"))

    def summaries(self) -> list[MeetingSummary]:
        return sorted((m.summary for m in self.meetings.values()), key=lambda item: item.started_at, reverse=True)

    def transcript(self, meeting_id: str) -> list[dict]:
        return [dict(row) for row in self.store.events(meeting_id)]
