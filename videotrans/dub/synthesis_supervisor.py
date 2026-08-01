"""Backend-neutral admission, watchdog, and resource policy for synthesis."""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from videotrans.util.resource_governor import ResourceSnapshot, resource_snapshot


@dataclass(frozen=True)
class AdmissionDecision:
    requested_speed: float
    effective_speed: float
    target_duration_ms: int
    predicted_duration_ms: int
    predicted_slot_ratio: float
    action: str


@dataclass(frozen=True)
class ResourceDecision:
    allow: bool
    recycle_service: bool
    reason: str
    snapshot: ResourceSnapshot
    swap_growth_mb: int


class SynthesisSupervisor:
    """One policy owner for duration, liveness, and unified-memory safety."""

    def __init__(self, *, stall_floor_s: float = 180.0,
                 stall_multiplier: float = 4.0, history_size: int = 20,
                 min_available_mb: int = 1536, max_slot_ratio: float = 1.15,
                 max_backend_speed: float = 1.3, snapshot_fn=None):
        self.stall_floor_s = max(float(stall_floor_s), 30.0)
        self.stall_multiplier = max(float(stall_multiplier), 2.0)
        self.min_available_mb = max(int(min_available_mb), 512)
        self.max_slot_ratio = max(float(max_slot_ratio), 1.0)
        self.max_backend_speed = max(float(max_backend_speed), 1.0)
        self._durations = deque(maxlen=max(int(history_size), 3))
        self._lock = threading.RLock()
        self._snapshot_fn = snapshot_fn or resource_snapshot
        initial = self._snapshot_fn()
        self._baseline_swap_mb = int(initial.swap_used_mb or 0)
        self.completed = 0
        self.timeouts = 0
        self.recycles = 0
        self.last_admission = None
        self.last_resource = None

    @staticmethod
    def _bounded_speed(value) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        return max(0.3, min(value, 2.0))

    def admit(self, *, requested_speed: float, ref_text: str, gen_text: str,
              ref_duration_ms: int, target_duration_ms: int,
              fit_to_slot: bool) -> AdmissionDecision:
        requested = self._bounded_speed(requested_speed)
        target = max(int(target_duration_ms or 0), 0)
        reference_ms = max(int(ref_duration_ms or 0), 0)
        ref_bytes = max(len((ref_text or "").encode("utf-8")), 1)
        gen_bytes = max(len((gen_text or "").encode("utf-8")), 1)
        predicted_at_one = reference_ms * gen_bytes / ref_bytes
        effective = requested
        action = "preserve_rate"
        if fit_to_slot and target and reference_ms:
            required = (
                predicted_at_one * 1.35 / (target * self.max_slot_ratio)
            )
            effective = max(requested, min(required, self.max_backend_speed))
            if effective > requested + 0.005:
                action = "fit_slot"
        predicted = int(round(predicted_at_one / max(effective, 0.3)))
        ratio = round(predicted / target, 3) if target else 0.0
        if fit_to_slot and target and ratio > 1.5:
            action = "bounded_overflow"
        decision = AdmissionDecision(
            requested_speed=round(requested, 2),
            effective_speed=round(effective, 2),
            target_duration_ms=target,
            predicted_duration_ms=predicted,
            predicted_slot_ratio=ratio,
            action=action,
        )
        with self._lock:
            self.last_admission = decision
        return decision

    def timeout_seconds(self) -> float:
        with self._lock:
            recent = list(self._durations)
        rolling = statistics.median(recent) * self.stall_multiplier if recent else 0.0
        return min(max(self.stall_floor_s, rolling), 900.0)

    def is_stalled(self, started_at: float, *, now=None) -> bool:
        now = time.monotonic() if now is None else float(now)
        return now - float(started_at) >= self.timeout_seconds()

    def finish(self, elapsed_s: float, *, success: bool, timed_out: bool = False):
        with self._lock:
            if timed_out:
                self.timeouts += 1
            if success:
                self._durations.append(max(float(elapsed_s), 0.0))
                self.completed += 1

    def resource_decision(self, snapshot=None) -> ResourceDecision:
        snapshot = snapshot or self._snapshot_fn()
        swap_growth = max(
            int(snapshot.swap_used_mb or 0) - self._baseline_swap_mb, 0)
        low_available = bool(
            snapshot.available_mb and snapshot.available_mb < self.min_available_mb)
        memory_critical = snapshot.memory_percent >= 92
        growing_swap_pressure = bool(
            swap_growth >= 1024
            and (
                snapshot.memory_percent >= 78
                or (snapshot.available_mb and snapshot.available_mb < 3584)
            )
        )
        allow = not (low_available or memory_critical or growing_swap_pressure)
        if low_available:
            reason = f"可用内存仅 {snapshot.available_mb} MB"
        elif memory_critical:
            reason = f"系统内存占用 {snapshot.memory_percent:.1f}%"
        elif growing_swap_pressure:
            reason = f"本轮 Swap 增长 {swap_growth} MB"
        else:
            reason = "资源正常"
        decision = ResourceDecision(
            allow=allow,
            recycle_service=not allow,
            reason=reason,
            snapshot=snapshot,
            swap_growth_mb=swap_growth,
        )
        with self._lock:
            self.last_resource = decision
        return decision

    def mark_recycle(self):
        with self._lock:
            self.recycles += 1

    def diagnostics(self) -> dict:
        with self._lock:
            recent = list(self._durations)
            return {
                "completed": self.completed,
                "timeouts": self.timeouts,
                "recycles": self.recycles,
                "rolling_median_s": round(statistics.median(recent), 3) if recent else None,
                "timeout_s": round(self.timeout_seconds(), 3),
                "last_admission": (
                    asdict(self.last_admission) if self.last_admission else None),
                "last_resource": (
                    {
                        **asdict(self.last_resource),
                        "snapshot": asdict(self.last_resource.snapshot),
                    }
                    if self.last_resource else None
                ),
            }
