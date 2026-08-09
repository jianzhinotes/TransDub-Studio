"""配音质量预设：把"时间 ↔ 质量"的取舍收敛成一个选择。

背景：配音路径长出了 20 多个 f5tts_* 开关，全部只存在于 cfg.json，
UI 上一个都看不到。用户既无从调整，也无法预测组合行为。

这里只收编**真正在时间和质量之间取舍**的开关。剩下的（内存门槛、看门狗
超时、资源等待、错峰加载等）是机器安全参数，不是用户的质量选择，
保持专家级、留在 cfg.json 里。

生效方式：把预设值写进内存中的 settings，因此 _f5tts.py 里既有的
settings.get('f5tts_xxx') 调用点一处都不用改。选 custom 则完全不干预，
cfg.json 里的值原样生效（专家模式）。
"""
from videotrans.configure.config import logger

PRESET_FAST = 'fast'
PRESET_BALANCED = 'balanced'
PRESET_QUALITY = 'quality'
PRESET_CUSTOM = 'custom'

DEFAULT_PRESET = PRESET_BALANCED

# 预设名 → i18n key（UI 下拉用）
PRESET_LABELS = {
    PRESET_FAST: 'dub_preset_fast',
    PRESET_BALANCED: 'dub_preset_balanced',
    PRESET_QUALITY: 'dub_preset_quality',
    PRESET_CUSTOM: 'dub_preset_custom',
}

PRESETS = {
    # 出小样、赶时间：扩散步数减半、跳过预飞、不做多说话人分离
    PRESET_FAST: {
        'f5tts_nfe': 16,
        'f5tts_preflight_samples': 0,
        'f5tts_multi_speaker': False,
        'f5tts_chinese_anchor': False,
        'f5tts_ref_similarity': 0.6,
        'f5tts_validation_batch_size': 40,
    },
    # 默认：F5 原生步数 + 完整质量链路
    PRESET_BALANCED: {
        'f5tts_nfe': 32,
        'f5tts_preflight_samples': 5,
        'f5tts_multi_speaker': True,
        'f5tts_chinese_anchor': True,
        'f5tts_ref_similarity': 0.75,
        'f5tts_validation_batch_size': 24,
    },
    # 成品交付：更多扩散步数、更严的参考回读、更细的质量核对批次
    PRESET_QUALITY: {
        'f5tts_nfe': 40,
        'f5tts_preflight_samples': 8,
        'f5tts_multi_speaker': True,
        'f5tts_chinese_anchor': True,
        'f5tts_ref_similarity': 0.8,
        'f5tts_validation_batch_size': 16,
    },
}

# 预设覆盖的键集合（并集，用于 UI 提示与测试）
MANAGED_KEYS = tuple(sorted(PRESETS[PRESET_BALANCED]))


def normalize(name) -> str:
    value = str(name or '').strip().lower()
    return value if value in PRESET_LABELS else DEFAULT_PRESET


def current(settings_obj=None) -> str:
    if settings_obj is None:
        from videotrans.configure.config import settings as settings_obj
    return normalize(settings_obj.get('f5tts_preset', DEFAULT_PRESET))


def values_for(name) -> dict:
    """返回该预设覆盖的键值；custom 返回空字典（不干预）。"""
    return dict(PRESETS.get(normalize(name), {}))


def apply(name=None, settings_obj=None) -> dict:
    """把预设写进内存 settings，使既有 settings.get 调用点自动生效。

    custom 时不写任何键，cfg.json 的专家配置原样保留。
    返回实际应用的键值，便于日志与测试。
    """
    if settings_obj is None:
        from videotrans.configure.config import settings as settings_obj
    resolved = normalize(name if name is not None else current(settings_obj))
    applied = values_for(resolved)
    for key, value in applied.items():
        settings_obj[key] = value
    if applied:
        logger.debug(f'配音预设[{resolved}]已应用: {applied}')
    return applied


def describe(name) -> str:
    """一行摘要，给 UI 的 tooltip 用。"""
    values = values_for(name)
    if not values:
        return ''
    return (f"nfe={values['f5tts_nfe']} · "
            f"preflight={values['f5tts_preflight_samples']} · "
            f"multi_speaker={'on' if values['f5tts_multi_speaker'] else 'off'}")
