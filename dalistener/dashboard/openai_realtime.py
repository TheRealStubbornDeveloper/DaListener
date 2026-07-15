from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
from websockets.asyncio.client import connect


TranscriptCallback = Callable[[dict], Awaitable[None]]
StatusCallback = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class Segment:
    number: int
    start_ms: int
    end_ms: int


class OpenAIRealtimeTranscriber:
    """One bounded OpenAI Realtime transcription connection per browser tab."""

    def __init__(
        self,
        api_key: str,
        model: str,
        input_sample_rate: int,
        on_transcript: TranscriptCallback,
        on_status: StatusCallback,
        connection_model: str = "gpt-realtime-2.1",
    ):
        self.api_key = api_key
        self.model = model
        self.input_sample_rate = input_sample_rate
        self.on_transcript = on_transcript
        self.on_status = on_status
        self.connection_model = connection_model
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=300)
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.started = time.monotonic()
        self.error: Exception | None = None
        self.dropped_chunks = 0
        self._segments: deque[Segment] = deque()
        self._item_segments: dict[str, Segment] = {}
        self._drafts: dict[str, str] = {}
        self._revisions: dict[str, int] = {}

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="openai-realtime-transcription")
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=20)
        except TimeoutError:
            if self.error:
                raise self.error
            raise RuntimeError("Timed out connecting to OpenAI Realtime transcription")
        if self.error:
            raise self.error

    def accept(self, pcm_float32: bytes) -> bool:
        try:
            self.queue.put_nowait(pcm_float32)
            return True
        except asyncio.QueueFull:
            self.dropped_chunks += 1
            return False

    async def close(self) -> None:
        if not self.task:
            return
        if asyncio.current_task() is self.task:
            return
        await self.queue.put(None)
        try:
            await asyncio.wait_for(self.task, timeout=8)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        except Exception:
            # The status callback already surfaced the provider error. Stop and
            # export should remain usable after a failed cloud connection.
            pass

    def _resample_pcm16(self, raw: bytes) -> bytes:
        values = np.frombuffer(raw, dtype="<f4")
        if not len(values):
            return b""
        if self.input_sample_rate != 24_000:
            output_length = max(1, round(len(values) * 24_000 / self.input_sample_rate))
            positions = np.linspace(0, len(values) - 1, output_length)
            values = np.interp(positions, np.arange(len(values)), values)
        return (np.clip(values, -1, 1) * 32767).astype("<i2").tobytes()

    def _session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {
                        "model": self.model,
                        "language": "en",
                        "delay": "low",
                    },
                }},
            },
        }

    async def _wait_for_session_ready(self, websocket) -> None:
        while True:
            event = json.loads(await asyncio.wait_for(websocket.recv(), timeout=15))
            event_type = event.get("type", "")
            if event_type == "error":
                error = event.get("error", {})
                raise RuntimeError(error.get("message", "OpenAI rejected the transcription session"))
            if event_type.endswith("session.updated"):
                return

    def _provider_error(self, exc: Exception) -> RuntimeError:
        detail = str(exc)
        normalized_detail = detail.lower()
        if "insufficient_quota" in normalized_detail or "exceeded your current quota" in normalized_detail:
            return RuntimeError(
                "OpenAI API quota is unavailable. Add API billing or credits, then try capture again."
            )
        if "invalid_model" in normalized_detail:
            return RuntimeError(
                f"OpenAI Realtime model {self.connection_model!r} is unavailable for this API project."
            )
        return RuntimeError(f"OpenAI Realtime error: {detail}")

    async def _run(self) -> None:
        url = f"wss://api.openai.com/v1/realtime?model={self.connection_model}"
        try:
            async with connect(
                url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=8 * 1024 * 1024,
                ping_interval=20,
            ) as websocket:
                await websocket.send(json.dumps(self._session_update()))
                await self._wait_for_session_ready(websocket)
                self.ready.set()
                await self.on_status("connected", "OpenAI Realtime transcription connected")
                sender = asyncio.create_task(self._send_audio(websocket))
                receiver = asyncio.create_task(self._receive_events(websocket))
                await sender
                await asyncio.sleep(3)
                await websocket.close()
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        except Exception as exc:
            self.error = self._provider_error(exc)
            self.ready.set()
            await self.on_status("error", str(self.error))

    async def _send_audio(self, websocket) -> None:
        speaking = False
        segment_start = 0
        last_voice = 0
        buffered_ms = 0
        segment_number = 0
        while True:
            raw = await self.queue.get()
            if raw is None:
                if speaking:
                    self._segments.append(Segment(segment_number, segment_start, buffered_ms))
                    await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                return
            pcm = self._resample_pcm16(raw)
            if not pcm:
                continue
            chunk_ms = len(pcm) // 2 / 24_000 * 1000
            chunk_start = buffered_ms
            buffered_ms += round(chunk_ms)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            rms = float(np.sqrt(np.mean(samples * samples)))
            if rms >= 0.012:
                if not speaking:
                    speaking = True
                    segment_start = chunk_start
                    segment_number += 1
                last_voice = buffered_ms
            await websocket.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
            should_commit = speaking and (
                buffered_ms - last_voice >= 450 or buffered_ms - segment_start >= 5_000
            )
            if should_commit:
                self._segments.append(Segment(segment_number, segment_start, last_voice))
                await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                speaking = False

    def _segment_for(self, item_id: str) -> Segment:
        segment = self._item_segments.get(item_id)
        if segment:
            return segment
        segment = self._segments.popleft() if self._segments else Segment(len(self._item_segments) + 1, 0, 0)
        self._item_segments[item_id] = segment
        return segment

    async def _receive_events(self, websocket) -> None:
        async for raw in websocket:
            event = json.loads(raw)
            event_type = event.get("type", "")
            if event_type == "error":
                error = event.get("error", {})
                await self.on_status("error", error.get("message", "OpenAI returned an error"))
                continue
            if event_type == "input_audio_buffer.committed":
                item_id = event.get("item_id")
                if item_id and self._segments:
                    self._item_segments[item_id] = self._segments.popleft()
                continue
            if event_type not in {
                "conversation.item.input_audio_transcription.delta",
                "conversation.item.input_audio_transcription.completed",
            }:
                continue
            item_id = event.get("item_id", "unknown")
            segment = self._segment_for(item_id)
            if event_type.endswith(".delta"):
                text = self._drafts.get(item_id, "") + event.get("delta", "")
                self._drafts[item_id] = text
                stability = "draft"
            else:
                text = event.get("transcript", self._drafts.get(item_id, "")).strip()
                stability = "final"
            if not text:
                continue
            revision = self._revisions.get(item_id, 0) + 1
            self._revisions[item_id] = revision
            await self.on_transcript({
                "utterance_id": item_id,
                "text": text,
                "start_ms": segment.start_ms,
                "end_ms": max(segment.end_ms, segment.start_ms + 1),
                "revision": revision,
                "stability": stability,
            })
