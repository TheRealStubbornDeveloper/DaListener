from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from openai import AsyncOpenAI

from ..models import AudioFrame, SourceKind, Stability, TranscriptionLanguage
from ..transcription import MoonshineEngine
from .intelligence import NOTES_SCHEMA


class MediaUploadService:
    def __init__(self, data_dir: Path, settings_store, local_models, mode_reader):
        self.output_dir = data_dir / "Transcripts" / "Uploads"
        self.temp_dir = data_dir / "Imports"
        self.settings_store = settings_store
        self.local_models = local_models
        self.mode_reader = mode_reader

    async def process(self, path: Path, original_name: str, watched_names: list[str], requested_provider: str) -> dict:
        mode = requested_provider if requested_provider in {"local", "cloud", "auto"} else self.mode_reader()
        names = [name.strip() for name in watched_names if name.strip()] or ["Vlad", "Vladimir"]
        provider = "local" if mode == "local" else "cloud"
        error = None
        try:
            if provider == "cloud":
                segments = await self._cloud_transcribe(path)
            else:
                segments = await asyncio.to_thread(self._local_transcribe, path)
        except Exception as exc:
            error = str(exc)
            if mode != "auto":
                raise
            provider = "local"
            segments = await asyncio.to_thread(self._local_transcribe, path)

        transcript = "\n".join(f"[{item['start_seconds']:>7.1f}s] {item['text']}" for item in segments)
        notes = await self._summarize(transcript, names, provider, mode)
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(name) for name in names) + r")(?!\w)", re.IGNORECASE)
        mentions = [item for item in segments if pattern.search(item["text"])]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(original_name).stem).strip("-.") or "upload"
        destination = self.output_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{stem}.txt"
        destination.write_text(
            f"DaListener upload: {original_name}\nProvider: {provider}\nWatched names: {', '.join(names)}\n\n{transcript}\n\nSUMMARY\n{notes.get('summary', '')}\n\nACTION ITEMS\n"
            + "\n".join(f"- {item}" for item in notes.get("action_items", [])),
            encoding="utf-8",
        )
        return {
            "file_name": original_name, "provider": provider, "fallback_reason": error,
            "watched_names": names, "segments": segments, "mentions": mentions,
            "transcript": transcript, "notes": notes, "saved_path": str(destination),
        }

    async def _cloud_transcribe(self, path: Path) -> list[dict]:
        settings = self.settings_store.load()
        if not settings.api_key:
            raise ValueError("Cloud upload transcription requires an OpenAI API key")
        client = AsyncOpenAI(api_key=settings.api_key)
        with path.open("rb") as media:
            response = await client.audio.transcriptions.create(
                model="gpt-4o-transcribe", file=media, response_format="json",
            )
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise RuntimeError("OpenAI returned an empty upload transcript")
        return [{"start_seconds": 0.0, "end_seconds": self._duration(path), "text": text}]

    def _local_transcribe(self, path: Path) -> list[dict]:
        status = self.local_models.status
        if not status.transcription_ready:
            raise ValueError("Local upload transcription is not prepared")
        import av
        finals: dict[str, dict] = {}

        def receive(event):
            if event.stability == Stability.FINAL:
                finals[event.utterance_id] = {
                    "start_seconds": round(event.start_ms / 1000, 2),
                    "end_seconds": round(event.end_ms / 1000, 2),
                    "text": event.text,
                }

        quality = "best" if status.compute_device == "cuda" else "balanced"
        engine = MoonshineEngine(self.local_models.root / "Moonshine", quality, receive, enable_finalizer=False)
        sequence = 0
        try:
            engine.start("upload-" + uuid.uuid4().hex, [SourceKind.SYSTEM], TranscriptionLanguage.ENGLISH)
            with av.open(str(path)) as container:
                streams = [stream for stream in container.streams if stream.type == "audio"]
                if not streams:
                    raise ValueError("The uploaded file has no decodable audio track")
                resampler = av.AudioResampler(format="flt", layout="mono", rate=16_000)
                for frame in container.decode(streams[0]):
                    converted = resampler.resample(frame)
                    for output in converted if isinstance(converted, list) else [converted]:
                        if output is None:
                            continue
                        samples = np.asarray(output.to_ndarray(), dtype=np.float32).reshape(-1)
                        engine.accept(AudioFrame(SourceKind.SYSTEM, sequence, int(time.monotonic() * 1000), 16_000, 1, samples))
                        sequence += 1
            engine.stop()
        finally:
            engine.close()
        rows = sorted(finals.values(), key=lambda item: item["start_seconds"])
        if not rows:
            raise RuntimeError("No speech was detected in the uploaded file")
        return rows

    async def _summarize(self, transcript: str, names: list[str], transcription_provider: str, mode: str) -> dict:
        settings = self.settings_store.load()
        use_cloud = mode == "cloud" or (mode == "auto" and transcription_provider == "cloud")
        prompt = (
            "Summarize only facts grounded in TRANSCRIPT. Identify key points, decisions, action items, and unfamiliar technologies. "
            f"The watched names are {', '.join(names)}. Suggest a response only when one of those people is directly asked something "
            "and the transcript contains enough supporting context; otherwise return null and false.\nTRANSCRIPT:\n" + transcript
        )
        if use_cloud and settings.api_key:
            client = AsyncOpenAI(api_key=settings.api_key)
            response = await client.responses.create(
                model=settings.intelligence_model, input=prompt,
                text={"format": {"type": "json_schema", "name": "upload_notes", "strict": True, "schema": NOTES_SCHEMA}},
            )
            return json.loads(response.output_text)
        status = self.local_models.status
        if not status.intelligence_ready or not status.model_path or not status.runtime_path:
            raise ValueError("Local LFM intelligence is not prepared")
        from .local_provider import LocalLFMService
        local = LocalLFMService(Path(status.model_path), Path(status.runtime_path))
        try:
            return json.loads(await asyncio.to_thread(local.complete, prompt, NOTES_SCHEMA))
        finally:
            await asyncio.to_thread(local.close)

    @staticmethod
    def _duration(path: Path) -> float:
        import av
        with av.open(str(path)) as container:
            return round(float(container.duration or 0) / 1_000_000, 2)
