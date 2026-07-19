"""联合规划器的硬约束与文本阶段评分。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    preferred_stretch: float = 1.12
    max_stretch: float = 1.25
    max_audio_attempts: int = 2


def stretch_ratio(duration_ms: int, window_ms: int) -> float:
    return float(duration_ms) / max(int(window_ms), 1)


def candidate_loss(candidate, window_ms: int, profile: QualityProfile) -> float:
    ratio = stretch_ratio(candidate.estimated_duration_ms or window_ms, window_ms)
    semantic_loss = 1.0 - float(candidate.semantic_score or 0.0)
    naturalness_loss = 1.0 - float(candidate.naturalness_score or 0.0)
    if ratio > profile.max_stretch:
        timing_loss = 1.0 + (ratio - profile.max_stretch) * 4.0
    elif ratio > profile.preferred_stretch:
        timing_loss = (ratio - profile.preferred_stretch) * 1.8
    elif ratio < 0.45:
        timing_loss = (0.45 - ratio) * 0.25
    else:
        timing_loss = 0.0
    loss = 0.46 * semantic_loss + 0.24 * naturalness_loss + 0.30 * timing_loss
    # Ordinary-English contamination is a hard quality violation for Chinese
    # dubbing, not a soft naturalness trade-off.  Keep the candidate visible for
    # audit, but make it impossible to beat a clean candidate merely by being
    # shorter and fitting the time window better.
    if (candidate.metadata or {}).get("english_leak_fallback"):
        loss += 10.0
    return loss
