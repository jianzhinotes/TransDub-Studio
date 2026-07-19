"""统一音频候选质量报告；先实现可验证的硬指标。"""

import uuid

from .constraints import stretch_ratio
from .schema import QualityReport


def evaluate_audio(segment, artifact, profile):
    window_ms = max(segment.end_ms - segment.start_ms, 1)
    ratio = stretch_ratio(artifact.duration_ms, window_ms)
    failures = []
    warnings = []
    leak = artifact.metadata.get("language_leak")
    if leak:
        failures.append("language_leak")
    if ratio > profile.max_stretch:
        failures.append("duration_overflow")
    elif ratio > profile.preferred_stretch:
        warnings.append("stretch_above_preferred")
    return QualityReport(
        id=str(uuid.uuid4()),
        unit_id=segment.id,
        audio_candidate_id=None,
        passed=not failures,
        hard_failures=failures,
        warnings=warnings,
        metrics={
            "window_ms": window_ms,
            "audio_duration_ms": artifact.duration_ms,
            "stretch_ratio": round(ratio, 4),
        },
    )
