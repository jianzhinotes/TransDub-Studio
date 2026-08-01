"""YouTube-like long-video dubbing reference and structural prosody policy.

The source audio is useful for identity and performance, but feeding a different
English waveform into every Chinese synthesis request also gives a cross-language
model hundreds of opportunities to copy English phonemes.  The default policy
therefore bootstraps a verified Chinese anchor per speaker and uses the source
timeline only as a compact prosody plan.

Audio profiling is deliberately model-free: a few frame-level statistics capture
relative performance without keeping another neural model in memory.  The same
persisted plan can be consumed by F5-TTS today and by other local backends later.
"""

from __future__ import annotations

import re
import hashlib
import json
import math
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
PROSODY_PLAN_VERSION = 2
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


def analyze_audio_performance(filename, *, frame_ms: int = 20) -> dict:
    """Return cheap, language-independent performance features for one clip.

    Absolute loudness varies with microphones and source mastering, so callers
    should use it only after normalizing within a speaker.  No pitch or phoneme
    representation is extracted: English articulation cannot leak through this
    contract.
    """
    try:
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(
            str(filename), dtype="float32", always_2d=True)
        if not audio.size or int(sample_rate or 0) <= 0:
            return {}
        mono = audio.mean(axis=1, dtype=np.float32)
        mono = mono[np.isfinite(mono)]
        if mono.size < max(int(sample_rate * 0.08), 1):
            return {}
        size = max(int(sample_rate * max(int(frame_ms), 5) / 1000), 1)
        usable = mono[: mono.size - (mono.size % size)]
        if not usable.size:
            usable = np.pad(mono, (0, size - mono.size))
        frames = usable.reshape(-1, size)
        rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
        frame_db = 20.0 * np.log10(np.maximum(rms, 1e-6))
        upper = float(np.percentile(frame_db, 90))
        active_threshold = max(-50.0, upper - 24.0)
        active = frame_db[frame_db >= active_threshold]
        if not active.size:
            active = frame_db
        overall_rms = float(np.sqrt(np.mean(np.square(mono)) + 1e-12))
        return {
            "energy_dbfs": round(20.0 * math.log10(max(overall_rms, 1e-6)), 2),
            "peak_dbfs": round(20.0 * math.log10(max(float(np.max(np.abs(mono))), 1e-6)), 2),
            "activity_ratio": round(float(active.size / max(frame_db.size, 1)), 3),
            "dynamic_range_db": round(float(
                np.percentile(active, 90) - np.percentile(active, 20)), 2),
        }
    except (ImportError, OSError, RuntimeError, ValueError, TypeError):
        return {}


def attach_source_performance(queue: Iterable[dict]) -> list[dict]:
    """Normalize source energy per speaker and enrich the shared prosody plan.

    The bounded gain is intentionally subtle.  It preserves the Chinese TTS
    model's natural phrasing while carrying emphasis across sentence boundaries.
    """
    rows = list(queue)
    measured = {}
    by_speaker = {}
    for index, item in enumerate(rows):
        ref_wav = item.get("ref_wav")
        if not ref_wav or not Path(str(ref_wav)).is_file():
            continue
        profile = analyze_audio_performance(ref_wav)
        if not profile:
            continue
        measured[index] = profile
        speaker = str(
            item.get("speaker_cluster_id")
            or item.get("cluster_ref")
            or item.get("speaker_id")
            or item.get("spk")
            or "__main_speaker__"
        )
        by_speaker.setdefault(speaker, []).append(profile["energy_dbfs"])

    for index, profile in measured.items():
        item = rows[index]
        speaker = str(
            item.get("speaker_cluster_id")
            or item.get("cluster_ref")
            or item.get("speaker_id")
            or item.get("spk")
            or "__main_speaker__"
        )
        energies = sorted(by_speaker[speaker])
        middle = len(energies) // 2
        baseline = (
            energies[middle] if len(energies) % 2
            else (energies[middle - 1] + energies[middle]) / 2
        )
        relative = max(-2.0, min(2.0, profile["energy_dbfs"] - baseline))
        plan = item.setdefault("prosody_plan", {})
        speech_act = str(plan.get("speech_act") or "statement")
        expressive_bonus = 0.35 if speech_act == "exclamation" else 0.0
        gain = max(-2.0, min(2.0, relative * 0.7 + expressive_bonus))
        plan["performance"] = {
            **profile,
            "speaker_energy_baseline_dbfs": round(baseline, 2),
            "relative_energy_db": round(relative, 2),
            "output_gain_db": round(gain, 2),
            "source": "speaker_relative_frame_statistics",
        }
        plan["version"] = PROSODY_PLAN_VERSION
    return rows


def apply_output_performance(filename, plan: dict) -> float:
    """Apply the plan's bounded expression gain atomically; return applied dB."""
    gain = float(((plan or {}).get("performance") or {}).get("output_gain_db") or 0)
    gain = max(-2.0, min(2.0, gain))
    path = Path(str(filename or ""))
    if abs(gain) < 0.1 or not path.is_file():
        return 0.0
    temp = path.with_name(f".{path.stem}.performance{path.suffix or '.wav'}")
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_file(path)
        # Preserve 0.5 dB of headroom.  Expression transfer must never trade
        # naturalness for digital clipping on already-hot generated clips.
        if gain > 0 and math.isfinite(audio.max_dBFS):
            gain = min(gain, max(-0.5 - float(audio.max_dBFS), 0.0))
        if abs(gain) < 0.1:
            return 0.0
        audio.apply_gain(gain).export(temp, format="wav")
        temp.replace(path)
        return round(gain, 2)
    except Exception:
        # Performance transfer is an optional finishing layer; a malformed
        # clip will still be caught by the mandatory duration/content gates.
        return 0.0
    finally:
        temp.unlink(missing_ok=True)


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
        existing_performance = dict(
            ((item.get("prosody_plan") or {}).get("performance") or {}))
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
        if existing_performance:
            plan["performance"] = existing_performance
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
