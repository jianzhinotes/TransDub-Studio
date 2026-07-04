"""把逐句配音片段(queue_tts)按字幕起始时间拼成单个可 seek 的预览 wav。

无 Qt 依赖。间隙自然呈现为静音、重叠为叠加，波形轨直接反映时间轴问题。
"""
import logging
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger('VideoTrans')

PREVIEW_NAME = 'dub_preview.wav'
_FRAME_RATE = 16000


def preview_path(cache_folder: str) -> Path:
    return Path(cache_folder) / PREVIEW_NAME


def invalidate_dub_preview(cache_folder: str) -> None:
    # 重新配音某行后调用，下次打开预览时重建
    preview_path(cache_folder).unlink(missing_ok=True)


def build_dub_preview_wav(queue_tts, duration_ms: int, cache_folder: str,
                          progress_cb=None) -> str:
    """queue_tts 每项需支持 ['start_time'](ms) 与 ['filename']；返回生成的 wav 路径。

    已存在则直接复用（用 invalidate_dub_preview 强制重建）。
    """
    out = preview_path(cache_folder)
    if out.exists():
        return str(out)

    segments = []
    total_ms = int(duration_ms)
    for item in queue_tts:
        filename = item.get('filename') if hasattr(item, 'get') else item['filename']
        start_ms = int(item['start_time'])
        if not filename or not Path(filename).exists():
            continue
        try:
            seg = AudioSegment.from_file(filename)
        except Exception as e:
            logger.warning(f'配音片段无法解码，预览中跳过: {filename} {e}')
            continue
        segments.append((start_ms, seg))
        total_ms = max(total_ms, start_ms + len(seg))

    base = AudioSegment.silent(duration=max(total_ms, 1), frame_rate=_FRAME_RATE)
    for i, (start_ms, seg) in enumerate(segments):
        base = base.overlay(seg, position=start_ms)
        if progress_cb:
            progress_cb(i + 1, len(segments))

    base.set_channels(1).export(str(out), format='wav')
    return str(out)
