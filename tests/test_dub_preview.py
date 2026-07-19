import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")
pydub = pytest.importorskip("pydub")

from videotrans.component.timeline.dub_preview import (
    build_dub_preview_wav,
    invalidate_dub_preview,
    preview_loading_policy,
    preview_path,
)


def _write_tone(path, seconds=1.0, sr=16000, amp=0.5):
    t = np.arange(int(seconds * sr)) / sr
    soundfile.write(str(path), amp * np.sin(2 * np.pi * 440 * t), sr)
    return str(path)


class TestBuildDubPreview:
    def test_long_video_defers_expensive_whole_track_preview(self):
        assert preview_loading_policy(26 * 60 * 1000, 537) == (False, False)
        assert preview_loading_policy(3 * 60 * 1000, 50) == (True, True)
        assert preview_loading_policy(3 * 60 * 1000, 500) == (True, False)

    def test_segments_positioned_with_silence_between(self, tmp_path):
        seg1 = _write_tone(tmp_path / "seg1.wav")
        seg2 = _write_tone(tmp_path / "seg2.wav")
        queue_tts = [
            {"start_time": 0, "end_time": 1000, "filename": seg1},
            {"start_time": 5000, "end_time": 6000, "filename": seg2},
        ]
        out = build_dub_preview_wav(queue_tts, duration_ms=7000, cache_folder=str(tmp_path))

        data, sr = soundfile.read(out)
        assert len(data) / sr == pytest.approx(7.0, abs=0.05)
        ms = sr // 1000
        assert np.abs(data[100 * ms:900 * ms]).max() > 0.3   # 第一段有声
        assert np.abs(data[2000 * ms:4500 * ms]).max() < 0.01  # 中间静音
        assert np.abs(data[5100 * ms:5900 * ms]).max() > 0.3  # 第二段有声

    def test_missing_segment_skipped(self, tmp_path):
        seg1 = _write_tone(tmp_path / "seg1.wav")
        queue_tts = [
            {"start_time": 0, "filename": seg1},
            {"start_time": 2000, "filename": str(tmp_path / "nonexistent.wav")},
        ]
        out = build_dub_preview_wav(queue_tts, duration_ms=3000, cache_folder=str(tmp_path))
        data, sr = soundfile.read(out)
        assert len(data) / sr == pytest.approx(3.0, abs=0.05)

    def test_reuse_and_invalidate(self, tmp_path):
        seg1 = _write_tone(tmp_path / "seg1.wav")
        queue = [{"start_time": 0, "filename": seg1}]
        out1 = build_dub_preview_wav(queue, 1000, str(tmp_path))
        mtime1 = preview_path(str(tmp_path)).stat().st_mtime_ns
        out2 = build_dub_preview_wav(queue, 1000, str(tmp_path))
        assert out1 == out2
        assert preview_path(str(tmp_path)).stat().st_mtime_ns == mtime1  # 复用未重建

        invalidate_dub_preview(str(tmp_path))
        assert not preview_path(str(tmp_path)).exists()
