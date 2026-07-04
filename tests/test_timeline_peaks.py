import shutil

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from videotrans.component.timeline.peaks import extract_peaks, PEAKS_PER_SECOND

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="ffmpeg not available")

AMP = 0.5


@pytest.fixture
def sine_wav(tmp_path):
    # 前 2 秒 440Hz 正弦(幅度 0.5) + 后 1 秒静音
    sr = 16000
    t = np.arange(2 * sr) / sr
    tone = AMP * np.sin(2 * np.pi * 440 * t)
    data = np.concatenate([tone, np.zeros(sr)])
    path = tmp_path / "tone.wav"
    soundfile.write(str(path), data, sr)
    return str(path)


class TestExtractPeaks:
    def test_shape_and_duration(self, sine_wav, tmp_path):
        peaks, duration_ms = extract_peaks(sine_wav, str(tmp_path / "cache"))
        assert duration_ms == pytest.approx(3000, abs=20)
        assert peaks.shape == (duration_ms * PEAKS_PER_SECOND // 1000, 2)
        assert peaks.dtype == np.float32

    def test_amplitude(self, sine_wav, tmp_path):
        peaks, _ = extract_peaks(sine_wav, str(tmp_path / "cache"))
        tone = peaks[10:190]  # 正弦区间(避开边缘)
        # 每桶 10ms 含多个 440Hz 周期，min/max 必接近 ∓/±0.5
        assert tone[:, 1].max() == pytest.approx(AMP, abs=0.05)
        assert tone[:, 0].min() == pytest.approx(-AMP, abs=0.05)
        silence = peaks[210:290]
        assert np.abs(silence).max() < 0.01

    def test_cache_hit(self, sine_wav, tmp_path):
        cache_dir = tmp_path / "cache"
        p1, d1 = extract_peaks(sine_wav, str(cache_dir))
        npy_files = list(cache_dir.glob("peaks_*.npy"))
        assert len(npy_files) == 1
        # 破坏缓存内容后再次调用应直接读缓存（内容不同即证明未重新解码）
        np.save(npy_files[0], np.zeros((5, 2), dtype=np.float32))
        p2, d2 = extract_peaks(sine_wav, str(cache_dir))
        assert p2.shape == (5, 2)
        assert d2 == 50
