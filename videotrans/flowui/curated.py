"""Flow UI 精选渠道清单与配置状态判断。无 Qt 依赖。

渠道 id 对应各包 _ID_NAME_DICT 的索引（tests/test_flow_curated.py 作为
上游重排渠道 id 时的金丝雀）。key_name 为 None 即免费/无需密钥。
"""

# 翻译：Google(免费)、DeepSeek、ChatGPT、Gemini、DeepL、本地LLM
CURATED_TRANS = [0, 4, 3, 5, 16, 8]
# 配音：Edge-TTS(免费)、ElevenLabs、Azure-TTS、OpenAI-TTS、F5-TTS(克隆,需本地服务)
CURATED_TTS = [0, 20, 28, 18, 8]
# 识别：faster-whisper(本地)、OpenAI Speech-to-Text、Gemini
CURATED_RECOGN = [0, 5, 6]

KIND_RECOGN = 'recogn'
KIND_TRANS = 'trans'
KIND_TTS = 'tts'

CURATED = {
    KIND_RECOGN: CURATED_RECOGN,
    KIND_TRANS: CURATED_TRANS,
    KIND_TTS: CURATED_TTS,
}


def id_name_dict(kind: str) -> dict:
    if kind == KIND_TRANS:
        from videotrans.translator import _ID_NAME_DICT
    elif kind == KIND_TTS:
        from videotrans.tts import _ID_NAME_DICT
    elif kind == KIND_RECOGN:
        from videotrans.recognition import _ID_NAME_DICT
    else:
        raise ValueError(kind)
    return _ID_NAME_DICT


def provider_for(kind: str, channel_id: int):
    """返回 ChannelProvider(name, imp, key_name, win)。"""
    return id_name_dict(kind)[channel_id]


def is_configured(provider, params_get) -> bool:
    """镜像 is_input_api 的判定但无弹窗副作用：无 key_name 即免费可用。"""
    if not provider.key_name:
        return True
    return bool(params_get(provider.key_name))


def is_free(provider) -> bool:
    return not provider.key_name
