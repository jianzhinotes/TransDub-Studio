"""TransDub 的版本化配音工程与联合规划基础设施。

第一阶段只提供数据契约、持久化和旧 ``queue_tts`` 兼容层。现有任务流水线
仍然可以继续读写 queue_tts；后续联合规划器将逐步改为直接操作 DubProject。
"""

from .schema import (
    AudioCandidate,
    DubProject,
    DubUnit,
    QualityReport,
    PlannedSegment,
    PlanningRevision,
    SourceTurn,
    SpeakerTrack,
    StaleReason,
    TextCandidate,
    WordTiming,
    PROJECT_SCHEMA_VERSION,
)
from .llm_candidates import DeepSeekCandidateGenerator, build_candidate_generator

__all__ = [
    "AudioCandidate",
    "DubProject",
    "DubUnit",
    "QualityReport",
    "PlannedSegment",
    "PlanningRevision",
    "SourceTurn",
    "SpeakerTrack",
    "StaleReason",
    "TextCandidate",
    "WordTiming",
    "PROJECT_SCHEMA_VERSION",
    "DeepSeekCandidateGenerator",
    "build_candidate_generator",
]
