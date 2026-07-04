"""Dubbing Studio 的纯编辑逻辑：无 Qt 依赖，可独立单测。

时间字段约定（毫秒 int）：拖块编辑的载荷字段是 start_time/end_time；
start_time_source/end_time_source 会被 align(_rate.py) 用 start_time 重算，
这里同步它们只为保持数据一致；startraw/endraw 供界面与最终 SRT 显示。
"""
import copy
import json
from pathlib import Path

MIN_DURATION_MS = 200
EDGE_PX = 6

STATUS_NO_AUDIO = 'no_audio'
STATUS_EXCEEDED = 'exceeded'
STATUS_SHORTENED = 'shortened'
STATUS_OK = 'ok'


def ms_to_srt(ms: int) -> str:
    ms = max(int(ms), 0)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, msec = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{msec:03d}'


def clamp_block(items, idx, start_ms, end_ms, mode, duration_ms,
                min_dur=MIN_DURATION_MS) -> tuple:
    """把拖动候选时间钳制到合法区间：不与邻居重叠、不出界、不短于 min_dur。

    mode: 'move'(保持长度) | 'left'(固定右端) | 'right'(固定左端)。
    返回 (start_ms, end_ms)。
    """
    lo = int(items[idx - 1]['end_time']) if idx > 0 else 0
    hi = int(items[idx + 1]['start_time']) if idx < len(items) - 1 else int(duration_ms)
    # 邻居本身可能已越界/重叠（历史数据），保证区间可用
    hi = max(hi, lo + min_dur)

    start_ms, end_ms = int(start_ms), int(end_ms)
    if mode == 'move':
        length = end_ms - start_ms
        length = min(max(length, min_dur), hi - lo)
        start_ms = min(max(start_ms, lo), hi - length)
        return start_ms, start_ms + length
    if mode == 'left':
        end_ms = int(items[idx]['end_time'])
        start_ms = min(max(start_ms, lo), end_ms - min_dur)
        return start_ms, end_ms
    if mode == 'right':
        start_ms = int(items[idx]['start_time'])
        end_ms = min(max(end_ms, start_ms + min_dur), hi)
        return start_ms, end_ms
    raise ValueError(f'unknown mode: {mode}')


def compute_status(item) -> tuple:
    """返回 (kind, dubbing_s, diff)。判定逻辑与旧校对弹窗 _precompute_data 一致：
    diff = 配音实际时长 - 字幕槽位时长（秒）。"""
    duration = (int(item['end_time']) - int(item['start_time'])) / 1000.0
    dubbing = float(item.get('dubbing_s', 0.0) or 0.0)
    diff = round(dubbing - duration, 3)
    if dubbing <= 0.0:
        return STATUS_NO_AUDIO, dubbing, diff
    if diff > 0:
        return STATUS_EXCEEDED, dubbing, diff
    if diff < 0:
        return STATUS_SHORTENED, dubbing, diff
    return STATUS_OK, dubbing, diff


def sync_time_fields(item, start_ms: int, end_ms: int) -> None:
    """写入新起止时间并同步全部派生字段。"""
    start_ms, end_ms = int(start_ms), int(end_ms)
    item['start_time'] = start_ms
    item['end_time'] = end_ms
    item['start_time_source'] = start_ms
    item['end_time_source'] = end_ms
    item['startraw'] = ms_to_srt(start_ms)
    item['endraw'] = ms_to_srt(end_ms)


def serializable(queue_tts) -> list:
    """深拷贝并剥离下划线前缀的界面辅助键（如旧弹窗的 _duration/_msg）。"""
    return [{k: copy.deepcopy(v) for k, v in it.items() if not k.startswith('_')}
            for it in queue_tts]


def dump_queue(queue_tts, cache_folder: str) -> str:
    path = Path(cache_folder) / 'queue_tts.json'
    path.write_text(json.dumps(serializable(queue_tts), ensure_ascii=False),
                    encoding='utf-8')
    return str(path)
