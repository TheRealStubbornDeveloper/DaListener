from __future__ import annotations

import asyncio
import difflib
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..capability import CapabilityService
from ..models import CaptureMode, CaptureSelection, SourceKind, Stability, TranscriptEvent, TranscriptionLanguage
from ..storage import SessionStore, TranscriptExporter
from .contracts import MeetingStatus, MeetingSummary, OpenAIStatus
from .events import EventHub
from .openai_realtime import OpenAIRealtimeTranscriber
from .local_provider import LocalStreamingTranscriber
from .local_models import LocalModelService
from .pricing import PricingCatalogService
from .usage import UsageLedger
from .settings import OpenAISettingsStore
from .intelligence import OpenAIIntelligenceService, watched_name
from .sources import SourceClassification, classify_source
from .provider_mode import ProviderModeStore


@dataclass(slots=True)
class BrowserMeetingRuntime:
    summary: MeetingSummary
    transcriber: object
    sample_rate: int
    rolling_audio: deque[bytes]
    pending_usage_seconds: float = 0.0
    switching: bool = False
    dedupe_until: float = 0.0
    provider_mode: str = "auto"


class BrowserMeetingManager:
    """Owns one independent OpenAI transcription session per captured tab."""

    def __init__(self, data_dir: Path, hub: EventHub, settings_store: OpenAISettingsStore, local_models: LocalModelService | None = None, provider_mode_store: ProviderModeStore | None = None):
        self.data_dir = data_dir
        self.hub = hub
        self.settings_store = settings_store
        self.store = SessionStore(data_dir / "sessions.db")
        self.exporter = TranscriptExporter(self.store)
        self.meetings: dict[str, BrowserMeetingRuntime] = {}
        self.pricing = PricingCatalogService(data_dir / "pricing.json")
        self.usage = UsageLedger(data_dir / "sessions.db")
        self.local_models = local_models or LocalModelService(data_dir, hub.publish)
        self.provider_mode_store = provider_mode_store or ProviderModeStore(data_dir / "provider-mode.json")
        self.intelligence = OpenAIIntelligenceService(
            settings_store, hub, self.transcript, self.local_models, self.store.save_notes,
            self.provider_mode_store.load,
        )

    def openai_status(self) -> OpenAIStatus:
        settings = self.settings_store.load()
        provider_mode = self.provider_mode_store.load()
        active = sum(
            m.summary.status in (MeetingStatus.PREPARING, MeetingStatus.LIVE)
            and m.summary.transcription_provider == "openai"
            for m in self.meetings.values()
        )
        configured = bool(settings.api_key)
        return OpenAIStatus(
            configured=configured,
            active_streams=active,
            transcription_model=settings.transcription_model,
            intelligence_model=settings.intelligence_model,
            status="ready" if configured else "missing-key",
            message=(
                f"Provider mode: {provider_mode}. Each cloud tab uses an independent OpenAI Realtime session."
                if configured else "Add an OpenAI API key before capturing a meeting."
            ),
        )

    async def start_browser_meeting(
        self, title: str, url: str, tab_id: int | None, browser: str, sample_rate: int,
        source_override: SourceClassification | None = None,
    ) -> BrowserMeetingRuntime:
        settings = self.settings_store.load()
        provider_mode = self.provider_mode_store.load()
        meeting_id = str(uuid.uuid4())
        source = source_override or classify_source(url)
        if not source.supported:
            raise RuntimeError("This browser page cannot be captured")
        display_title = self._unique_title(title or f"Browser tab {tab_id}")
        summary = MeetingSummary(
            id=meeting_id,
            title=display_title,
            browser=browser,
            tab_id=tab_id,
            status=MeetingStatus.PREPARING,
            transcription_model=settings.transcription_model,
            capture_category=source.category,
            site_domain=source.domain,
            service_label=source.service_label,
            started_at=datetime.now(timezone.utc),
        )
        transcriber = self._cloud_transcriber(meeting_id, sample_rate, settings) if settings.api_key and provider_mode != "local" else None
        runtime = BrowserMeetingRuntime(
            summary=summary, transcriber=transcriber, sample_rate=sample_rate,
            rolling_audio=deque(maxlen=max(10, round(15 * sample_rate / 4096))),
            provider_mode=provider_mode,
        )
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
            if provider_mode == "local":
                raise RuntimeError("Local mode selected")
            if not transcriber:
                raise RuntimeError("OpenAI is not configured")
            await transcriber.start()
        except Exception as exc:
            if provider_mode not in {"local", "auto"} or not await self._activate_local(meeting_id, str(exc), close_previous=False):
                self.store.abort_session(meeting_id)
                summary.status = MeetingStatus.ERROR
                summary.last_error = (
                    f"{exc}. Cloud mode selected; local fallback was not attempted."
                    if provider_mode == "cloud" else f"{exc}. Local fallback not prepared."
                )
                self.hub.publish("meeting.updated", meeting_id, summary.model_dump(mode="json"))
                raise RuntimeError(summary.last_error) from exc
        summary.status = MeetingStatus.LIVE
        self.hub.publish("meeting.updated", meeting_id, summary.model_dump(mode="json"))
        return runtime

    def _unique_title(self, requested: str) -> str:
        base = " ".join(requested.split()).strip() or "Shared browser tab"
        existing = {runtime.summary.title.casefold() for runtime in self.meetings.values()}
        if base.casefold() not in existing:
            return base
        while True:
            candidate = f"{base} · {secrets.token_hex(2)}"
            if candidate.casefold() not in existing:
                return candidate

    def _cloud_transcriber(self, meeting_id: str, sample_rate: int, settings):
        return OpenAIRealtimeTranscriber(
            api_key=settings.api_key, model=settings.transcription_model, input_sample_rate=sample_rate,
            on_transcript=lambda payload: self._on_transcript(meeting_id, payload),
            on_status=lambda status, message: self._on_status(meeting_id, status, message),
            connection_model=settings.realtime_connection_model,
        )

    def accept_pcm(self, meeting_id: str, pcm: bytes) -> None:
        runtime = self.meetings.get(meeting_id)
        if runtime and runtime.summary.status == MeetingStatus.LIVE and len(pcm) % 4 == 0:
            runtime.rolling_audio.append(pcm)
            accepted = runtime.transcriber.accept(pcm)
            if accepted and runtime.summary.transcription_provider == "openai":
                seconds = len(pcm) / 4 / runtime.sample_rate
                runtime.pending_usage_seconds += seconds
                runtime.summary.openai_audio_seconds += seconds
                rate = self.pricing.snapshot(refresh=False).rate_per_minute_usd
                runtime.summary.estimated_cost_usd = round(runtime.summary.openai_audio_seconds / 60 * rate, 6)
                if runtime.pending_usage_seconds >= 5:
                    self._flush_usage(runtime)

    def _flush_usage(self, runtime: BrowserMeetingRuntime) -> None:
        if runtime.pending_usage_seconds <= 0:
            return
        self.usage.record(
            runtime.summary.id, runtime.summary.tab_id, "openai", runtime.summary.transcription_model,
            runtime.pending_usage_seconds,
        )
        runtime.pending_usage_seconds = 0.0

    async def _on_status(self, meeting_id: str, status: str, message: str) -> None:
        runtime = self.meetings.get(meeting_id)
        if not runtime:
            return
        if status == "error" and runtime.summary.status == MeetingStatus.LIVE and runtime.summary.transcription_provider == "openai":
            runtime.summary.last_error = message
            self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
            if runtime.provider_mode == "auto":
                asyncio.create_task(self._fallback_after_retry(meeting_id, message))
        elif status == "lag":
            try:
                runtime.summary.measured_transcription_lag_seconds = round(float(message), 2)
                self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
            except ValueError:
                pass
        self.hub.publish("transcription.status", meeting_id, {"status": status, "message": message})

    async def _fallback_after_retry(self, meeting_id: str, reason: str) -> None:
        await asyncio.sleep(5)
        runtime = self.meetings.get(meeting_id)
        if runtime and runtime.summary.status == MeetingStatus.LIVE and runtime.summary.transcription_provider == "openai":
            await self._activate_local(meeting_id, reason)

    async def _activate_local(self, meeting_id: str, reason: str, close_previous: bool = True) -> bool:
        runtime = self.meetings.get(meeting_id)
        local = self.local_models.status
        if not runtime or runtime.switching or not local.transcription_ready:
            return False
        runtime.switching = True
        previous = runtime.transcriber
        try:
            if close_previous and previous:
                await previous.close()
            report = CapabilityService(self.local_models.root / "capability.json").inspect()
            transcriber = LocalStreamingTranscriber(
                meeting_id, self.local_models.root / "Moonshine", report.quality_mode.value,
                runtime.sample_rate, lambda payload: self._on_transcript(meeting_id, payload),
                lambda status, message: self._on_status(meeting_id, status, message),
            )
            await transcriber.start()
            runtime.transcriber = transcriber
            runtime.summary.transcription_provider = "local"
            runtime.summary.transcription_model = report.model_name
            runtime.summary.compute_device = "cuda" if report.gpu_refinement else "cpu"
            runtime.summary.provider_reason = reason
            runtime.summary.fallback_active = True
            runtime.summary.last_error = None
            runtime.summary.transcription_delay_seconds = list(report.final_delay_seconds)
            runtime.dedupe_until = time.monotonic() + 20
            for buffered in runtime.rolling_audio:
                transcriber.accept(buffered)
            self._flush_usage(runtime)
            self._record_status_event(meeting_id, f"Provider switched to Local: {reason}")
            self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
            return True
        except Exception as exc:
            runtime.summary.status = MeetingStatus.ERROR
            runtime.summary.last_error = f"Local fallback failed: {exc}"
            self.hub.publish("meeting.updated", meeting_id, runtime.summary.model_dump(mode="json"))
            return False
        finally:
            runtime.switching = False

    def _record_status_event(self, meeting_id: str, text: str) -> None:
        event = TranscriptEvent(
            session_id=meeting_id, source_id=SourceKind.STATUS, utterance_id=f"status-{uuid.uuid4()}",
            text=text, start_ms=0, end_ms=1, revision=1, stability=Stability.FINAL, detected_language="en",
        )
        self.store.save_event(event)
        self.hub.publish("transcript.upserted", meeting_id, {
            "utterance_id": event.utterance_id, "source_id": "status", "text": text,
            "start_ms": 0, "end_ms": 1, "revision": 1, "stability": "final", "detected_language": "en",
        })

    async def _on_transcript(self, meeting_id: str, payload: dict) -> None:
        runtime = self.meetings.get(meeting_id)
        if (
            runtime and runtime.summary.transcription_provider == "local"
            and payload.get("stability") == "final" and time.monotonic() < runtime.dedupe_until
            and self._duplicates_recent_final(meeting_id, str(payload.get("text", "")))
        ):
            return
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

    def _duplicates_recent_final(self, meeting_id: str, text: str) -> bool:
        normalized = re.sub(r"\W+", " ", text.lower()).strip()
        if not normalized:
            return True
        recent = [dict(row) for row in self.store.events(meeting_id, finals_only=True)[-20:]]
        for row in recent:
            candidate = re.sub(r"\W+", " ", str(row["text"]).lower()).strip()
            if candidate and difflib.SequenceMatcher(None, normalized, candidate).ratio() >= 0.92:
                return True
        return False

    async def stop(self, meeting_id: str) -> None:
        runtime = self.meetings.get(meeting_id)
        if not runtime or runtime.summary.status == MeetingStatus.ENDED:
            return
        await runtime.transcriber.close()
        self._flush_usage(runtime)
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
