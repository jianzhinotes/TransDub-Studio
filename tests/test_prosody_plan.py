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
