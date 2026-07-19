"""本地、确定性的中文候选生成；后续可由本地 LLM 后端替换。"""

import hashlib
import re

from .schema import TextCandidate


_SPOKEN_REPLACEMENTS = (
    ("我们将会", "我们会"),
    ("然而", "不过"),
    ("因此", "所以"),
    ("此外", "另外"),
    ("例如", "比如"),
    ("我认为", "我觉得"),
)

_COMPACT_FILLERS = (
    "事实上", "实际上", "基本上", "你知道", "我的意思是", "从某种程度上说",
)


def _clean(text):
    text = re.sub(r"[ \t]+", " ", str(text or "").strip())
    text = re.sub(r"\s*([，。！？；：])\s*", r"\1", text)
    text = re.sub(r"，{2,}", "，", text)
    return text


def _id(segment_id, kind, text):
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{segment_id}:{kind}:{digest}"


class ChineseCandidateGenerator:
    """只做保守改写，不删除数字、专名或普通实词。"""

    name = "rules"

    def generate_turn(self, *, turn, options, target_language, duration_model):
        """按整轮接口返回候选；规则实现仍保持完全本地和确定性。"""
        return {
            option.id: {
                group.id: self.generate(
                    segment_id=group.id,
                    baseline_text=group.baseline_text,
                    target_duration_ms=max(group.end_ms - group.start_ms, 1),
                    speaker_id=group.speaker_id,
                    duration_model=duration_model,
                )
                for group in option.groups
            }
            for option in options
        }

    def generate(self, *, segment_id, baseline_text, target_duration_ms,
                 speaker_id, duration_model):
        baseline = _clean(baseline_text)
        candidates = [TextCandidate(
            id=_id(segment_id, "baseline", baseline),
            text=baseline,
            kind="baseline",
            semantic_score=1.0,
            naturalness_score=0.72,
            estimated_duration_ms=duration_model.estimate(baseline, speaker_id),
        )]
        if not re.search(r"[\u3400-\u9fff]", baseline):
            return candidates

        spoken = baseline
        replacements = []
        for before, after in _SPOKEN_REPLACEMENTS:
            if before in spoken:
                spoken = spoken.replace(before, after)
                replacements.append([before, after])
        spoken = _clean(spoken)
        if spoken and spoken != baseline:
            candidates.append(TextCandidate(
                id=_id(segment_id, "spoken", spoken),
                text=spoken,
                kind="spoken",
                semantic_score=0.98,
                naturalness_score=0.90,
                estimated_duration_ms=duration_model.estimate(spoken, speaker_id),
                metadata={"replacements": replacements},
            ))

        compact = spoken
        removed = []
        for filler in _COMPACT_FILLERS:
            if filler in compact:
                compact = compact.replace(filler, "")
                removed.append(filler)
        compact = re.sub(r"^[嗯呃啊]+[，,、 ]*", "", compact)
        compact = _clean(compact).lstrip("，,")
        if compact and compact not in {candidate.text for candidate in candidates}:
            candidates.append(TextCandidate(
                id=_id(segment_id, "compact", compact),
                text=compact,
                kind="compact",
                semantic_score=0.91,
                naturalness_score=0.84,
                estimated_duration_ms=duration_model.estimate(compact, speaker_id),
                metadata={"removed_fillers": removed},
            ))
        return candidates
