"""把逐句配音片段(queue_tts)按字幕起始时间拼成单个可 seek 的预览 wav。

无 Qt 依赖。间隙自然呈现为静音、重叠为叠加，波形轨直接反映时间轴问题。
"""
import hashlib
import logging
import os
import wave
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger('VideoTrans')

PREVIEW_NAME = 'dub_preview.wav'
_FRAME_RATE = 16000
EAGER_WAVEFORM_MAX_MS = 5 * 60 * 1000
EAGER_DUB_PREVIEW_MAX_ITEMS = 120


def preview_loading_policy(duration_ms: int, item_count: int) -> tuple:
    """长视频启动时跳过高成本整轨构建，保留逐段试听。"""
    eager_waveform = int(duration_ms or 0) <= EAGER_WAVEFORM_MAX_MS
    eager_dubbed = eager_waveform and int(item_count or 0) <= EAGER_DUB_PREVIEW_MAX_ITEMS
    return eager_waveform, eager_dubbed


def preview_path(cache_folder: str, name: str = PREVIEW_NAME) -> Path:
    return Path(cache_folder) / name


def preview_cache_name(queue_tts, duration_ms: int) -> str:
    """Return a content-addressed preview name for safe cross-session reuse."""
    digest = hashlib.sha1()
    digest.update(str(int(duration_ms or 0)).encode())
    for item in queue_tts or []:
        path = Path(str(item.get('filename') or ''))
        try:
            stat = path.stat()
            file_token = f'{path}:{stat.st_size}:{stat.st_mtime_ns}'
        except OSError:
            file_token = str(path)
        digest.update(
            f"|{int(item.get('start_time', 0) or 0)}:{file_token}".encode(
                'utf-8', errors='ignore'))
    return f'dub_preview_{digest.hexdigest()[:16]}.wav'


def invalidate_dub_preview(cache_folder: str) -> None:
    # 重新配音某行后调用，下次打开预览时重建
    preview_path(cache_folder).unlink(missing_ok=True)


def cleanup_previews(cache_folder: str) -> None:
    # 清理所有轮换版本（dub_preview*.wav）
    try:
        for f in Path(cache_folder).glob('dub_preview*.wav'):
            f.unlink(missing_ok=True)
    except OSError:
        pass


def build_dub_preview_wav(queue_tts, duration_ms: int, cache_folder: str,
                          progress_cb=None, out_name: str = PREVIEW_NAME) -> str:
    """queue_tts 每项需支持 ['start_time'](ms) 与 ['filename']；返回生成的 wav 路径。

    已存在则直接复用（用 invalidate_dub_preview 强制重建）。Studio 每次重建
    传入递增的 out_name：QMediaPlayer 对同名 URL 可能不重新加载。
    """
    out = preview_path(cache_folder, out_name)
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
            seg = (AudioSegment.from_file(filename)
                   .set_frame_rate(_FRAME_RATE)
                   .set_channels(1)
                   .set_sample_width(2))
        except Exception as e:
            logger.warning(f'配音片段无法解码，预览中跳过: {filename} {e}')
            continue
        segments.append((start_ms, seg))
        total_ms = max(total_ms, start_ms + len(seg))

    # ``AudioSegment.overlay`` copies the entire 26-minute base for every
    # clip.  With hundreds of clips that becomes quadratic and was the reason
    # long-video sync preview had to be disabled.  Mix each clip into one
    # disk-backed int32 buffer instead, then stream-clamp it to PCM16 WAV.
    import numpy as np

    sample_count = max((max(total_ms, 1) * _FRAME_RATE + 999) // 1000, 1)
    raw_mix = out.with_name(f'.{out.name}.{os.getpid()}.mix')
    tmp_wav = out.with_name(f'.{out.name}.{os.getpid()}.tmp')
    mix = None
    try:
        mix = np.memmap(raw_mix, dtype=np.int32, mode='w+', shape=(sample_count,))
        mix[:] = 0
        for i, (start_ms, seg) in enumerate(segments):
            samples = np.frombuffer(seg.raw_data, dtype='<i2').astype(np.int32)
            start_sample = max(int(start_ms * _FRAME_RATE / 1000), 0)
            end_sample = min(start_sample + len(samples), sample_count)
            if end_sample > start_sample:
                mix[start_sample:end_sample] += samples[:end_sample - start_sample]
            if progress_cb:
                progress_cb(i + 1, len(segments))
        mix.flush()

        with wave.open(str(tmp_wav), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(_FRAME_RATE)
            chunk = _FRAME_RATE * 30
            for start in range(0, sample_count, chunk):
                pcm = np.clip(mix[start:start + chunk], -32768, 32767).astype('<i2')
                wav.writeframes(pcm.tobytes())
        os.replace(tmp_wav, out)
    finally:
        if mix is not None:
            del mix
        raw_mix.unlink(missing_ok=True)
        tmp_wav.unlink(missing_ok=True)
    return str(out)
