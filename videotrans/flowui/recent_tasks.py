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

# 既无耐久日志、又无 pid 的遗留记录，超过这个时长仍是 running 就判为已中断。
# 现实中最长的单任务（长访谈 + 本地串行配音 + 质量返工）在 1-2 小时量级，
# 留 3 倍余量。新记录都带 pid，走精确判定，不依赖这个阈值。
STALE_RUNNING_S = 6 * 3600


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
    existing = load(path)
    previous = next(
        (e for e in existing if e.get('video_path') == entry.get('video_path')), None)
    if previous:
        # 重跑同一个视频不该丢掉上一轮回填的工程定位信息，
        # 否则"重新编辑"入口和状态自愈都会失效
        for key in ('project_dir', 'run_state_file', 'target_dir'):
            if not entry.get(key) and previous.get(key):
                entry[key] = previous[key]
    entries = [e for e in existing if e.get('video_path') != entry.get('video_path')]
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


def remove(video_path: str, path: str = None) -> list:
    """删除一条最近任务记录。"""
    path = path or _default_path()
    entries = [e for e in load(path) if e.get('video_path') != video_path]
    try:
        _write(entries, path)
    except OSError:
        pass
    return entries


def prune(statuses=(STATUS_SUCCEED,), path: str = None) -> list:
    """清理指定状态的记录（默认清掉已完成的）。"""
    path = path or _default_path()
    targets = set(statuses)
    entries = [e for e in load(path) if e.get('status') not in targets]
    try:
        _write(entries, path)
    except OSError:
        pass
    return entries


def _backfill_paths(entry: dict) -> bool:
    """补全空的 target_dir，让三级查找重新能命中。

    早期写入路径会留下 target_dir='' 的记录，导致 run_state 找不到、
    状态永远冻结在"进行中"，"重新编辑"也永远出不来。
    """
    if entry.get('target_dir'):
        return False
    if entry.get('project_dir'):
        entry['target_dir'] = str(Path(entry['project_dir']).parent)
    elif entry.get('run_state_file'):
        entry['target_dir'] = str(Path(entry['run_state_file']).parent.parent)
    elif entry.get('video_path'):
        entry['target_dir'] = (Path(entry['video_path']).parent / '_video_out').as_posix()
    else:
        return False
    return True


def reconcile_run_states(path: str = None, now: float = None) -> list:
    """Refresh stale recent-task status from durable per-project journals."""
    path = path or _default_path()
    now = int(now if now is not None else time.time())
    entries = load(path)
    changed = False
    from videotrans.dub.run_state import (
        effective_status, find_run_state, load_run_state, process_is_alive)
    for entry in entries:
        video_path = entry.get('video_path') or ''
        changed = _backfill_paths(entry) or changed
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
        # 没有耐久日志可查时的僵尸兜底：应用崩溃在 begin_run 之前，
        # 或历史脏记录。优先用条目自带的 pid 精确判定。
        if not payload and entry.get('status') == STATUS_RUNNING:
            pid = entry.get('pid')
            if pid is not None and not process_is_alive(pid):
                entry['status'] = STATUS_STOPPED
                entry['stale_reason'] = 'owner_gone'
                changed = True
            elif pid is None and (now - int(entry.get('ts') or 0)) > STALE_RUNNING_S:
                entry['status'] = STATUS_STOPPED
                entry['stale_reason'] = 'timeout'
                changed = True
    if changed:
        try:
            _write(entries, path)
        except OSError:
            pass
    return entries
