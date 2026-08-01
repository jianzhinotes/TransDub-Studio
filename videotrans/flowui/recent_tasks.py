"""最近任务持久化：recent_tasks.json（与 params.json 同目录）。无 Qt 依赖。

列表新入在前、按 video_path 去重置顶、上限 MAX_ENTRIES；
文件损坏时容错返回空表；写入走 tmp+rename 降低截断风险。
"""
import json
import os
import time
from pathlib import Path

MAX_ENTRIES = 20

STATUS_RUNNING = 'running'
STATUS_SUCCEED = 'succeed'
STATUS_ERROR = 'error'
STATUS_STOPPED = 'stopped'


def _default_path() -> str:
    from videotrans.configure.config import ROOT_DIR
    return f'{ROOT_DIR}/videotrans/recent_tasks.json'


def load(path: str = None) -> list:
    path = path or _default_path()
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _write(entries: list, path: str) -> None:
    tmp = f'{path}.tmp'
    Path(tmp).write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def append(entry: dict, path: str = None) -> list:
    """新增/置顶一条记录并落盘；entry 至少含 video_path。返回最新列表。"""
    path = path or _default_path()
    entry = dict(entry)
    entry.setdefault('ts', int(time.time()))
    entry.setdefault('status', STATUS_RUNNING)
    entries = [e for e in load(path) if e.get('video_path') != entry.get('video_path')]
    entries.insert(0, entry)
    entries = entries[:MAX_ENTRIES]
    try:
        _write(entries, path)
    except OSError:
        pass
    return entries


def update_status(video_path: str, status: str, path: str = None) -> None:
    update_fields(video_path, path=path, status=status)


def update_fields(video_path: str, path: str = None, **fields) -> None:
    """更新某条最近任务的任意字段（如 status、project_dir）。"""
    path = path or _default_path()
    entries = load(path)
    changed = False
    for e in entries:
        if e.get('video_path') == video_path:
            e.update(fields)
            changed = True
    if changed:
        try:
            _write(entries, path)
        except OSError:
            pass


def reconcile_run_states(path: str = None) -> list:
    """Refresh stale recent-task status from durable per-project journals."""
    path = path or _default_path()
    entries = load(path)
    changed = False
    from videotrans.dub.run_state import effective_status, find_run_state, load_run_state
    for entry in entries:
        video_path = entry.get('video_path') or ''
        # Fast paths first; recursive discovery is only needed once for old or
        # early records whose predicted project location was not exact.
        state_file = entry.get('run_state_file')
        if state_file and not Path(state_file).is_file():
            state_file = None
        project_dir = entry.get('project_dir')
        if not state_file and project_dir:
            candidate = Path(project_dir) / 'run_state.json'
            state_file = str(candidate) if candidate.is_file() else None
        if not state_file:
            state_file = find_run_state(entry.get('target_dir'), Path(video_path).stem)
        payload = load_run_state(state_file) if state_file else None
        status = effective_status(payload)
        mapped = {
            'completed': STATUS_SUCCEED,
            'failed': STATUS_ERROR,
            'interrupted': STATUS_STOPPED,
            'running': STATUS_RUNNING,
        }.get(status)
        if mapped and entry.get('status') != mapped:
            entry['status'] = mapped
            changed = True
        if state_file and entry.get('run_state_file') != state_file:
            entry['run_state_file'] = state_file
            changed = True
    if changed:
        try:
            _write(entries, path)
        except OSError:
            pass
    return entries
