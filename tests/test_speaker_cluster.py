import os
import tempfile

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from videotrans.util.speaker_cluster import cluster_speakers

SR = 16000


def _voice(path, secs, f0, bright, seed=1):
    """合成带谐波的伪人声：f0 基频，bright 控制高频能量（音色差异）。

    seed 让同一"说话人"的每段话有自然差异（基频漂移/噪声/包络相位），
    避免完全相同的文件构成聚类的退化输入。
    """
    rng = np.random.RandomState(seed)
    t = np.arange(int(SR * secs)) / SR
    f0 = f0 * (1 + 0.02 * rng.randn())
    y = np.zeros_like(t)
    for k in range(1, 12):
        y += (bright ** k) * np.sin(2 * np.pi * f0 * k * t + k + rng.rand())
    y = y / np.abs(y).max() * 0.7 + 0.02 * rng.randn(len(t))
    y *= (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t + rng.rand()) ** 2)
    soundfile.write(path, y, SR)
    return path


@pytest.fixture
def tmp_wavs(tmp_path):
    counter = [0]

    def make(name, secs, f0, bright):
        counter[0] += 1
        return _voice(str(tmp_path / name), secs, f0, bright, seed=counter[0])
    return make


class TestClusterSpeakers:
    def test_two_voices_separate(self, tmp_wavs):
        paths = [tmp_wavs(f"a{i}.wav", 6.0, 110, 0.75) for i in range(4)]
        paths += [tmp_wavs(f"b{i}.wav", 6.0, 220, 0.35) for i in range(4)]
        labels = cluster_speakers(paths)
        assert labels is not None
        a = {labels[i] for i in range(4)}
        b = {labels[i] for i in range(4, 8)}
        assert len(a) == 1 and len(b) == 1 and a != b

    def test_single_voice_no_crash(self, tmp_wavs):
        # 单说话人可能返回 None（silhouette 低）也可能被轻微分簇——后者无害：
        # 两簇都是同一个人，调用方取主簇后参考仍是正确的声音。契约只要求不崩溃、
        # 且返回值要么是 None 要么是合法的 {下标: 0/1}。
        paths = [tmp_wavs(f"s{i}.wav", 6.0, 130, 0.6) for i in range(8)]
        labels = cluster_speakers(paths)
        assert labels is None or set(labels.values()) <= {0, 1}

    def test_too_few_clips_returns_none(self, tmp_wavs):
        paths = [tmp_wavs(f"f{i}.wav", 6.0, 130, 0.6) for i in range(3)]
        assert cluster_speakers(paths) is None

    def test_unreadable_files_skipped(self, tmp_wavs, tmp_path):
        paths = [tmp_wavs(f"g{i}.wav", 6.0, 110, 0.75) for i in range(4)]
        paths += [tmp_wavs(f"h{i}.wav", 6.0, 220, 0.35) for i in range(4)]
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"not a wav")
        paths.append(str(bad))
        labels = cluster_speakers(paths)
        assert labels is not None
        assert len(paths) - 1 not in labels  # 坏文件被跳过
