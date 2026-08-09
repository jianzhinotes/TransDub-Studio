"""配音期遥测读取与 ETA 估算。

只消费结构化 JSON，不解析任何日志文案——后端改文案不会让这里失效。
纯函数、无 Qt 依赖，可直接单测。
"""
import json
from pathlib import Path

# 配音阶段允许推进的百分点跨度。后端 precent 在整个配音期不动
# （只在阶段边界 +3/+5），所以这段要由段数插值补上。
DUB_BAND_SPAN = 55
# 硬天花板：越过 90 会让 stage_from_percent 把阶段错误推到"合成"
DUB_BAND_CEIL = 85
# 少于这么多实测段就不用均值，改用 supervisor 的滚动中位数
MIN_SAMPLES = 3
EWMA_ALPHA = 0.3


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def read_dubbing_telemetry(cache_dir=None, project_dir=None):
    """合并 tts_progress.json（分子+分母）与 synthesis_supervisor.json（中位数/异常）。

    cache_folder 会在任务结束时被清理，届时回退到项目目录下的断点副本。
    两者都读不到返回 None。
    """
    from videotrans.dub.performance_report import TTS_PROGRESS_FILE

    progress = supervisor = None
    if cache_dir:
        progress = _read_json(Path(cache_dir) / TTS_PROGRESS_FILE)
        supervisor = _read_json(Path(cache_dir) / 'synthesis_supervisor.json')
    if supervisor is None and project_dir:
        supervisor = _read_json(
            Path(project_dir) / 'checkpoints' / 'dubbing' / 'supervisor.json')
    if progress is None and project_dir:
        manifest = _read_json(
            Path(project_dir) / 'checkpoints' / 'dubbing' / 'manifest.json')
        if manifest:
            # 断点清单只有分子没有分母：能显计数，不能算 ETA
            progress = {'completed': len(manifest.get('entries') or {}), 'total': 0}
    if not progress and not supervisor:
        return None
    progress = progress or {}
    supervisor = supervisor or {}
    completed = int(progress.get('completed') or supervisor.get('completed') or 0)
    return {
        'total': int(progress.get('total') or 0),
        'completed': completed,
        'prefilled': int(progress.get('prefilled') or 0),
        'elapsed_s': float(progress.get('elapsed_s') or 0.0),
        'median_s': supervisor.get('rolling_median_s'),
        'timeout_s': supervisor.get('timeout_s'),
        'timeouts': int(supervisor.get('timeouts') or 0),
        'recycles': int(supervisor.get('recycles') or 0),
        'finished': progress.get('status') == 'finished',
    }


def estimate_eta(tel, previous_rate=None):
    """→ (剩余秒数|None, 平滑后的速率|None)。

    速率取墙钟秒/段：池化后端的并发天然算在里面，不必知道线程数。
    缓存命中的行（prefilled）耗时≈0，必须从分子里剔除，否则速率被严重低估。
    """
    total = tel.get('total') or 0
    completed = tel.get('completed') or 0
    if total <= 0:
        return None, previous_rate
    synthesized = max(completed - (tel.get('prefilled') or 0), 0)
    remaining = max(total - completed, 0)
    rate = None
    if synthesized >= MIN_SAMPLES and tel.get('elapsed_s', 0) > 0:
        rate = tel['elapsed_s'] / synthesized
    elif tel.get('median_s'):
        # F5 串行且逐段落盘，滚动中位数从第 1 段就可用，且抗单段超时离群值
        rate = float(tel['median_s'])
    if not rate or rate <= 0:
        return None, previous_rate
    rate = rate if previous_rate is None else (
        EWMA_ALPHA * rate + (1 - EWMA_ALPHA) * previous_rate)
    return rate * remaining, rate


def quantize_eta(seconds):
    """量化到人能接受的粒度，避免每次刷新都抖一个新数字。"""
    if seconds is None:
        return None
    value = max(float(seconds), 0.0)
    if value < 120:
        return round(value / 30) * 30 or 30
    if value < 3600:
        return round(value / 60) * 60
    return round(value / 300) * 300


def dubbing_percent(base, completed, total, span=DUB_BAND_SPAN, ceil=DUB_BAND_CEIL):
    """在 [base, min(base+span, ceil)] 内按段数线性插值。

    对 completed 单调递增，与 UI 侧 set_percent 的 max() 保护天然兼容。
    """
    base = int(base or 0)
    if not total or total <= 0:
        return base
    end = min(base + span, ceil)
    if end <= base:
        return base
    frac = min(max(completed / total, 0.0), 1.0)
    return int(base + (end - base) * frac)


def format_duration(seconds) -> str:
    seconds = max(int(seconds or 0), 0)
    hours, remain = divmod(seconds, 3600)
    minutes, secs = divmod(remain, 60)
    if hours:
        return f'{hours}小时{minutes}分'
    if minutes:
        return f'{minutes}分{secs}秒'
    return f'{secs}秒'
