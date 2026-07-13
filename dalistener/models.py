from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CaptureMode(StrEnum):
    MICROPHONE = "microphone"
    SYSTEM = "system"
    BOTH = "both"


class SourceKind(StrEnum):
    MICROPHONE = "microphone"
    SYSTEM = "system"
    STATUS = "status"


class Stability(StrEnum):
    DRAFT = "draft"
    FINAL = "final"


class QualityMode(StrEnum):
    EFFICIENT = "efficient"
    BALANCED = "balanced"
    BEST = "best"


class PerformanceRating(StrEnum):
    FAST = "fast"
    LIVEISH = "live-ish"
    DELAYED = "delayed"
    NOT_RECOMMENDED = "not-recommended"


@dataclass(slots=True)
class CaptureSelection:
    mode: CaptureMode = CaptureMode.BOTH
    microphone_id: str | None = None
    output_id: str | None = None
    follow_default_microphone: bool = True
    follow_default_output: bool = True


@dataclass(slots=True)
class AudioFrame:
    source_id: SourceKind
    sequence: int
    monotonic_ms: int
    sample_rate: int
    channels: int
    samples: Any


@dataclass(slots=True)
class TranscriptEvent:
    session_id: str
    source_id: SourceKind
    utterance_id: str
    text: str
    start_ms: int
    end_ms: int
    revision: int
    stability: Stability


@dataclass(slots=True)
class AudioDevice:
    id: str
    name: str
    kind: SourceKind
    is_default: bool = False


@dataclass(slots=True)
class CapabilityReport:
    fingerprint: str
    os_name: str
    cpu_name: str
    architecture: str
    physical_cores: int
    logical_cores: int
    total_ram_gb: float
    available_ram_gb: float
    gpu_name: str | None
    gpu_vram_gb: float | None
    providers: list[str]
    quality_mode: QualityMode
    model_name: str
    rating: PerformanceRating
    draft_delay_seconds: tuple[float, float]
    final_delay_seconds: tuple[float, float]
    estimated_memory_mb: int
    gpu_refinement: bool
    verified: bool = False
    real_time_factor: float | None = None
    downgrade_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["quality_mode"] = self.quality_mode.value
        value["rating"] = self.rating.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityReport":
        value = dict(value)
        value["quality_mode"] = QualityMode(value["quality_mode"])
        value["rating"] = PerformanceRating(value["rating"])
        value["draft_delay_seconds"] = tuple(value["draft_delay_seconds"])
        value["final_delay_seconds"] = tuple(value["final_delay_seconds"])
        return cls(**value)
