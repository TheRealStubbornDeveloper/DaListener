# DaListener performance notes

## RTX 3090 versus Ryzen 7 5700X

Measured on July 14, 2026 using the same 20-second, 16 kHz English speech clip and the same `faster-whisper large-v3-turbo` model. Each path received one unmeasured warm-up followed by three measured runs. The median is reported.

| Path | Compute | Median | Real-time factor | Processing rate |
|---|---:|---:|---:|---:|
| Ryzen 7 5700X | INT8 CPU | 6.176 s | 0.3088 | 3.24× real time |
| RTX 3090 | INT8/FP16 CUDA | 0.288 s | 0.0144 | 69.43× real time |

For this utterance, the RTX 3090 completed inference **21.44× faster** than the CPU path. Both paths produced the same transcript. Model initialization took 6.054 seconds on CPU and 3.851 seconds on GPU and is excluded from the inference comparison because DaListener keeps a loaded model alive during a session.

This is a same-model throughput comparison, not an accuracy comparison. DaListener's normal architecture uses the smaller Moonshine Medium Streaming model for low-latency CPU drafts and reserves Whisper Turbo for optional phrase finalization. A separate five-second dual-lane Moonshine calibration completed in approximately 0.51 seconds on this machine.

Raw results: [`benchmarks/2026-07-14-rtx3090-vs-ryzen5700x.json`](benchmarks/2026-07-14-rtx3090-vs-ryzen5700x.json)

## Reproduce

From an installed development environment:

```powershell
.venv\Scripts\Activate.ps1
python tools\benchmark_asr.py --seconds 20 --rounds 3
```

Results depend on driver versions, background GPU use, CPU power policy, thermals, and model/runtime versions. Close GPU-heavy programs and run several times when comparing machines.
