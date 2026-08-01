from pathlib import Path

import numpy as np
import soundfile as sf

from videotrans.dub.legacy_adapter import (
    make_project_id,
    plan_to_queue,
    project_from_queue,
)
from videotrans.dub.planner import JointDubPlanner
from videotrans.dub.prosody import (
    REFERENCE_MODE_CHINESE_ONLY,
    REFERENCE_MODE_HYBRID,
    REFERENCE_MODE_SOURCE_CLONE,
    apply_smart_synthesis_policy,
    apply_output_performance,
    attach_source_performance,
    attach_queue_prosody,
    build_prosody_plan,
    normalize_reference_mode,
    synthesis_policy_signature,
)


def _row(line, text, start, end, ref_text):
    return {
        "line": line,
        "text": text,
        "ref_text": ref_text,
        "start_time": start,
        "end_time": end,
        "start_time_source": start,
        "end_time_source": end,
        "role": "clone",
        "tts_type": 8,
        "filename": f"/tmp/prosody-{line}.wav",
    }


def test_reference_mode_is_safe_by_default_and_source_clone_is_explicit():
    assert normalize_reference_mode(None) == REFERENCE_MODE_HYBRID
    assert normalize_reference_mode("youtube") == REFERENCE_MODE_HYBRID
    assert normalize_reference_mode("clone") == REFERENCE_MODE_SOURCE_CLONE
    assert normalize_reference_mode("natural_zh") == REFERENCE_MODE_CHINESE_ONLY
    assert normalize_reference_mode("unknown") == REFERENCE_MODE_HYBRID


def test_prosody_plan_unifies_timing_pauses_and_speech_act():
    plan = build_prosody_plan(
        source_text="How does this work?",
        target_text="这是怎么实现的",
        source_start_ms=1000,
        source_end_ms=3000,
        target_start_ms=900,
        target_end_ms=3400,
        pause_before_ms=250,
        pause_after_ms=600,
    )
    assert plan["reference_mode"] == REFERENCE_MODE_HYBRID
    assert plan["source_duration_ms"] == 2000
    assert plan["target_duration_ms"] == 2500
    assert plan["pause_before_ms"] == 250
    assert plan["pause_after_ms"] == 600
    assert plan["speech_act"] == "question"
    assert plan["timing_pressure"] > 0


def test_queue_prosody_uses_final_timeline_and_preserves_zero_source_start():
    queue = [
        _row(1, "第一句", 100, 1100, "First sentence."),
        _row(2, "第二句", 1400, 2600, "Second sentence!"),
    ]
    queue[0]["start_time_source"] = 0
    attach_queue_prosody(queue)
    assert queue[0]["prosody_plan"]["source_duration_ms"] == 1100
    assert queue[0]["prosody_plan"]["pause_after_ms"] == 300
    assert queue[1]["prosody_plan"]["pause_before_ms"] == 300
    assert queue[1]["prosody_plan"]["speech_act"] == "exclamation"
    assert queue[0]["reference_mode"] == REFERENCE_MODE_HYBRID


def test_reference_policy_changes_audio_signature():
    item = _row(1, "测试", 0, 1000, "Test.")
    attach_queue_prosody([item], reference_mode=REFERENCE_MODE_HYBRID)
    hybrid = synthesis_policy_signature(item)
    attach_queue_prosody([item], reference_mode=REFERENCE_MODE_SOURCE_CLONE)
    assert synthesis_policy_signature(item) != hybrid


def test_smart_policy_changes_old_audio_path_and_is_stable():
    item = _row(1, "测试", 0, 1000, "Test.")
    item["filename"] = "/cache/smart-0-old.wav"
    item["planned_segment_id"] = "seg-1"
    apply_smart_synthesis_policy([item], reference_mode=REFERENCE_MODE_HYBRID)
    hybrid_path = item["filename"]
    assert hybrid_path != "/cache/smart-0-old.wav"
    apply_smart_synthesis_policy([item], reference_mode=REFERENCE_MODE_HYBRID)
    assert item["filename"] == hybrid_path
    apply_smart_synthesis_policy([item], reference_mode=REFERENCE_MODE_SOURCE_CLONE)
    assert item["filename"] != hybrid_path


def test_joint_plan_materializes_same_prosody_contract():
    queue = [_row(1, "这是怎么实现的", 0, 2200, "How does this work?")]
    project = project_from_queue(
        queue,
        project_id=make_project_id("/video/prosody.mp4", "zh-cn"),
        name="prosody",
        source_language="en",
        target_language="zh-cn",
    )
    plan = JointDubPlanner().optimize(project, limit=None)
    assert plan.segments[0].prosody["speech_act"] == "question"
    materialized = plan_to_queue(project, plan)
    assert materialized[0]["prosody_plan"] == plan.segments[0].prosody
    assert materialized[0]["reference_mode"] == REFERENCE_MODE_HYBRID


def _tone(path, amplitude):
    sample_rate = 16000
    timeline = np.arange(sample_rate, dtype=np.float32) / sample_rate
    audio = amplitude * np.sin(2 * np.pi * 180 * timeline)
    sf.write(path, audio, sample_rate)
    return str(path)


def test_source_performance_is_speaker_relative_and_bounded(tmp_path):
    queue = [
        _row(1, "轻声说明。", 0, 1000, "Quiet statement."),
        _row(2, "重点说明！", 1000, 2000, "Important statement!"),
    ]
    queue[0]["ref_wav"] = _tone(tmp_path / "quiet.wav", 0.08)
    queue[1]["ref_wav"] = _tone(tmp_path / "loud.wav", 0.45)
    attach_queue_prosody(queue)
    before = synthesis_policy_signature(queue[0])
    attach_source_performance(queue)

    quiet = queue[0]["prosody_plan"]["performance"]
    loud = queue[1]["prosody_plan"]["performance"]
    assert quiet["relative_energy_db"] < 0
    assert loud["relative_energy_db"] > 0
    assert -2 <= quiet["output_gain_db"] <= 2
    assert -2 <= loud["output_gain_db"] <= 2
    assert synthesis_policy_signature(queue[0]) != before


def test_source_performance_normalizes_explicit_speakers_independently(tmp_path):
    queue = [
        _row(1, "甲说话。", 0, 1000, "Speaker A."),
        _row(2, "乙说话。", 1000, 2000, "Speaker B."),
    ]
    queue[0].update(
        speaker_id="speaker-a", ref_wav=_tone(tmp_path / "a.wav", 0.05))
    queue[1].update(
        speaker_id="speaker-b", ref_wav=_tone(tmp_path / "b.wav", 0.5))
    attach_queue_prosody(queue)
    attach_source_performance(queue)

    assert queue[0]["prosody_plan"]["performance"]["relative_energy_db"] == 0
    assert queue[1]["prosody_plan"]["performance"]["relative_energy_db"] == 0


def test_output_performance_gain_is_applied_atomically(tmp_path):
    target = Path(_tone(tmp_path / "target.wav", 0.1))
    before, _ = sf.read(target)
    applied = apply_output_performance(
        target, {"performance": {"output_gain_db": 2.0}})
    after, _ = sf.read(target)

    assert applied == 2.0
    assert np.sqrt(np.mean(after ** 2)) > np.sqrt(np.mean(before ** 2))
    assert not list(tmp_path.glob(".*.performance.wav"))
