from __future__ import annotations

import asyncio
import io
import json
import os
import re
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
from openai import AsyncOpenAI

from ..models import AudioFrame, SourceKind, Stability, TranscriptionLanguage
from ..transcription import MoonshineEngine
from .intelligence import NOTES_SCHEMA


class MediaUploadService:
    def __init__(self, data_dir: Path, settings_store, local_models, mode_reader, hub):
        self.output_dir = data_dir / "Transcripts" / "Uploads"
        self.temp_dir = data_dir / "Imports"
        self.settings_store = settings_store
        self.local_models = local_models
        self.mode_reader = mode_reader
        self.hub = hub
        self.tasks: dict[str, asyncio.Task] = {}
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}

    def start(self, path: Path, original_name: str, watched_names: list[str], requested_provider: str) -> dict:
        if len(self.jobs) >= 50:
            completed = [key for key, value in self.jobs.items() if value.get("status") in {"complete", "error"}]
            for key in completed[: max(1, len(self.jobs) - 49)]:
                self.jobs.pop(key, None)
                self.tasks.pop(key, None)
        job_id = uuid.uuid4().hex
        self.cancel_events[job_id] = threading.Event()
        self.jobs[job_id] = {"job_id": job_id, "status": "queued", "file_name": original_name, "progress": 0.0, "segments": []}
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, path, original_name, watched_names, requested_provider))
        return self.jobs[job_id]

    async def _run(self, job_id: str, path: Path, original_name: str, watched_names: list[str], requested_provider: str) -> None:
        def progress(event_type: str, payload: dict) -> None:
            value = {"job_id": job_id, **payload}
            if event_type == "upload.segment":
                segment = payload["segment"]
                segments = self.jobs[job_id].setdefault("segments", [])
                segments[:] = [item for item in segments if not (item["start_seconds"] == segment["start_seconds"] and item["text"] == segment["text"])]
                segments.append(segment)
                segments.sort(key=lambda item: item["start_seconds"])
                self.jobs[job_id].update({key: item for key, item in payload.items() if key != "segment"})
            else:
                self.jobs[job_id].update(payload)
            self.hub.publish_threadsafe(event_type, None, value)

        self.jobs[job_id]["status"] = "transcribing"
        self.hub.publish("upload.started", None, dict(self.jobs[job_id]))
        try:
            result = await self.process(path, original_name, watched_names, requested_provider, progress, self.cancel_events[job_id])
            self.jobs[job_id] = {"job_id": job_id, "status": "complete", "progress": 1.0, "result": result}
            self.hub.publish("upload.completed", None, {"job_id": job_id, "result": result})
        except Exception as exc:
            self.jobs[job_id] = {"job_id": job_id, "status": "error", "error": str(exc)}
            self.hub.publish("upload.error", None, {"job_id": job_id, "message": str(exc)})
        finally:
            path.unlink(missing_ok=True)
            self.cancel_events.pop(job_id, None)

    def status(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    async def close(self) -> None:
        for event in self.cancel_events.values():
            event.set()
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def process(self, path: Path, original_name: str, watched_names: list[str], requested_provider: str, progress=None, cancel_event=None) -> dict:
        mode = requested_provider if requested_provider in {"local", "cloud", "auto"} else self.mode_reader()
        names = [name.strip() for name in watched_names if name.strip()] or ["Vlad", "Vladimir"]
        provider = "local" if mode == "local" else "cloud"
        error = None
        report = progress or (lambda _event_type, _payload: None)
        try:
            if provider == "cloud":
                report("upload.status", {"status": "transcribing", "message": "Sending media to OpenAI transcription…", "provider": "cloud"})
                segments = await self._cloud_transcribe(path, report)
            else:
                segments = await asyncio.to_thread(self._local_transcribe, path, report, cancel_event)
        except Exception as exc:
            error = str(exc)
            if mode != "auto":
                raise
            provider = "local"
            report("upload.status", {"status": "transcribing", "message": f"Cloud failed; continuing locally: {exc}", "provider": "local"})
            segments = await asyncio.to_thread(self._local_transcribe, path, report, cancel_event)

        transcript = "\n".join(f"[{item['start_seconds']:>7.1f}s] {item['text']}" for item in segments)
        report("upload.status", {"status": "summarizing", "message": "Transcript complete. Generating summary and action items…", "provider": provider, "progress": 0.9})
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

    async def _cloud_transcribe(self, path: Path, progress=None) -> list[dict]:
        settings = self.settings_store.load()
        if not settings.api_key:
            raise ValueError("Cloud upload transcription requires an OpenAI API key")
        client = AsyncOpenAI(api_key=settings.api_key)
        report = progress or (lambda _event_type, _payload: None)
        chunks = await asyncio.to_thread(self._wav_chunks, path)
        if not chunks:
            raise ValueError("The uploaded file has no decodable audio track")
        semaphore = asyncio.Semaphore(3)
        completed = 0

        async def transcribe(index: int, start: float, duration: float, content: bytes) -> dict:
            nonlocal completed
            async with semaphore:
                response = await client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=(f"chunk-{index:04d}.wav", content, "audio/wav"),
                    response_format="json",
                )
            text = str(getattr(response, "text", "") or "").strip()
            if not text:
                raise RuntimeError(f"OpenAI returned an empty transcript for chunk {index + 1}")
            segment = {"start_seconds": start, "end_seconds": round(start + duration, 2), "text": text}
            completed += 1
            report("upload.segment", {
                "status": "transcribing", "segment": segment, "provider": "cloud",
                "progress": min(0.85, completed / len(chunks) * 0.85),
            })
            return segment

        rows = await asyncio.gather(*(transcribe(index, *chunk) for index, chunk in enumerate(chunks)))
        return sorted(rows, key=lambda item: item["start_seconds"])

    @staticmethod
    def _wav_chunks(path: Path, chunk_seconds: int = 60) -> list[tuple[float, float, bytes]]:
        import av
        chunk_size = 16_000 * 2 * chunk_seconds
        pcm = bytearray()
        chunks: list[tuple[float, float, bytes]] = []
        with av.open(str(path)) as container:
            streams = [stream for stream in container.streams if stream.type == "audio"]
            if not streams:
                return []
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16_000)
            for frame in container.decode(streams[0]):
                converted = resampler.resample(frame)
                for output in converted if isinstance(converted, list) else [converted]:
                    if output is not None:
                        pcm.extend(np.asarray(output.to_ndarray(), dtype="<i2").reshape(-1).tobytes())
                while len(pcm) >= chunk_size:
                    chunks.append(MediaUploadService._wav_chunk(len(chunks) * chunk_seconds, bytes(pcm[:chunk_size])))
                    del pcm[:chunk_size]
            for output in resampler.resample(None):
                pcm.extend(np.asarray(output.to_ndarray(), dtype="<i2").reshape(-1).tobytes())
        if pcm:
            chunks.append(MediaUploadService._wav_chunk(len(chunks) * chunk_seconds, bytes(pcm)))
        return chunks

    @staticmethod
    def _wav_chunk(start_seconds: float, pcm: bytes) -> tuple[float, float, bytes]:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(pcm)
        return float(start_seconds), round(len(pcm) / 2 / 16_000, 2), buffer.getvalue()

    def _local_transcribe(self, path: Path, progress=None, cancel_event=None) -> list[dict]:
        status = self.local_models.status
        if not status.transcription_ready:
            raise ValueError("Local upload transcription is not prepared")
        report = progress or (lambda _event_type, _payload: None)
        if status.compute_device == "cuda" and os.environ.get("DALISTENER_UPLOAD_GPU", "1") != "0":
            try:
                return self._gpu_transcribe(path, report, cancel_event)
            except Exception as exc:
                report("upload.status", {
                    "status": "transcribing", "provider": "local",
                    "message": f"GPU file transcription unavailable; continuing with CPU streaming: {exc}",
                })
        import av
        finals: dict[str, dict] = {}
        duration = max(self._duration(path), 0.001)

        def receive(event):
            if event.stability == Stability.FINAL:
                segment = {
                    "start_seconds": round(event.start_ms / 1000, 2),
                    "end_seconds": round(event.end_ms / 1000, 2),
                    "text": event.text,
                }
                finals[event.utterance_id] = segment
                report("upload.segment", {
                    "status": "transcribing", "segment": segment, "provider": "local",
                    "progress": min(0.85, segment["end_seconds"] / duration * 0.85),
                })

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
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Upload transcription cancelled")
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

    def _gpu_transcribe(self, path: Path, report, cancel_event=None) -> list[dict]:
        from ..capability import _register_cuda_runtime_dirs
        _register_cuda_runtime_dirs()
        from faster_whisper import WhisperModel

        model_reference: str | Path = "large-v3-turbo"
        candidates = [self.local_models.root / "Whisper" / "large-v3-turbo"]
        if self.local_models.offline_root:
            candidates.insert(0, self.local_models.offline_root / "Whisper" / "large-v3-turbo")
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.iterdir()):
                model_reference = candidate
                break
        report("upload.status", {
            "status": "transcribing", "provider": "local", "progress": 0.01,
            "message": "Loading faster-whisper Turbo on NVIDIA CUDA…",
        })
        model = WhisperModel(str(model_reference), device="cuda", compute_type="int8_float16")
        duration = max(self._duration(path), 0.001)
        generator, _info = model.transcribe(
            str(path), language="en", beam_size=1, vad_filter=True,
            condition_on_previous_text=False,
        )
        rows = []
        for item in generator:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Upload transcription cancelled")
            text = item.text.strip()
            if not text:
                continue
            segment = {
                "start_seconds": round(item.start, 2),
                "end_seconds": round(item.end, 2),
                "text": text,
            }
            rows.append(segment)
            report("upload.segment", {
                "status": "transcribing", "segment": segment, "provider": "local",
                "compute_device": "cuda", "progress": min(0.85, item.end / duration * 0.85),
            })
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
            return self._normalize_notes(json.loads(response.output_text), transcript, names)
        status = self.local_models.status
        if not status.intelligence_ready or not status.model_path or not status.runtime_path:
            raise ValueError("Local LFM intelligence is not prepared")
        from .local_provider import LocalLFMService
        local = LocalLFMService(Path(status.model_path), Path(status.runtime_path))
        try:
            notes = json.loads(await asyncio.to_thread(local.complete, prompt, NOTES_SCHEMA))
            summary_text = str(notes.get("summary") or "").strip().lower() if isinstance(notes, dict) else ""
            action_items = notes.get("action_items") if isinstance(notes, dict) else None
            retry_needed = (
                not summary_text
                or bool(re.fullmatch(r"(?:null|none)(?:\W+(?:and\W+)?false)?\W*", summary_text))
                or "no direct question" in summary_text
                or "no response is returned" in summary_text
                or not isinstance(action_items, list)
                or not action_items
            )
            if retry_needed:
                retry_prompt = (
                    "Return ONLY valid compact JSON with keys summary and action_items. summary must be 3-5 sentences "
                    "about the technical discussion, not about watched names or response eligibility. action_items must be "
                    "a JSON array of concrete work people agreed or were asked to do. Use only the transcript. Correct "
                    "obvious ASR term errors when context is clear, such as upset meaning upsert.\nTRANSCRIPT:\n" + transcript
                )
                retry_text = await asyncio.to_thread(local.complete, retry_prompt)
                retry = self._json_object(retry_text)
                if retry:
                    notes["summary"] = retry.get("summary") or notes.get("summary")
                    if isinstance(retry.get("action_items"), list) and retry["action_items"]:
                        notes["action_items"] = retry["action_items"]
            return self._normalize_notes(notes, transcript, names)
        finally:
            await asyncio.to_thread(local.close)

    @staticmethod
    def _json_object(text: str) -> dict | None:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _normalize_notes(notes: dict, transcript: str, names: list[str] | None = None) -> dict:
        """Keep structured output useful when a local model emits null-like fields."""
        value = notes if isinstance(notes, dict) else {}
        transcript_rows = []
        for line in transcript.splitlines():
            match = re.match(r"\[\s*([\d.]+)s\]\s*(.+)", line)
            if match and match.group(2).strip():
                transcript_rows.append((float(match.group(1)), match.group(2).strip()))
        priority = re.compile(r"\b(?:need to|have to|has to|should|must|will|issue|problem|delta|duplicate|merge|upsert|dms|dq|test|update|load)\b", re.IGNORECASE)
        ranked = sorted(
            transcript_rows,
            key=lambda row: (len(priority.findall(row[1])), min(len(row[1]), 180)),
            reverse=True,
        )
        relevant = []
        for timestamp, text in ranked:
            normalized = re.sub(r"\W+", " ", text.lower()).strip()
            if normalized and not any(normalized in existing[2] or existing[2] in normalized for existing in relevant):
                relevant.append((timestamp, text, normalized))
            if len(relevant) == 5:
                break
        relevant.sort(key=lambda row: row[0])
        summary = value.get("summary")
        summary_text = summary.strip().lower() if isinstance(summary, str) else ""
        invalid_summary = (
            not summary_text
            or bool(re.fullmatch(r"(?:null|none)(?:\W+(?:and\W+)?false)?\W*", summary_text))
            or "no direct question" in summary_text
            or "no response is returned" in summary_text
            or "no suggested response" in summary_text
        )
        if invalid_summary:
            excerpts = [item[1] for item in relevant]
            if not excerpts:
                excerpts = [text for _timestamp, text in transcript_rows[:3]]
            summary = " ".join(excerpts)[:800] or "No spoken content was available to summarize."
        arrays = {}
        for field in ("key_points", "decisions", "action_items"):
            items = value.get(field)
            arrays[field] = [str(item) for item in items if str(item).strip()] if isinstance(items, list) else []
        if not arrays["key_points"]:
            arrays["key_points"] = [f"[{timestamp:.0f}s] {text}" for timestamp, text, _normalized in relevant]
        if not arrays["action_items"]:
            action_pattern = re.compile(r"\b(?:need to|have to|has to|should|must|please|can you|run|test|update|make sure|verify)\b", re.IGNORECASE)
            actions = [(timestamp, text) for timestamp, text in transcript_rows if action_pattern.search(text)]
            arrays["action_items"] = [f"[{timestamp:.0f}s] {text}" for timestamp, text in actions[:8]]
        technologies = value.get("technologies")
        technologies = [item for item in technologies if isinstance(item, dict) and item.get("name") and item.get("explanation")] if isinstance(technologies, list) else []
        technologies = [item for item in technologies if str(item["name"]).strip().lower() not in {"unfamiliar technologies", "none", "null"}]
        response = value.get("suggested_response")
        response_text = response.strip().lower() if isinstance(response, str) else ""
        response_valid = bool(response_text) and not bool(re.fullmatch(r"(?:null|none)(?:\W+(?:and\W+)?false)?\W*", response_text))
        watched = [name for name in (names or ["Vlad", "Vladimir"]) if name]
        mentioned = bool(watched) and bool(re.search(r"(?<!\w)(?:" + "|".join(re.escape(name) for name in watched) + r")(?!\w)", transcript, re.IGNORECASE))
        confident = value.get("suggestion_confident") is True and response_valid and mentioned
        return {
            "summary": summary.strip(), **arrays, "technologies": technologies,
            "suggested_response": response if confident else None,
            "suggestion_confident": confident,
        }

    @staticmethod
    def _duration(path: Path) -> float:
        import av
        with av.open(str(path)) as container:
            return round(float(container.duration or 0) / 1_000_000, 2)
