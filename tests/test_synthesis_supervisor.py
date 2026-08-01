from videotrans.dub.synthesis_supervisor import SynthesisSupervisor
from videotrans.util.resource_governor import ResourceSnapshot


def _normal():
    return ResourceSnapshot(
        memory_percent=55, available_mb=7000, swap_used_mb=2000,
        load_per_cpu=0.2, pressure="normal")


def test_admission_preserves_slow_rate_when_slot_has_room():
    supervisor = SynthesisSupervisor(snapshot_fn=_normal)
    decision = supervisor.admit(
        requested_speed=0.5,
        ref_text="A reasonably complete source sentence.",
        gen_text="短句。",
        ref_duration_ms=5000,
        target_duration_ms=6000,
        fit_to_slot=True,
    )
    assert decision.effective_speed == 0.5
    assert decision.action == "preserve_rate"


def test_admission_bounds_pathological_slow_generation():
    supervisor = SynthesisSupervisor(snapshot_fn=_normal)
    decision = supervisor.admit(
        requested_speed=0.5,
        ref_text="AI卫星其实比星链卫星简单得多。",
        gen_text="卡尔达肖夫考虑过这个问题，这是一个很好的分类方式，你可以评估。",
        ref_duration_ms=4049,
        target_duration_ms=6120,
        fit_to_slot=True,
    )
    assert decision.effective_speed == 1.3
    assert decision.predicted_slot_ratio < 1.5
    assert decision.action == "fit_slot"


def test_watchdog_uses_rolling_median_after_warmup():
    supervisor = SynthesisSupervisor(
        stall_floor_s=30, stall_multiplier=4, snapshot_fn=_normal)
    for elapsed in (10, 11, 12):
        supervisor.finish(elapsed, success=True)
    assert supervisor.timeout_seconds() == 44
    assert supervisor.is_stalled(100, now=143) is False
    assert supervisor.is_stalled(100, now=144) is True


def test_absolute_old_swap_does_not_block_a_fresh_run():
    supervisor = SynthesisSupervisor(snapshot_fn=lambda: ResourceSnapshot(
        memory_percent=60, available_mb=6500, swap_used_mb=9000,
        load_per_cpu=0.2, pressure="critical"))
    decision = supervisor.resource_decision(ResourceSnapshot(
        memory_percent=61, available_mb=6200, swap_used_mb=9200,
        load_per_cpu=0.2, pressure="critical"))
    assert decision.allow is True
    assert decision.swap_growth_mb == 200


def test_swap_growth_with_low_headroom_trips_recycle():
    supervisor = SynthesisSupervisor(snapshot_fn=_normal)
    decision = supervisor.resource_decision(ResourceSnapshot(
        memory_percent=84, available_mb=2400, swap_used_mb=3300,
        load_per_cpu=0.3, pressure="high"))
    assert decision.allow is False
    assert decision.recycle_service is True
    assert "Swap" in decision.reason
