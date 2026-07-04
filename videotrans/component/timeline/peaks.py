"""音频波形峰值提取：ffmpeg 解码 -> numpy 分桶 min/max，供波形轨绘制。

无 Qt 依赖。峰值按 10ms 一桶（PEAKS_PER_SECOND=100），最大缩放 200px/s 时
每桶约 2 像素，足够精细；2 小时音频约 72 万桶 ≈ 5.5MB float32。
"""
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

logger = logging.getLogger('VideoTrans')

PEAKS_PER_SECOND = 100
_SAMPLE_RATE = 8000
_SAMPLES_PER_BUCKET = _SAMPLE_RATE // PEAKS_PER_SECOND  # 80


def _cache_file(media_path: str, cache_dir: str) -> Path:
    st = os.stat(media_path)
    key = hashlib.md5(f'{media_path}|{st.st_size}|{st.st_mtime_ns}'.encode()).hexdigest()
    return Path(cache_dir) / f'peaks_{key}.npy'


def extract_peaks(media_path: str, cache_dir: str) -> tuple:
    """返回 (peaks, duration_ms)。

    peaks: float32 ndarray[N,2]，每行为该 10ms 桶内的 (min, max)，范围 -1..1。
    结果以 .npy 缓存在 cache_dir（键含文件大小与 mtime，源文件变化自动失效）。
    """
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache = _cache_file(media_path, cache_dir)
    if cache.exists():
        peaks = np.load(cache)
        return peaks, int(len(peaks) * 1000 / PEAKS_PER_SECOND)

    # 解码到临时 raw 文件而非 stdout，避免长视频产生数百 MB 的管道缓冲
    raw_path = cache.with_suffix('.raw')
    cmd = ['ffmpeg', '-y', '-hide_banner', '-nostdin',
           '-i', media_path, '-vn',
           '-ac', '1', '-ar', str(_SAMPLE_RATE), '-f', 's16le',
           raw_path.as_posix()]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        samples = np.fromfile(raw_path, dtype=np.int16)
    finally:
        raw_path.unlink(missing_ok=True)

    duration_ms = int(round(len(samples) * 1000 / _SAMPLE_RATE))
    pad = (-len(samples)) % _SAMPLES_PER_BUCKET
    if pad:
        samples = np.pad(samples, (0, pad))
    if len(samples) == 0:
        peaks = np.zeros((0, 2), dtype=np.float32)
    else:
        buckets = samples.reshape(-1, _SAMPLES_PER_BUCKET).astype(np.float32) / 32768.0
        peaks = np.stack([buckets.min(axis=1), buckets.max(axis=1)], axis=1)

    try:
        np.save(cache, peaks)
    except OSError as e:
        logger.warning(f'峰值缓存写入失败(不影响使用): {e}')
    return peaks, duration_ms
