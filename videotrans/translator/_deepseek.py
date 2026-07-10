# -*- coding: utf-8 -*-
from dataclasses import dataclass
from videotrans.configure.config import params, settings
from videotrans.translator._openaicompat import OpenAICampat

# 单次请求最多发送的字幕块数。整份字幕一次性发给 DeepSeek 会撑爆模型上下文，
# 长视频报 "content too long / 超出最大 token"。按窗口分批，每批仍带足够上下文，
# 又不会溢出。可在配置中用 deepseek_srt_batch 覆盖。
DEEPSEEK_SRT_BATCH = 100


@dataclass
class DeepSeek(OpenAICampat):

    def __post_init__(self):
        self.ainame="deepseek"
        self.model_name = params.get('deepseek_model', "deepseek-v4-flash")
        self.api_url = 'https://api.deepseek.com/v1/'
        self.api_key = params.get('deepseek_key', '')
        # 输出 token 预留不能接近模型上下文上限，否则留给输入的空间不足，
        # 连短字幕都会被拒（"content too long"）。默认 16384 兼顾 thinking 模式，
        # 可在配置中用 deepseek_max_token 调大。
        self.max_tokens=int(params.get('deepseek_max_token') or 16384)
        self.reasoning_effort="high" if params.get('deepseek_thinking') else None
        self.extra_body={"thinking": {"type": "enabled" if params.get('deepseek_thinking') else "disabled"}}
        super().__post_init__()
        # 发送完整 SRT 片段以利用上下文（让 ASR 切碎的句子借助前后文理解），
        # 但限制单批行数，避免长视频一次性发送而超出上下文长度。
        if self.aisendsrt and self.text_list:
            max_lines = int(settings.get('deepseek_srt_batch') or DEEPSEEK_SRT_BATCH)
            self.trans_thread = max(1, min(len(self.text_list), max_lines))
