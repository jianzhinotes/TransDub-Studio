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
    path = path or _default_path()
    entries = load(path)
    changed = False
    for e in entries:
        if e.get('video_path') == video_path:
            e['status'] = status
            changed = True
    if changed:
        try:
            _write(entries, path)
        except OSError:
            pass
