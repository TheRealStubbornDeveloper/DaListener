from __future__ import annotations

import asyncio
import json
import queue
import secrets
import subprocess
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

import numpy as np

from ..models import AudioFrame, SourceKind, TranscriptEvent, TranscriptionLanguage
from ..transcription import MoonshineEngine
from .intelligence import NOTES_SCHEMA


TranscriptCallback = Callable[[dict], Awaitable[None]]
StatusCallback = Callable[[str, str], Awaitable[None]]


class LocalStreamingTranscriber:
    """English-only local transcription adapter with the same shape as the cloud provider."""

    def __init__(self, meeting_id: str, model_dir: Path, quality: str, sample_rate: int, on_transcript: TranscriptCallback, on_status: StatusCallback):
        self.meeting_id = meeting_id
        self.sample_rate = sample_rate
        self.on_transcript = on_transcript
        self.on_status = on_status
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sequence = 0
        self.engine = MoonshineEngine(model_dir, quality, self._event)
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=max(8, round(15 * sample_rate / 4096)))
        self.worker: threading.Thread | None = None
        self.last_lag_report = 0.0

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.engine.prepare)
        await asyncio.to_thread(self.engine.start, self.meeting_id, [SourceKind.SYSTEM], TranscriptionLanguage.ENGLISH)
        self.worker = threading.Thread(target=self._audio_worker, name=f"local-stt-{self.meeting_id[:8]}", daemon=True)
        self.worker.start()
        await self.on_status("connected", "Local English transcription connected")

    def _audio_worker(self) -> None:
        while True:
            pcm_float32 = self.audio_queue.get()
            if pcm_float32 is None:
                return
            samples = np.frombuffer(pcm_float32, dtype="<f4").copy()
            self.engine.accept(AudioFrame(SourceKind.SYSTEM, self.sequence, 0, self.sample_rate, 1, samples))
            self.sequence += 1

    def _event(self, event: TranscriptEvent) -> None:
        if not self.loop or self.loop.is_closed():
            return
        payload = {
            "utterance_id": f"local-{event.utterance_id}", "text": event.text,
            "start_ms": event.start_ms, "end_ms": event.end_ms,
            "revision": event.revision, "stability": event.stability.value,
        }
        asyncio.run_coroutine_threadsafe(self.on_transcript(payload), self.loop)

    def accept(self, pcm_float32: bytes) -> bool:
        if len(pcm_float32) % 4:
            return False
        try:
            self.audio_queue.put_nowait(bytes(pcm_float32))
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait(bytes(pcm_float32))
            except (queue.Empty, queue.Full):
                return False
        lag = self.audio_queue.qsize() * 4096 / self.sample_rate
        now = time.monotonic()
        if self.loop and now - self.last_lag_report >= 1.0:
            self.last_lag_report = now
            asyncio.run_coroutine_threadsafe(self.on_status("lag", f"{lag:.2f}"), self.loop)
        return True

    async def close(self) -> None:
        try:
            self.audio_queue.put_nowait(None)
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        if self.worker:
            await asyncio.to_thread(self.worker.join, 5.0)
        await asyncio.to_thread(self.engine.close)


class LocalLFMService:
    """Starts a loopback llama.cpp server and keeps every answer transcript-grounded."""

    def __init__(self, model_path: Path, runtime_path: Path):
        self.model_path = model_path
        self.runtime_path = runtime_path
        self.port = 58941
        self.process: subprocess.Popen | None = None
        self.log_file = None
        self.api_key = secrets.token_urlsafe(32)
        self.warmed = False
        self.warm_lock = asyncio.Lock()
        self.lock = threading.Lock()

    def _ensure_server(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            arguments = [
                str(self.runtime_path), "--model", str(self.model_path), "--host", "127.0.0.1",
                "--port", str(self.port), "--ctx-size", "32768", "--temp", "0.2", "--top-k", "80",
                "--repeat-penalty", "1.05", "--parallel", "1", "--cont-batching", "--jinja",
                "--api-key", self.api_key,
            ]
            if "cuda" in str(self.runtime_path).lower():
                arguments.extend(["--n-gpu-layers", "99"])
            log_path = self.model_path.parent / "llama-server.log"
            self.log_file = log_path.open("a", encoding="utf-8", buffering=1)
            self.process = subprocess.Popen(
                arguments, stdout=self.log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=self.runtime_path.parent,
            )

    def complete(self, prompt: str, json_schema: dict | None = None) -> str:
        self._ensure_server()
        body = {
            "model": "lfm-local", "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2, "top_k": 80, "repeat_penalty": 1.05,
        }
        if json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "meeting_notes", "strict": True, "schema": json_schema}}
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST",
        )
        last_error = None
        for _ in range(240):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.loads(response.read().decode())
                return payload["choices"][0]["message"]["content"]
            except Exception as exc:
                last_error = exc
                if self.process and self.process.poll() is not None:
                    log_path = self.model_path.parent / "llama-server.log"
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else ""
                    raise RuntimeError(f"Local LFM server exited with code {self.process.returncode}.\n{tail}") from exc
                time.sleep(0.25)
        raise RuntimeError(f"Local LFM server did not become ready: {last_error}")

    async def warmup(self) -> None:
        if self.warmed:
            return
        async with self.warm_lock:
            if self.warmed:
                return
            await asyncio.to_thread(self.complete, "Reply with only: OK")
            self.warmed = True

    def close(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None
            if self.log_file:
                self.log_file.close()
                self.log_file = None

    async def notes(self, transcript: str) -> dict:
        prompt = (
            "Return JSON matching the requested schema. Use only facts in TRANSCRIPT. Identify key points, decisions, "
            "action items, and briefly explain unfamiliar technologies. Suggest a response only if Vladimir or Vlad "
            "was directly asked something and the transcript contains enough facts; otherwise use null and false.\nTRANSCRIPT:\n" + transcript
        )
        return json.loads(await asyncio.to_thread(self.complete, prompt, NOTES_SCHEMA))

    async def answer(self, transcript: str, question: str) -> str:
        prompt = "Answer only from TRANSCRIPT and cite a [seconds] timestamp. Say when unsupported.\nTRANSCRIPT:\n" + transcript + "\nQUESTION:\n" + question
        return await asyncio.to_thread(self.complete, prompt)
