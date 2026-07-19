"""TransDub v2 配音工程的数据契约。

这些类型刻意不依赖 Qt、TTS 或媒体处理库，便于在任务线程、编辑器、CLI 和
测试中复用。JSON 反序列化会忽略未知字段，允许后续版本向前扩展。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


PROJECT_SCHEMA_VERSION = 2


class StaleReason(str, Enum):
    TRANSLATION_CHANGED = "translation_changed"
    BOUNDARY_CHANGED = "boundary_changed"
    SPEAKER_CHANGED = "speaker_changed"
    VOICE_CHANGED = "voice_changed"
    BACKEND_CHANGED = "backend_changed"
    QUALITY_FAILED = "quality_failed"
    SOURCE_CHANGED = "source_changed"


def _now() -> int:
    return int(time.time())


def _plain(value):
    if dataclasses.is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _known(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in names}


@dataclass
class JsonModel:
    def to_dict(self) -> Dict[str, Any]:
        return _plain(self)


@dataclass
class WordTiming(JsonModel):
    text: str
    start_ms: int
    end_ms: int
    confidence: Optional[float] = None

    @classmethod
    def from_dict(cls, data):
        return cls(**_known(cls, data))


@dataclass
class SpeakerTrack(JsonModel):
    id: str
    name: str = ""
    backend: str = "legacy"
    voice: str = ""
    identity_reference: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        return cls(**_known(cls, data))


@dataclass
class TextCandidate(JsonModel):
    id: str
    text: str
    kind: str = "baseline"
    semantic_score: Optional[float] = None
    naturalness_score: Optional[float] = None
    estimated_duration_ms: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        return cls(**_known(cls, data))


@dataclass
class AudioCandidate(JsonModel):
    id: str
    text_candidate_id: str
    path: str = ""
    backend: str = "legacy"
    duration_ms: Optional[int] = None
    settings: Dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now)

    @classmethod
    def from_dict(cls, data):
        return cls(**_known(cls, data))


@dataclass
class QualityReport(JsonModel):
    id: str
    unit_id: str
    audio_candidate_id: Optional[str] = None
    passed: bool = False
    hard_failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now)

    @classmethod
    def from_dict(cls, data):
        return cls(**_known(cls, data))


@dataclass
class DubUnit(JsonModel):
    id: str
    speaker_id: str
    source_start_ms: int
    source_end_ms: int
    source_text: str
    planned_start_ms: int
    planned_end_ms: int
    revision: int = 1
    stale_reasons: List[str] = field(default_factory=list)
    selected_text_candidate_id: Optional[str] = None
    selected_audio_candidate_id: Optional[str] = None
    text_candidates: List[TextCandidate] = field(default_factory=list)
    audio_candidates: List[AudioCandidate] = field(default_factory=list)
    quality_reports: List[QualityReport] = field(default_factory=list)
    legacy_payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        values = _known(cls, data)
        values["text_candidates"] = [TextCandidate.from_dict(x) for x in data.get("text_candidates", [])]
        values["audio_candidates"] = [AudioCandidate.from_dict(x) for x in data.get("audio_candidates", [])]
        values["quality_reports"] = [QualityReport.from_dict(x) for x in data.get("quality_reports", [])]
        return cls(**values)


@dataclass
class PlannedSegment(JsonModel):
    """一次规划中的实际配音段，可覆盖一个或多个原始 DubUnit。"""

    id: str
    unit_ids: List[str]
    speaker_id: str
    start_ms: int
    end_ms: int
    source_text: str
    text_candidates: List[TextCandidate] = field(default_factory=list)
    selected_text_candidate_id: Optional[str] = None
    audio_candidates: List[AudioCandidate] = field(default_factory=list)
    selected_audio_candidate_id: Optional[str] = None
    quality_reports: List[QualityReport] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        values = _known(cls, data)
        values["text_candidates"] = [TextCandidate.from_dict(x) for x in data.get("text_candidates", [])]
        values["audio_candidates"] = [AudioCandidate.from_dict(x) for x in data.get("audio_candidates", [])]
        values["quality_reports"] = [QualityReport.from_dict(x) for x in data.get("quality_reports", [])]
        return cls(**values)


@dataclass
class PlanningRevision(JsonModel):
    id: str
    scope_unit_ids: List[str]
    segmentation_kind: str
    segments: List[PlannedSegment]
    score: float
    status: str = "planned"
    created_at: int = field(default_factory=_now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        values = _known(cls, data)
        values["segments"] = [PlannedSegment.from_dict(x) for x in data.get("segments", [])]
        return cls(**values)


@dataclass
class SourceTurn(JsonModel):
    id: str
    speaker_id: str
    start_ms: int
    end_ms: int
    source_text: str
    words: List[WordTiming] = field(default_factory=list)
    unit_ids: List[str] = field(default_factory=list)
    prosody: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        values = _known(cls, data)
        values["words"] = [WordTiming.from_dict(x) for x in data.get("words", [])]
        return cls(**values)


@dataclass
class DubProject(JsonModel):
    project_id: str
    name: str
    source_language: str
    target_language: str
    schema_version: int = PROJECT_SCHEMA_VERSION
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    speakers: List[SpeakerTrack] = field(default_factory=list)
    source_turns: List[SourceTurn] = field(default_factory=list)
    units: List[DubUnit] = field(default_factory=list)
    plans: List[PlanningRevision] = field(default_factory=list)
    selected_plan_id: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def touch(self):
        self.updated_at = _now()

    @classmethod
    def from_dict(cls, data):
        values = _known(cls, data)
        values["speakers"] = [SpeakerTrack.from_dict(x) for x in data.get("speakers", [])]
        values["source_turns"] = [SourceTurn.from_dict(x) for x in data.get("source_turns", [])]
        values["units"] = [DubUnit.from_dict(x) for x in data.get("units", [])]
        values["plans"] = [PlanningRevision.from_dict(x) for x in data.get("plans", [])]
        values["schema_version"] = int(data.get("schema_version") or PROJECT_SCHEMA_VERSION)
        return cls(**values)
