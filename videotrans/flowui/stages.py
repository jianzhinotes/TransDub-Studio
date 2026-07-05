"""任务消息 → 六阶段步进器映射。无 Qt 依赖。

两层检测：
1) 文本标记（精确）：logs 文本与 trans_create.py 各阶段起点 signal 的 tr() 文本
   比对——生产/消费同 locale 同 tr()，全串相等安全（合成阶段带耗时后缀，用前缀）。
2) 百分比兜底（粗略、只进不退）：set_precent 文本为 "{秒}???{percent}"。
"""

STAGES = ['prepare', 'recogn', 'trans', 'dubbing', 'align', 'assemble']
STAGE_PREPARE, STAGE_RECOGN, STAGE_TRANS, STAGE_DUBBING, STAGE_ALIGN, STAGE_ASSEMBLE = range(6)

# tr 键 → 阶段（键与 trans_create.py:280/595/606/664/694/760 的 signal 一一对应）
_MARKER_KEYS = {
    'kaishishibie': STAGE_RECOGN,
    'starttrans': STAGE_TRANS,
    'kaishitiquhefanyi': STAGE_TRANS,
    'kaishipeiyin': STAGE_DUBBING,
    'duiqicaozuo': STAGE_ALIGN,
    'kaishihebing': STAGE_ASSEMBLE,   # 可能带 " {end_time}" 后缀 → startswith
}

_PREFIX_KEYS = {'kaishihebing'}


def stage_markers(tr=None) -> dict:
    """返回 {本地化文本: (阶段, 是否前缀匹配)}；tr 可注入便于测试。"""
    if tr is None:
        from videotrans.configure.config import tr as _tr
        tr = _tr
    markers = {}
    for key, stage in _MARKER_KEYS.items():
        text = tr(key)
        if text:
            markers[text] = (stage, key in _PREFIX_KEYS)
    return markers


def stage_from_text(text: str, current: int, markers: dict) -> int:
    """根据 logs 文本推进阶段；找不到标记或会回退时保持 current。"""
    if not text:
        return current
    text = text.strip()
    for marker, (stage, is_prefix) in markers.items():
        if (text.startswith(marker) if is_prefix else text == marker):
            return max(stage, current)
    return current


def parse_percent(text: str):
    """解析 set_precent 消息文本 "{秒}???{percent}"，返回 (秒或None, percent或None)。"""
    if not text:
        return None, None
    if '???' in text:
        secs, _, pct = text.partition('???')
        try:
            return int(float(secs)), min(int(float(pct)), 100)
        except (ValueError, TypeError):
            return None, None
    try:
        return None, min(int(float(text)), 100)
    except (ValueError, TypeError):
        return None, None


def stage_from_percent(percent, current: int) -> int:
    """百分比兜底：只用于不回退地粗推阶段。≥90 至少到合成（trans_create 合成期钳制 90-98）。"""
    if percent is None:
        return current
    if percent >= 90:
        return max(STAGE_ASSEMBLE, current)
    return current
