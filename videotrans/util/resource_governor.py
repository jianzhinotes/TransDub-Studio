"""Automatic resource limits for long local AI pipelines.

These limits change concurrency, never model quality.  In particular a 16 GB
Apple Silicon machine must not keep F5 and a large validator resident together.
"""

from __future__ import annotations

import os
import platform
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ResourceProfile:
    low_memory_apple_silicon: bool
    reference_workers: int
    separation_threads: int
    validator_cpu_threads: int
    ffmpeg_audio_threads: int = 1


PressureLevel = Literal["normal", "elevated", "high", "critical"]


@dataclass(frozen=True)
class ResourceSnapshot:
    """Small, dependency-tolerant view of current machine pressure."""

    memory_percent: float = 0.0
    available_mb: int = 0
    swap_used_mb: int = 0
    load_per_cpu: float = 0.0
    pressure: PressureLevel = "normal"


@dataclass(frozen=True)
class RuntimeLimits:
    """Concurrency limits selected without changing any model or quality rule."""

    reference_workers: int
    separation_threads: int
    validator_cpu_threads: int
    validation_batch_size: int
    pressure: PressureLevel


def _physical_memory_bytes() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def current_profile() -> ResourceProfile:
    cpu_count = max(int(os.cpu_count() or 1), 1)
    apple = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    memory = _physical_memory_bytes()
    low_memory = bool(apple and memory and memory <= 18 * 1024 ** 3)
    if low_memory:
        return ResourceProfile(
            low_memory_apple_silicon=True,
            reference_workers=min(2, cpu_count),
            separation_threads=min(2, cpu_count),
            validator_cpu_threads=min(4, cpu_count),
        )
    return ResourceProfile(
        low_memory_apple_silicon=False,
        reference_workers=min(4, cpu_count),
        separation_threads=min(4, cpu_count),
        validator_cpu_threads=min(6, cpu_count),
    )


def _pressure_level(
        *, memory_percent: float, available_mb: int, swap_used_mb: int,
        load_per_cpu: float) -> PressureLevel:
    """Classify pressure conservatively for long unified-memory workloads."""
    if (
            memory_percent >= 92
            or (available_mb and available_mb <= 1024)
            or swap_used_mb >= 6144
    ):
        return "critical"
    if (
            memory_percent >= 86
            or (available_mb and available_mb <= 2048)
            or swap_used_mb >= 3072
            or load_per_cpu >= 1.25
    ):
        return "high"
    if (
            memory_percent >= 78
            or (available_mb and available_mb <= 3584)
            or swap_used_mb >= 1024
            or load_per_cpu >= 0.85
    ):
        return "elevated"
    return "normal"


def resource_snapshot() -> ResourceSnapshot:
    """Read live pressure; gracefully fall back when psutil is unavailable."""
    memory_percent = 0.0
    available_mb = 0
    swap_used_mb = 0
    try:
        import psutil

        memory = psutil.virtual_memory()
        memory_percent = float(memory.percent)
        available_mb = int(memory.available // 1024 ** 2)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        import psutil

        swap_used_mb = int(psutil.swap_memory().used // 1024 ** 2)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        # macOS may deny the swap query inside a sandbox. Memory/load pressure
        # still provides a safe and useful signal in that environment.
        pass

    cpu_count = max(int(os.cpu_count() or 1), 1)
    try:
        load_per_cpu = max(float(os.getloadavg()[0]) / cpu_count, 0.0)
    except (AttributeError, OSError, TypeError, ValueError):
        load_per_cpu = 0.0
    pressure = _pressure_level(
        memory_percent=memory_percent,
        available_mb=available_mb,
        swap_used_mb=swap_used_mb,
        load_per_cpu=load_per_cpu,
    )
    return ResourceSnapshot(
        memory_percent=round(memory_percent, 1),
        available_mb=available_mb,
        swap_used_mb=swap_used_mb,
        load_per_cpu=round(load_per_cpu, 2),
        pressure=pressure,
    )


def runtime_limits(
        *, mode: str = "auto", snapshot: ResourceSnapshot | None = None,
        validation_batch_size: int = 24) -> RuntimeLimits:
    """Return live limits for the next stage.

    ``auto`` is the default. ``cool`` intentionally moves one pressure step
    toward lower heat. ``performance`` keeps normal limits unless pressure is
    already high, so safety is never fully disabled.
    """
    profile = current_profile()
    snapshot = snapshot or resource_snapshot()
    levels: tuple[PressureLevel, ...] = ("normal", "elevated", "high", "critical")
    level_index = levels.index(snapshot.pressure)
    mode = str(mode or "auto").strip().lower()
    if mode == "cool":
        level_index = min(level_index + 1, len(levels) - 1)
    elif mode == "performance" and level_index == 1:
        level_index = 0
    pressure = levels[level_index]

    base_batch = max(4, min(int(validation_batch_size or 24), 40))
    if pressure == "critical":
        return RuntimeLimits(1, 1, min(2, profile.validator_cpu_threads),
                             min(base_batch, 12), pressure)
    if pressure == "high":
        return RuntimeLimits(1, 1, min(3, profile.validator_cpu_threads),
                             min(base_batch, 16), pressure)
    if pressure == "elevated":
        return RuntimeLimits(
            min(2, max(1, profile.reference_workers - 1)),
            min(2, max(1, profile.separation_threads - 1)),
            min(4, max(2, profile.validator_cpu_threads - 1)),
            min(base_batch, 20),
            pressure,
        )
    return RuntimeLimits(
        profile.reference_workers,
        profile.separation_threads,
        profile.validator_cpu_threads,
        base_batch,
        pressure,
    )


class ResourceGovernor:
    """Process-local exclusion for heavyweight model stages.

    F5 itself lives in a managed child service.  The orchestrator still enters
    this guard around model lifecycle transitions so future MLX/CT2 workers use
    the same scheduling contract.
    """

    _heavy_model_lock = threading.RLock()

    @classmethod
    @contextmanager
    def heavy_model(cls, _name: str):
        with cls._heavy_model_lock:
            yield
