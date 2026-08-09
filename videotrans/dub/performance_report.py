"""Low-frequency performance telemetry for long dubbing runs.

Only process/resource totals and stage timing are recorded.  No subtitle text,
API credentials, prompts, or media content are included.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from pathlib import Path

import psutil

from videotrans.dub.store import atomic_write_json


PERFORMANCE_FILE = "performance_report.json"
TTS_RUN_STATS_FILE = "tts_run_stats.json"
# 配音期逐段落盘的 live 进度（分子+分母），供 UI 计算真实 ETA
TTS_PROGRESS_FILE = "tts_progress.json"
PERFORMANCE_SCHEMA_VERSION = 2


class PerformanceReporter:
    def __init__(self, project_dir, *, sample_interval_s: float = 2.0):
        self.root = Path(project_dir)
        self.path = self.root / PERFORMANCE_FILE
        self.sample_interval_s = max(float(sample_interval_s), 0.25)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._run_started = None
        self._stage_started = {}
        self._cpu_times = {}
        self.data = {}

    def start(self, run_id: str, context=None, *, background=True):
        total_memory_mb = round(psutil.virtual_memory().total / 1024 / 1024, 1)
        with self._lock:
            self._run_started = time.monotonic()
            self.data = {
                "schema_version": PERFORMANCE_SCHEMA_VERSION,
                "run_id": str(run_id or ""),
                "pid": os.getpid(),
                "status": "running",
                "started_at": int(time.time()),
                "finished_at": None,
                "duration_s": None,
                "system": {
                    "platform": platform.system(),
                    "machine": platform.machine(),
                    "logical_cpus": psutil.cpu_count(logical=True),
                    "total_memory_mb": total_memory_mb,
                },
                "context": dict(context or {}),
                "resources": {
                    "peak_process_tree_rss_mb": 0.0,
                    "peak_process_tree_cpu_percent": 0.0,
                    "peak_system_memory_percent": 0.0,
                    "lowest_available_memory_mb": total_memory_mb,
                    "peak_system_swap_used_mb": 0,
                    "peak_load_per_cpu": 0.0,
                    "peak_pressure": "normal",
                    "pressure_samples": {
                        "normal": 0, "elevated": 0, "high": 0, "critical": 0,
                    },
                    "samples": 0,
                },
                "stages": {},
            }
            self._sample_locked()
            self._save_locked()
        if background:
            self._thread = threading.Thread(
                target=self._sample_loop,
                name="transdub-performance-sampler",
                daemon=True,
            )
            self._thread.start()

    def _process_tree(self):
        try:
            root = psutil.Process(os.getpid())
            return [root, *root.children(recursive=True)]
        except (psutil.Error, OSError):
            return []

    def _sample_locked(self):
        rss = 0
        cpu = 0.0
        now = time.monotonic()
        live_pids = set()
        for process in self._process_tree():
            try:
                rss += process.memory_info().rss
                times = process.cpu_times()
                total_cpu = float(times.user + times.system)
                live_pids.add(process.pid)
                previous = self._cpu_times.get(process.pid)
                if previous and now > previous[1]:
                    cpu += max((total_cpu - previous[0]) / (now - previous[1]) * 100, 0)
                self._cpu_times[process.pid] = (total_cpu, now)
            except (psutil.Error, OSError):
                continue
        self._cpu_times = {
            pid: value for pid, value in self._cpu_times.items() if pid in live_pids
        }
        from videotrans.util.resource_governor import resource_snapshot

        snapshot = resource_snapshot()
        memory_percent = snapshot.memory_percent
        resources = self.data.setdefault("resources", {})
        resources["peak_process_tree_rss_mb"] = max(
            float(resources.get("peak_process_tree_rss_mb") or 0),
            round(rss / 1024 / 1024, 1),
        )
        resources["peak_process_tree_cpu_percent"] = max(
            float(resources.get("peak_process_tree_cpu_percent") or 0), round(cpu, 1))
        resources["peak_system_memory_percent"] = max(
            float(resources.get("peak_system_memory_percent") or 0),
            round(memory_percent, 1),
        )
        if snapshot.available_mb:
            resources["lowest_available_memory_mb"] = min(
                float(resources.get("lowest_available_memory_mb") or snapshot.available_mb),
                snapshot.available_mb,
            )
        resources["peak_system_swap_used_mb"] = max(
            int(resources.get("peak_system_swap_used_mb") or 0), snapshot.swap_used_mb)
        resources["peak_load_per_cpu"] = max(
            float(resources.get("peak_load_per_cpu") or 0), snapshot.load_per_cpu)
        pressure_order = {"normal": 0, "elevated": 1, "high": 2, "critical": 3}
        old_pressure = str(resources.get("peak_pressure") or "normal")
        if pressure_order.get(snapshot.pressure, 0) > pressure_order.get(old_pressure, 0):
            resources["peak_pressure"] = snapshot.pressure
        pressure_samples = resources.setdefault("pressure_samples", {})
        pressure_samples[snapshot.pressure] = int(
            pressure_samples.get(snapshot.pressure) or 0) + 1
        resources["samples"] = int(resources.get("samples") or 0) + 1

    def _sample_loop(self):
        last_save = time.monotonic()
        while not self._stop.wait(self.sample_interval_s):
            with self._lock:
                self._sample_locked()
                if time.monotonic() - last_save >= 15:
                    self._save_locked()
                    last_save = time.monotonic()

    def _save_locked(self):
        atomic_write_json(self.path, self.data)

    def start_stage(self, name: str, metadata=None):
        with self._lock:
            self._stage_started[name] = time.monotonic()
            old = self.data.setdefault("stages", {}).get(name) or {}
            self.data["stages"][name] = {
                **old,
                "status": "running",
                "attempt": int(old.get("attempt") or 0) + 1,
                "started_at": int(time.time()),
                "finished_at": None,
                "duration_s": None,
                "metadata": dict(metadata or {}),
                "error": "",
            }
            self._save_locked()

    def finish_stage(self, name: str, *, status="completed", metadata=None, error=""):
        with self._lock:
            started = self._stage_started.pop(name, time.monotonic())
            stage = self.data.setdefault("stages", {}).setdefault(name, {})
            stage.update({
                "status": status,
                "finished_at": int(time.time()),
                "duration_s": round(max(time.monotonic() - started, 0), 3),
                "error": str(error or "")[:1000],
            })
            if metadata:
                stage["metadata"] = {**stage.get("metadata", {}), **dict(metadata)}
            if name == "dubbing":
                audio_duration = float(stage.get("metadata", {}).get("audio_duration_s") or 0)
                if audio_duration > 0:
                    stage["metadata"]["real_time_factor"] = round(
                        stage["duration_s"] / audio_duration, 3)
            self._sample_locked()
            self._save_locked()

    def finish(self, status: str, error=""):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=min(self.sample_interval_s + 0.5, 3.0))
        with self._lock:
            self._sample_locked()
            now = int(time.time())
            if status != "completed":
                for name, stage in self.data.get("stages", {}).items():
                    if stage.get("status") == "running":
                        started = self._stage_started.pop(name, time.monotonic())
                        stage["status"] = (
                            "interrupted" if status == "interrupted" else "failed")
                        stage["finished_at"] = now
                        stage["duration_s"] = round(
                            max(time.monotonic() - started, 0), 3)
                        if error:
                            stage["error"] = str(error)[:1000]
            self.data["status"] = str(status)
            self.data["finished_at"] = now
            self.data["duration_s"] = round(
                max(time.monotonic() - (self._run_started or time.monotonic()), 0), 3)
            self.data["last_error"] = str(error or "")[:2000]
            self._save_locked()


def load_performance_report(path):
    import json
    path = Path(path)
    if path.is_dir() or path.suffix != ".json":
        path = path / PERFORMANCE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        from videotrans.dub.run_state import mark_stale_payload_interrupted
        mark_stale_payload_interrupted(
            payload,
            stopped_at=max(
                int(payload.get("updated_at") or 0),
                int(path.stat().st_mtime),
            ),
        )
        return payload
    except (OSError, json.JSONDecodeError, TypeError):
        return None
