from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable

from openai import AsyncOpenAI

from .events import EventHub
from .settings import OpenAISettingsStore


NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "technologies": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "explanation": {"type": "string"}}, "required": ["name", "explanation"], "additionalProperties": False}},
        "suggested_response": {"type": ["string", "null"]},
        "suggestion_confident": {"type": "boolean"}
    },
    "required": ["summary", "key_points", "decisions", "action_items", "technologies", "suggested_response", "suggestion_confident"],
    "additionalProperties": False
}


class OpenAIIntelligenceService:
    def __init__(self, settings_store: OpenAISettingsStore, hub: EventHub, transcript_reader: Callable[[str], list[dict]], local_models=None, note_writer: Callable[[str, dict], None] | None = None, mode_reader: Callable[[], str] | None = None):
        self.settings_store = settings_store
        self.hub = hub
        self.transcript_reader = transcript_reader
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_final_count: dict[str, int] = {}
        self.local_models = local_models
        self.note_writer = note_writer
        self.mode_reader = mode_reader or (lambda: "auto")
        self._local = None

    def _local_service(self):
        if self._local:
            return self._local
        status = self.local_models.status if self.local_models else None
        if not status or not status.intelligence_ready or not status.model_path or not status.runtime_path:
            return None
        from .local_provider import LocalLFMService
        from pathlib import Path
        self._local = LocalLFMService(Path(status.model_path), Path(status.runtime_path))
        return self._local

    def transcript_finalized(self, meeting_id: str) -> None:
        local = self._local_service()
        if local:
            asyncio.create_task(self._warm_local(local, meeting_id))
        current = self.tasks.get(meeting_id)
        if current and not current.done():
            return
        self.hub.publish("intelligence.status", meeting_id, {"status": "scheduled", "message": "Notes will refresh 30 seconds after new speech."})
        self.tasks[meeting_id] = asyncio.create_task(self._summarize_after_delay(meeting_id))

    async def _warm_local(self, local, meeting_id: str) -> None:
        try:
            await local.warmup()
            self.hub.publish("intelligence.status", meeting_id, {"status": "scheduled", "message": "Local LFM is warm; notes will refresh at the 30-second mark."})
        except Exception as exc:
            self.hub.publish("intelligence.status", meeting_id, {"status": "error", "message": f"Local LFM warmup failed: {exc}"})

    async def _summarize_after_delay(self, meeting_id: str) -> None:
        await asyncio.sleep(30)
        await self.summarize(meeting_id)

    def _transcript_text(self, meeting_id: str) -> tuple[str, int]:
        rows = [row for row in self.transcript_reader(meeting_id) if row.get("stability") == "final"]
        text = "\n".join(f"[{row['start_ms'] // 1000:>5}s] {row['text']}" for row in rows)
        return text, len(rows)

    async def summarize(self, meeting_id: str) -> dict | None:
        transcript, count = self._transcript_text(meeting_id)
        if not transcript or count == self.last_final_count.get(meeting_id):
            return None
        settings = self.settings_store.load()
        mode = self.mode_reader()
        local_service = self._local_service()
        if mode == "local" and not local_service:
            self.hub.publish("intelligence.status", meeting_id, {"status": "unavailable", "message": "Local mode selected, but the LFM runtime is not ready."})
            return None
        if mode == "cloud" and not settings.api_key:
            self.hub.publish("intelligence.status", meeting_id, {"status": "unavailable", "message": "Cloud mode selected, but OpenAI is not configured."})
            return None
        if mode == "auto" and not settings.api_key and not local_service:
            self.hub.publish("intelligence.status", meeting_id, {"status": "unavailable", "message": "Notes unavailable: configure OpenAI or finish preparing the local LFM runtime."})
            return None
        self.hub.publish("intelligence.status", meeting_id, {"status": "running", "message": "Generating grounded summary and action items…"})
        try:
            if settings.api_key and mode != "local":
                client = AsyncOpenAI(api_key=settings.api_key)
                response = await client.responses.create(
                    model=settings.intelligence_model,
                    input=(
                        "You are a live transcription copilot. Summarize only facts grounded in the captured transcript. "
                        "Identify decisions and action items, and briefly explain technologies that may be unfamiliar. "
                        "Only suggest a response if Vladimir or Vlad was addressed and the transcript provides enough context; "
                        "otherwise set suggested_response to null and suggestion_confident to false.\n\nTRANSCRIPT:\n" + transcript
                    ),
                    text={"format": {"type": "json_schema", "name": "meeting_notes", "strict": True, "schema": NOTES_SCHEMA}},
                )
                notes = json.loads(response.output_text)
            else:
                notes = await local_service.notes(transcript)
            self.last_final_count[meeting_id] = count
            if self.note_writer:
                self.note_writer(meeting_id, notes)
            self.hub.publish("intelligence.updated", meeting_id, notes)
            self.hub.publish("intelligence.status", meeting_id, {"status": "ready", "message": "Notes are up to date."})
            return notes
        except Exception as exc:
            local = local_service
            if mode == "auto" and settings.api_key and local:
                try:
                    notes = await local.notes(transcript)
                    self.last_final_count[meeting_id] = count
                    if self.note_writer:
                        self.note_writer(meeting_id, notes)
                    self.hub.publish("intelligence.updated", meeting_id, notes)
                    self.hub.publish("intelligence.provider", meeting_id, {"provider": "local", "reason": str(exc)})
                    self.hub.publish("intelligence.status", meeting_id, {"status": "ready", "message": "Notes generated locally after OpenAI failed."})
                    return notes
                except Exception as local_exc:
                    exc = local_exc
            self.hub.publish("intelligence.error", meeting_id, {"message": str(exc)})
            self.hub.publish("intelligence.status", meeting_id, {"status": "error", "message": f"Notes failed: {exc}"})
            return None

    async def answer(self, meeting_id: str, question: str) -> str:
        transcript, _ = self._transcript_text(meeting_id)
        if not transcript:
            raise ValueError("This meeting has no finalized transcript yet")
        settings = self.settings_store.load()
        mode = self.mode_reader()
        if settings.api_key and mode != "local":
            try:
                client = AsyncOpenAI(api_key=settings.api_key)
                response = await client.responses.create(
                    model=settings.intelligence_model,
                    input=(
                        "Answer the question using only the meeting transcript. If it is not supported by the transcript, say so. "
                        "Include the relevant timestamp in seconds.\n\nTRANSCRIPT:\n" + transcript + "\n\nQUESTION:\n" + question
                    ),
                )
                return response.output_text
            except Exception:
                if mode == "cloud":
                    raise
        local = self._local_service()
        if mode == "cloud" or not local:
            raise ValueError("Neither OpenAI nor prepared local intelligence is available")
        return await local.answer(transcript, question)

    async def close(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self._local:
            await asyncio.to_thread(self._local.close)
            self._local = None


def watched_name(text: str, names: tuple[str, ...] = ("Vladimir", "Vlad")) -> str | None:
    for name in names:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            return name
    return None
