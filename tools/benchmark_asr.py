"""Reproducible CPU/GPU ASR benchmark for DaListener development."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import time
from pathlib import Path

import moonshine_voice
import numpy as np

from dalistener.capability import _cuda_runtime_ready, _register_cuda_runtime_dirs


def load_sample(seconds: float) -> tuple[np.ndarray, int]:
    sample_path = Path(moonshine_voice.__file__).parent / "assets" / "two_cities.wav"
    samples, sample_rate = moonshine_voice.load_wav_file(sample_path)
    audio = np.asarray(samples[: int(sample_rate * seconds)], dtype=np.float32)
    if sample_rate != 16_000:
        output_length = round(len(audio) * 16_000 / sample_rate)
        source_positions = np.arange(len(audio), dtype=np.float64)
        target_positions = np.linspace(0, len(audio) - 1, output_length)
        audio = np.interp(target_positions, source_positions, audio).astype(np.float32)
        sample_rate = 16_000
    return audio, sample_rate


def benchmark(device: str, audio: np.ndarray, rounds: int) -> dict:
    from faster_whisper import WhisperModel

    compute_type = "int8" if device == "cpu" else "int8_float16"
    started = time.perf_counter()
    model = WhisperModel(
        "large-v3-turbo",
        device=device,
        compute_type=compute_type,
        cpu_threads=8,
    )
    load_seconds = time.perf_counter() - started

    timings: list[float] = []
    transcript = ""
    for iteration in range(rounds + 1):
        started = time.perf_counter()
        segments, _ = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        elapsed = time.perf_counter() - started
        if iteration > 0:
            timings.append(elapsed)

    median = statistics.median(timings[-rounds:])
    result = {
        "device": device,
        "compute_type": compute_type,
        "model_load_seconds": round(load_seconds, 3),
        "median_inference_seconds": round(median, 3),
        "real_time_factor": round(median / (len(audio) / 16_000), 4),
        "realtime_speedup": round((len(audio) / 16_000) / median, 2),
        "round_seconds": [round(value, 3) for value in timings[-rounds:]],
        "transcript": transcript,
    }
    del model
    gc.collect()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    audio, sample_rate = load_sample(args.seconds)
    if sample_rate != 16_000:
        raise RuntimeError(f"Expected 16 kHz benchmark audio, received {sample_rate} Hz")

    _register_cuda_runtime_dirs()
    cpu = benchmark("cpu", audio, args.rounds)
    gpu = benchmark("cuda", audio, args.rounds) if _cuda_runtime_ready() else None
    result = {
        "system": {
            "os": platform.platform(),
            "processor": platform.processor(),
            "audio_seconds": round(len(audio) / sample_rate, 3),
            "model": "faster-whisper large-v3-turbo",
            "rounds": args.rounds,
            "warmup_rounds": 1,
        },
        "cpu": cpu,
        "gpu": gpu,
        "gpu_vs_cpu_inference_speedup": (
            round(cpu["median_inference_seconds"] / gpu["median_inference_seconds"], 2)
            if gpu else None
        ),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
