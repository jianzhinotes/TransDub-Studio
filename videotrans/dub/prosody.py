"""YouTube-like long-video dubbing reference and structural prosody policy.

The source audio is useful for identity and performance, but feeding a different
English waveform into every Chinese synthesis request also gives a cross-language
model hundreds of opportunities to copy English phonemes.  The default policy
therefore bootstraps a verified Chinese anchor per speaker and uses the source
timeline only as a compact prosody plan.

This module intentionally has no audio/model dependency.  The same persisted plan
can be consumed by F5-TTS today and by other local backends later.
"""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Iterable


REFERENCE_MODE_HYBRID = "youtube_hybrid"
REFERENCE_MODE_SOURCE_CLONE = "source_clone"
REFERENCE_MODE_CHINESE_ONLY = "chinese_anchor_only"
REFERENCE_MODES = {
    REFERENCE_MODE_HYBRID,
    REFERENCE_MODE_SOURCE_CLONE,
    REFERENCE_MODE_CHINESE_ONLY,
}
PROSODY_PLAN_VERSION = 1
PREFLIGHT_REPORT_FILE = "preflight_report.json"


def normalize_reference_mode(value) -> str:
    aliases = {
        "hybrid": REFERENCE_MODE_HYBRID,
        "smart": REFERENCE_MODE_HYBRID,
        "youtube": REFERENCE_MODE_HYBRID,
        "youtube_like": REFERENCE_MODE_HYBRID,
        "source": REFERENCE_MODE_SOURCE_CLONE,
        "clone": REFERENCE_MODE_SOURCE_CLONE,
        "per_line_clone": REFERENCE_MODE_SOURCE_CLONE,
        "chinese": REFERENCE_MODE_CHINESE_ONLY,
        "chinese_only": REFERENCE_MODE_CHINESE_ONLY,
        "natural_zh": REFERENCE_MODE_CHINESE_ONLY,
    }
    raw = str(value or "").strip().lower().replace("-", "_")
    normalized = aliases.get(raw, raw)
    return normalized if normalized in REFERENCE_MODES else REFERENCE_MODE_HYBRID


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u4e00-\u9fff]|\d+(?:\.\d+)?", text or "")


def speaking_style(source_text: str, target_text: str = "") -> str:
    source = str(source_text or "").strip()
    target = str(target_text or "").strip()
    if source.endswith(("?", "？")) or target.endswith(("?", "？")) or re.match(
            r"(?i)^\s*(who|what|when|where|why|how|do|does|did|is|are|can|could|would|will)\b",
            source):
        return "question"
    if source.endswith(("!", "！")) or target.endswith(("!", "！")):
        return "exclamation"
    return "statement"


def build_prosody_plan(
        *,
        source_text: str,
        target_text: str,
        source_start_ms: int,
        source_end_ms: int,
        target_start_ms: int,
        target_end_ms: int,
        pause_before_ms: int = 0,
        pause_after_ms: int = 0,
        reference_mode: str = REFERENCE_MODE_HYBRID,
) -> dict:
    """Build the single timing/prosody contract used by planning and synthesis.

    This first version captures features available without loading another model:
    timing pressure, pauses and speech act.  Pitch/energy contours can be added to
    the same contract later without changing the TTS queue shape.
    """
    source_duration = max(int(source_end_ms or 0) - int(source_start_ms or 0), 1)
    target_duration = max(int(target_end_ms or 0) - int(target_start_ms or 0), 1)
    source_units = len(_tokens(source_text))
    target_units = len(_tokens(target_text))
    source_rate = source_units * 1000 / source_duration
    target_rate = target_units * 1000 / target_duration
    pressure = target_rate / max(source_rate, 0.1)
    style = speaking_style(source_text, target_text)
    return {
        "version": PROSODY_PLAN_VERSION,
        "reference_mode": normalize_reference_mode(reference_mode),
        "source_duration_ms": source_duration,
        "target_duration_ms": target_duration,
        "pause_before_ms": max(int(pause_before_ms or 0), 0),
        "pause_after_ms": max(int(pause_after_ms or 0), 0),
        "speech_act": style,
        "source_units_per_second": round(source_rate, 3),
        "target_units_per_second": round(target_rate, 3),
        "timing_pressure": round(pressure, 3),
        "emphasis": "high" if style == "exclamation" else "normal",
    }


def synthesis_policy_signature(item: dict) -> str:
    """Stable short key that prevents old-policy audio from bypassing TTS."""
    payload = {
        "text": str(item.get("text") or ""),
        "role": str(item.get("role") or ""),
        "tts_type": item.get("tts_type"),
        "reference_mode": normalize_reference_mode(item.get("reference_mode")),
        "prosody_plan": item.get("prosody_plan") or {},
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def attach_queue_prosody(
        queue: Iterable[dict], *, reference_mode: str = REFERENCE_MODE_HYBRID
) -> list[dict]:
    """Attach a deterministic plan to the final materialized queue in place."""
    rows = list(queue)
    mode = normalize_reference_mode(reference_mode)

    def as_int(item, key, fallback):
        value = item.get(key)
        return int(fallback if value is None or value == "" else value)

    for index, item in enumerate(rows):
        start = as_int(item, "start_time", 0)
        end = as_int(item, "end_time", start)
        source_start = as_int(item, "start_time_source", start)
        source_end = as_int(item, "end_time_source", end)
        previous_end = as_int(rows[index - 1], "end_time", start) if index else start
        next_start = (
            as_int(rows[index + 1], "start_time", end)
            if index + 1 < len(rows) else end
        )
        plan = build_prosody_plan(
            source_text=str(item.get("ref_text") or ""),
            target_text=str(item.get("text") or ""),
            source_start_ms=source_start,
            source_end_ms=source_end,
            target_start_ms=start,
            target_end_ms=end,
            pause_before_ms=max(start - previous_end, 0),
            pause_after_ms=max(next_start - end, 0),
            reference_mode=mode,
        )
        item["reference_mode"] = mode
        item["prosody_plan"] = plan
        item["target_duration_ms"] = plan["target_duration_ms"]
    return rows


def apply_smart_synthesis_policy(
        queue: Iterable[dict], *, reference_mode: str = REFERENCE_MODE_HYBRID
) -> list[dict]:
    """Finalize the queue contract and invalidate files from older policies."""
    rows = attach_queue_prosody(queue, reference_mode=reference_mode)
    for index, item in enumerate(rows):
        filename = Path(str(item.get("filename") or ""))
        if item.get("planned_segment_id") or filename.name.startswith("smart-"):
            suffix = filename.suffix or ".wav"
            item["filename"] = str(
                filename.parent
                / f"smart-{index}-{synthesis_policy_signature(item)}{suffix}"
            )
    return rows
