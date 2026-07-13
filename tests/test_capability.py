from pathlib import Path

from dalistener.capability import CapabilityService
from dalistener.models import CapabilityReport, PerformanceRating, QualityMode


def report() -> CapabilityReport:
    return CapabilityReport(
        fingerprint="abc", os_name="Windows", cpu_name="CPU", architecture="AMD64",
        physical_cores=8, logical_cores=16, total_ram_gb=16, available_ram_gb=8,
        gpu_name=None, gpu_vram_gb=None, providers=["CPUExecutionProvider"],
        quality_mode=QualityMode.BALANCED, model_name="Moonshine Medium Streaming",
        rating=PerformanceRating.LIVEISH, draft_delay_seconds=(0.5, 2.0),
        final_delay_seconds=(1.0, 2.5), estimated_memory_mb=1100, gpu_refinement=False,
    )


def test_capability_round_trip():
    restored = CapabilityReport.from_dict(report().to_dict())
    assert restored == report()


def test_calibration_classifies_and_caches(tmp_path: Path, monkeypatch):
    service = CapabilityService(tmp_path / "capability.json")
    times = iter([10.0, 11.0])
    monkeypatch.setattr("dalistener.capability.time.perf_counter", lambda: next(times))
    verified = service.verify(report(), lambda: 5.0)
    assert verified.verified
    assert verified.real_time_factor == 0.2
    assert verified.rating == PerformanceRating.FAST
    assert service.load_cached("abc") == verified
