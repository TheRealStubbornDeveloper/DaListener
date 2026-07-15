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
    def __init__(self, settings_store: OpenAISettingsStore, hub: EventHub, transcript_reader: Callable[[str], list[dict]]):
        self.settings_store = settings_store
        self.hub = hub
        self.transcript_reader = transcript_reader
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_final_count: dict[str, int] = {}

    def transcript_finalized(self, meeting_id: str) -> None:
        current = self.tasks.get(meeting_id)
        if current and not current.done():
            return
        self.tasks[meeting_id] = asyncio.create_task(self._summarize_after_delay(meeting_id))

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
        if not settings.api_key:
            return None
        self.last_final_count[meeting_id] = count
        client = AsyncOpenAI(api_key=settings.api_key)
        try:
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
            self.hub.publish("intelligence.updated", meeting_id, notes)
            return notes
        except Exception as exc:
            self.hub.publish("intelligence.error", meeting_id, {"message": str(exc)})
            return None

    async def answer(self, meeting_id: str, question: str) -> str:
        transcript, _ = self._transcript_text(meeting_id)
        if not transcript:
            raise ValueError("This meeting has no finalized transcript yet")
        settings = self.settings_store.load()
        if not settings.api_key:
            raise ValueError("OpenAI is not configured")
        client = AsyncOpenAI(api_key=settings.api_key)
        response = await client.responses.create(
            model=settings.intelligence_model,
            input=(
                "Answer the question using only the meeting transcript. If it is not supported by the transcript, say so. "
                "Include the relevant timestamp in seconds.\n\nTRANSCRIPT:\n" + transcript + "\n\nQUESTION:\n" + question
            ),
        )
        return response.output_text


def watched_name(text: str, names: tuple[str, ...] = ("Vladimir", "Vlad")) -> str | None:
    for name in names:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            return name
    return None
