from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

import numpy as np
import soundcard as sc

from .models import AudioDevice, AudioFrame, CaptureMode, CaptureSelection, SourceKind


FrameCallback = Callable[[AudioFrame], None]
StatusCallback = Callable[[SourceKind, str], None]


class AudioDeviceService:
    def list_microphones(self) -> list[AudioDevice]:
        default = sc.default_microphone()
        default_id = str(default.id) if default else None
        devices = []
        for device in sc.all_microphones(include_loopback=False):
            devices.append(AudioDevice(
                id=str(device.id), name=device.name, kind=SourceKind.MICROPHONE,
                is_default=str(device.id) == default_id,
            ))
        return devices

    def list_outputs(self) -> list[AudioDevice]:
        default = sc.default_speaker()
        default_id = str(default.id) if default else None
        return [
            AudioDevice(
                id=str(device.id), name=device.name, kind=SourceKind.SYSTEM,
                is_default=str(device.id) == default_id,
            )
            for device in sc.all_speakers()
        ]

    def resolve_microphone(self, device_id: str | None, follow_default: bool):
        if follow_default or not device_id:
            return sc.default_microphone()
        return sc.get_microphone(device_id, include_loopback=False)

    def resolve_output_loopback(self, device_id: str | None, follow_default: bool):
        speaker = sc.default_speaker() if follow_default or not device_id else sc.get_speaker(device_id)
        if not speaker:
            raise RuntimeError("No output device is available")
        try:
            return sc.get_microphone(str(speaker.id), include_loopback=True)
        except Exception:
            loopbacks = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]
            match = next((m for m in loopbacks if speaker.name.lower() in m.name.lower()), None)
            if not match:
                raise RuntimeError(f"WASAPI loopback is unavailable for {speaker.name}")
            return match


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        source: SourceKind,
        device,
        callback: FrameCallback,
        status_callback: StatusCallback,
        sample_rate: int = 48_000,
        chunk_ms: int = 200,
    ):
        super().__init__(name=f"capture-{source.value}", daemon=True)
        self.source = source
        self.device = device
        self.callback = callback
        self.status_callback = status_callback
        self.sample_rate = sample_rate
        self.frames_per_chunk = sample_rate * chunk_ms // 1000
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.sequence = 0

    def run(self) -> None:
        self.status_callback(self.source, f"Connected: {self.device.name}")
        try:
            with self.device.recorder(samplerate=self.sample_rate) as recorder:
                while not self.stop_event.is_set():
                    samples = recorder.record(numframes=self.frames_per_chunk)
                    if self.pause_event.is_set():
                        continue
                    mono = np.asarray(samples, dtype=np.float32)
                    if mono.ndim > 1:
                        mono = mono.mean(axis=1)
                    self.sequence += 1
                    self.callback(AudioFrame(
                        source_id=self.source,
                        sequence=self.sequence,
                        monotonic_ms=time.monotonic_ns() // 1_000_000,
                        sample_rate=self.sample_rate,
                        channels=1,
                        samples=np.ascontiguousarray(mono, dtype=np.float32),
                    ))
        except Exception as exc:
            self.status_callback(self.source, f"Audio source stopped: {exc}")

    def stop(self) -> None:
        self.stop_event.set()

    def set_paused(self, paused: bool) -> None:
        self.pause_event.set() if paused else self.pause_event.clear()


class CaptureManager:
    def __init__(self, devices: AudioDeviceService | None = None, max_queue: int = 50):
        self.devices = devices or AudioDeviceService()
        self.queue: queue.Queue[AudioFrame] = queue.Queue(maxsize=max_queue)
        self.workers: dict[SourceKind, CaptureWorker] = {}
        self.dispatcher: threading.Thread | None = None
        self.dispatch_stop = threading.Event()
        self.dropped_frames = 0

    def start(
        self,
        selection: CaptureSelection,
        frame_callback: FrameCallback,
        status_callback: StatusCallback,
    ) -> None:
        self.stop()
        self.dispatch_stop.clear()
        self.dropped_frames = 0
        workers: dict[SourceKind, CaptureWorker] = {}

        def enqueue(frame: AudioFrame) -> None:
            try:
                self.queue.put_nowait(frame)
            except queue.Full:
                self.dropped_frames += 1
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(frame)
                except queue.Empty:
                    pass
                status_callback(frame.source_id, "Transcription is falling behind; oldest audio was dropped")

        if selection.mode in (CaptureMode.MICROPHONE, CaptureMode.BOTH):
            mic = self.devices.resolve_microphone(selection.microphone_id, selection.follow_default_microphone)
            if not mic:
                raise RuntimeError("No microphone is available")
            workers[SourceKind.MICROPHONE] = CaptureWorker(
                SourceKind.MICROPHONE, mic, enqueue, status_callback,
            )
        if selection.mode in (CaptureMode.SYSTEM, CaptureMode.BOTH):
            loopback = self.devices.resolve_output_loopback(selection.output_id, selection.follow_default_output)
            workers[SourceKind.SYSTEM] = CaptureWorker(
                SourceKind.SYSTEM, loopback, enqueue, status_callback,
            )

        def dispatch() -> None:
            while not self.dispatch_stop.is_set():
                try:
                    frame_callback(self.queue.get(timeout=0.2))
                except queue.Empty:
                    continue
                except Exception as exc:
                    status_callback(SourceKind.STATUS, f"Transcription pipeline stopped: {exc}")
                    self.dispatch_stop.set()
                    return

        self.workers = workers
        self.dispatcher = threading.Thread(target=dispatch, name="audio-dispatch", daemon=True)
        try:
            self.dispatcher.start()
            for worker in self.workers.values():
                worker.start()
        except Exception:
            self.stop()
            raise

    def pause(self, paused: bool) -> None:
        for worker in self.workers.values():
            worker.set_paused(paused)

    def stop(self) -> None:
        self.dispatch_stop.set()
        for worker in self.workers.values():
            worker.stop()
        for worker in self.workers.values():
            if worker.ident is not None:
                worker.join(timeout=1.5)
        self.workers.clear()
        if self.dispatcher:
            self.dispatcher.join(timeout=1.0)
        self.dispatcher = None
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
