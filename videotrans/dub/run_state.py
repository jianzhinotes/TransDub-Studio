"""Atomic stage journal for resumable long-video tasks."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from videotrans.dub.store import atomic_write_json


RUN_STATE_FILE = "run_state.json"
RUN_STATE_VERSION = 1


def load_run_state(path):
    """Read a journal from either a project directory or run_state.json path."""
    path = Path(path)
    if path.is_dir() or path.suffix != ".json":
        path = path / RUN_STATE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def find_run_state(root_dir, video_stem):
    """Locate an early-created journal even before project.json is available."""
    if not root_dir or not video_stem:
        return None
    root = Path(root_dir)
    if not root.is_dir():
        return None
    direct = root / f"{video_stem}.tdproj" / RUN_STATE_FILE
    if direct.is_file():
        return str(direct)
    for path in root.glob(f"**/{video_stem}.tdproj/{RUN_STATE_FILE}"):
        return str(path)
    return None


def process_is_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def effective_status(payload) -> str:
    """Return ``interrupted`` when a running journal's owner no longer exists."""
    if not isinstance(payload, dict):
        return ""
    status = str(payload.get("status") or "")
    if status == "running" and not process_is_alive(payload.get("pid")):
        return "interrupted"
    return status


def mark_stale_payload_interrupted(payload, *, stopped_at=None, reason="") -> bool:
    """Convert a dead owner's in-memory ``running`` payload to interrupted.

    This schema-compatible helper works for both run_state.json and
    performance_report.json. The caller decides whether to persist it.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "running" or process_is_alive(payload.get("pid")):
        return False
    stopped_at = int(stopped_at or payload.get("updated_at") or time.time())
    for stage in payload.get("stages", {}).values():
        if stage.get("status") != "running":
            continue
        started = int(stage.get("started_at") or stopped_at)
        stage["status"] = "interrupted"
        stage["finished_at"] = stopped_at
        stage["duration_s"] = max(stopped_at - started, 0)
        if reason:
            stage["error"] = str(reason)[:1000]
    payload["status"] = "interrupted"
    payload["current_stage"] = ""
    payload["finished_at"] = stopped_at
    started = int(payload.get("started_at") or stopped_at)
    if payload.get("duration_s") is None:
        payload["duration_s"] = max(stopped_at - started, 0)
    payload["last_error"] = str(reason or "运行进程已退出，任务已中断")[:2000]
    payload["updated_at"] = stopped_at
    return True


def repair_stale_project_run(project_dir, reason="") -> int:
    """Persist stale-run repair for the journal and performance report."""
    root = Path(project_dir)
    repaired = 0
    for filename in (RUN_STATE_FILE, "performance_report.json"):
        path = root / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stopped_at = max(
                int(payload.get("updated_at") or 0),
                int(path.stat().st_mtime),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if mark_stale_payload_interrupted(
                payload, stopped_at=stopped_at, reason=reason):
            atomic_write_json(path, payload)
            repaired += 1
    return repaired


class RunStateStore:
    def __init__(self, project_dir):
        self.root = Path(project_dir)
        self.path = self.root / RUN_STATE_FILE
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return {
            "schema_version": RUN_STATE_VERSION,
            "run_id": "",
            "status": "idle",
            "current_stage": "",
            "stages": {},
            "updated_at": 0,
        }

    def _save(self):
        self.data["updated_at"] = int(time.time())
        atomic_write_json(self.path, self.data)

    def begin_run(self, run_id: str):
        with self._lock:
            now = int(time.time())
            for stage in self.data.get("stages", {}).values():
                old_status = str(stage.get("status") or "")
                stage["previous_status"] = (
                    "interrupted" if old_status == "running" else old_status)
                stage["status"] = "pending"
                stage["started_at"] = None
                stage["finished_at"] = None
                stage["duration_s"] = None
                stage["error"] = ""
            self.data.update({
                "run_id": str(run_id or ""),
                "pid": os.getpid(),
                "status": "running",
                "current_stage": "",
                "started_at": now,
                "finished_at": None,
                "last_error": "",
            })
            self._save()

    def start_stage(self, name: str, metadata=None):
        with self._lock:
            now = int(time.time())
            old = self.data.setdefault("stages", {}).get(name) or {}
            self.data["stages"][name] = {
                **old,
                "status": "running",
                "attempt": int(old.get("attempt") or 0) + 1,
                "started_at": now,
                "finished_at": None,
                "duration_s": None,
                "error": "",
                "metadata": dict(metadata or {}),
            }
            self.data["current_stage"] = name
            self._save()

    def finish_stage(self, name: str, *, status="completed", metadata=None, error=""):
        with self._lock:
            now = int(time.time())
            stage = self.data.setdefault("stages", {}).setdefault(name, {})
            started = int(stage.get("started_at") or now)
            stage.update({
                "status": status,
                "finished_at": now,
                "duration_s": max(now - started, 0),
                "error": str(error or "")[:1000],
            })
            if metadata:
                stage["metadata"] = {**stage.get("metadata", {}), **dict(metadata)}
            if self.data.get("current_stage") == name:
                self.data["current_stage"] = ""
            self._save()

    def complete_stage(self, name: str, metadata=None):
        self.finish_stage(name, status="completed", metadata=metadata)

    def fail_stage(self, name: str, error):
        self.finish_stage(name, status="failed", error=error)

    def finish_run(self, status: str, error="", artifacts=None):
        with self._lock:
            now = int(time.time())
            if status != "completed":
                for stage in self.data.get("stages", {}).values():
                    if stage.get("status") == "running":
                        started = int(stage.get("started_at") or now)
                        stage["status"] = "interrupted" if status == "interrupted" else "failed"
                        stage["finished_at"] = now
                        stage["duration_s"] = max(now - started, 0)
                        if error:
                            stage["error"] = str(error)[:1000]
            self.data["status"] = status
            self.data["current_stage"] = ""
            self.data["finished_at"] = now
            self.data["last_error"] = str(error or "")[:2000]
            if artifacts is not None:
                self.data["artifacts"] = dict(artifacts)
            self._save()
