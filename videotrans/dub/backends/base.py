"""可替换配音后端契约。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class BackendCapabilities:
    supports_voice_clone: bool = False
    supports_performance_reference: bool = False
    supports_speed_control: bool = False
    supports_duration_control: bool = False
    supports_batch: bool = True
    recommended_concurrency: int = 1


@dataclass
class SynthesisRequest:
    id: str
    segment_id: str
    text_candidate_id: str
    text: str
    output_path: str
    language: str
    speaker_id: str
    legacy_payload: Dict[str, Any] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AudioArtifact:
    request_id: str
    path: str
    duration_ms: int
    backend: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class DubbingBackend:
    name = "unknown"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def synthesize_batch(self, requests: List[SynthesisRequest]) -> List[AudioArtifact]:
        raise NotImplementedError

    def should_isolate_failure(self, requests, error: Exception) -> bool:
        """批量失败是否可能由单个片段引起。假后端/通用后端默认可隔离。"""
        return True

    def synthesize(self, request: SynthesisRequest) -> AudioArtifact:
        return self.synthesize_batch([request])[0]
