"""mlx-whisper 识别 worker（Apple Silicon 专用，Metal GPU 加速）。

与 stt_fun.faster_whisper 同构：VAD 预切分时逐段识别（时间戳直接取 VAD 边界），
否则整文件识别取词级时间戳后复用 stt_fun._resegment 断句。
仅在 settings['use_mlx_whisper'] 开启且平台满足时由 _whisper.py 调用；
任何失败由调用方回退到 faster-whisper，本函数只需如实返回 (False, 错误)。
"""
import json
import traceback
from pathlib import Path
from typing import List, Tuple, Union

from videotrans.configure.config import logger
from videotrans.task.taskcfg import SrtItem
from videotrans.util import tools


def _clip_samples(audio, start_ms, end_ms):
    """切出 [start_ms, end_ms) 并转为 mlx 需要的 16k 单声道 float32。"""
    import numpy as np
    clip = audio[start_ms:end_ms].set_frame_rate(16000).set_channels(1).set_sample_width(2)
    return np.array(clip.get_array_of_samples()).astype('float32') / 32768.0


def mlx_whisper_fun(
        *,
        prompt=None,
        detect_language=None,
        model_dir=None,
        logs_file=None,
        speech_timestamps=None,
        audio_file=None,
        jianfan=False,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        temperature=None,
        max_speech_ms=6000,
        subtitle_srt=None,
        **_ignored,
) -> Tuple[Union[List[SrtItem], bool], Union[str, None]]:
    import zhconv
    import mlx_whisper
    from videotrans.process.stt_fun import _resegment, _write_log

    try:
        lang = detect_language.split('-')[0] if detect_language and detect_language != 'auto' else None
        if lang == 'fil':
            lang = 'tl'
        common = dict(
            path_or_hf_repo=model_dir,
            language=lang,
            initial_prompt=prompt or None,
            condition_on_previous_text=condition_on_previous_text,
            no_speech_threshold=no_speech_threshold,
            verbose=None,
        )
        if temperature and not isinstance(temperature, (list, tuple)) \
                and not str(temperature).startswith(('[', '(')):
            common['temperature'] = float(temperature)

        if speech_timestamps and isinstance(speech_timestamps, str):
            speech_timestamps = json.loads(Path(speech_timestamps).read_text(encoding='utf-8'))

        raws: List[SrtItem] = []
        if speech_timestamps:
            # VAD 已断句：逐段识别，时间戳直接用 VAD 边界（与 faster 的批量路径同语义）
            from pydub import AudioSegment
            _write_log(logs_file, json.dumps({"type": "logs", "text": 'mlx-whisper transcribe by VAD clips...'}))
            audio = AudioSegment.from_file(audio_file)
            i = 0
            for st, en in speech_timestamps:
                result = mlx_whisper.transcribe(_clip_samples(audio, st, en),
                                                word_timestamps=False, **common)
                text = (result.get('text') or '').strip()
                if not text:
                    continue
                if jianfan:
                    text = zhconv.convert(text, 'zh-hans')
                i += 1
                tmp = SrtItem(text=text, start_time=int(st), end_time=int(en))
                tmp['startraw'] = tools.ms_to_time_string(ms=tmp['start_time'])
                tmp['endraw'] = tools.ms_to_time_string(ms=tmp['end_time'])
                tmp['time'] = f"{tmp['startraw']} --> {tmp['endraw']}"
                raws.append(tmp)
                _write_log(logs_file, json.dumps({"type": "subtitle", "text": f'[{i}] {text}\n'}))
        else:
            # 整文件识别取词级时间戳，复用 stt_fun 的断句逻辑
            _write_log(logs_file, json.dumps({"type": "logs", "text": 'mlx-whisper transcribe word_timestamps...'}))
            result = mlx_whisper.transcribe(audio_file, word_timestamps=True, **common)
            texts = []
            for seg in result.get('segments') or []:
                if not (seg.get('text') or '').strip():
                    continue
                texts.append({
                    "text": seg['text'],
                    "start": seg['start'],
                    "end": seg['end'],
                    "words": [{'word': w['word'], 'start': w['start'], 'end': w['end']}
                              for w in seg.get('words') or []],
                })
            if not texts:
                return False, "mlx-whisper returned no transcription results."
            raws = _resegment(texts, result.get('language') or lang, max_speech_ms, logs_file)
            if jianfan and raws:
                for it in raws:
                    it['text'] = zhconv.convert(it['text'], 'zh-hans')

        if not raws:
            return False, "mlx-whisper returned no transcription results."
        if subtitle_srt:
            Path(subtitle_srt).write_text(
                "\n\n".join(f'{i + 1}\n{it["startraw"]} --> {it["endraw"]}\n{it["text"]}'
                            for i, it in enumerate(raws)),
                encoding="utf-8")
        return raws, None
    except BaseException as e:
        msg = traceback.format_exc()
        logger.exception(e, exc_info=True)
        return False, f'{e}:{msg}'
