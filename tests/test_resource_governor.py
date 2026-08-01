from videotrans.util import resource_governor


def _profile():
    return resource_governor.ResourceProfile(
        low_memory_apple_silicon=True,
        reference_workers=2,
        separation_threads=2,
        validator_cpu_threads=4,
    )


def test_runtime_limits_keep_quality_and_normal_concurrency(monkeypatch):
    monkeypatch.setattr(resource_governor, "current_profile", _profile)
    snapshot = resource_governor.ResourceSnapshot(
        memory_percent=55,
        available_mb=7000,
        swap_used_mb=0,
        load_per_cpu=0.3,
        pressure="normal",
    )

    limits = resource_governor.runtime_limits(
        snapshot=snapshot, validation_batch_size=24
    )

    assert limits.reference_workers == 2
    assert limits.separation_threads == 2
    assert limits.validator_cpu_threads == 4
    assert limits.validation_batch_size == 24
    assert limits.pressure == "normal"


def test_runtime_limits_shrink_under_critical_pressure(monkeypatch):
    monkeypatch.setattr(resource_governor, "current_profile", _profile)
    snapshot = resource_governor.ResourceSnapshot(
        memory_percent=94,
        available_mb=800,
        swap_used_mb=7000,
        load_per_cpu=1.4,
        pressure="critical",
    )

    limits = resource_governor.runtime_limits(
        snapshot=snapshot, validation_batch_size=24
    )

    assert limits.reference_workers == 1
    assert limits.separation_threads == 1
    assert limits.validator_cpu_threads == 2
    assert limits.validation_batch_size == 12
    assert limits.pressure == "critical"


def test_cool_mode_reduces_heat_before_machine_is_under_pressure(monkeypatch):
    monkeypatch.setattr(resource_governor, "current_profile", _profile)
    snapshot = resource_governor.ResourceSnapshot(pressure="normal")

    limits = resource_governor.runtime_limits(mode="cool", snapshot=snapshot)

    assert limits.pressure == "elevated"
    assert limits.reference_workers == 1
    assert limits.separation_threads == 1
    assert limits.validator_cpu_threads == 3
    assert limits.validation_batch_size == 20


def test_pressure_level_uses_swap_even_when_memory_percent_is_moderate():
    level = resource_governor._pressure_level(
        memory_percent=70,
        available_mb=5000,
        swap_used_mb=3500,
        load_per_cpu=0.2,
    )

    assert level == "high"


def test_snapshot_keeps_memory_signal_when_swap_query_is_denied(monkeypatch):
    import psutil

    memory = type("Memory", (), {"percent": 81.0, "available": 3 * 1024 ** 3})()
    monkeypatch.setattr(psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(
        psutil, "swap_memory", lambda: (_ for _ in ()).throw(PermissionError())
    )
    monkeypatch.setattr(resource_governor.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(resource_governor.os, "cpu_count", lambda: 10)

    snapshot = resource_governor.resource_snapshot()

    assert snapshot.memory_percent == 81.0
    assert snapshot.available_mb == 3072
    assert snapshot.swap_used_mb == 0
    assert snapshot.pressure == "elevated"
