"""F5-TTS 自动参考质检/复合参考/多说话人归属 的单测（不触网、不加载大模型）。"""
from pathlib import Path

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")
pydub = pytest.importorskip("pydub")

from videotrans.tts._f5tts import F5TTS

SR = 16000


def _voice(path, secs, f0, bright, seed=1):
    rng = np.random.RandomState(seed)
    t = np.arange(int(SR * secs)) / SR
    f0 = f0 * (1 + 0.02 * rng.randn())
    y = np.zeros_like(t)
    for k in range(1, 12):
        y += (bright ** k) * np.sin(2 * np.pi * f0 * k * t + k + rng.rand())
    y = y / np.abs(y).max() * 0.7 + 0.02 * rng.randn(len(t))
    y *= (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t + rng.rand()) ** 2)
    soundfile.write(str(path), y, SR)
    return str(path)


def _f5(queue):
    t = F5TTS.__new__(F5TTS)
    t.queue_tts = queue
    return t


class TestTextSimilarity:
    def test_match(self):
        assert F5TTS._text_similarity(
            "hello world this is a test", "Hello world, this is a test.") > 0.9

    def test_mismatch(self):
        assert F5TTS._text_similarity(
            "first you have got the shoes", "完全不同的中文内容在这里") < 0.3

    def test_empty(self):
        assert F5TTS._text_similarity("", "anything") == 0.0


class TestPunctPenalty:
    def test_mid_sentence_ranked_below_complete(self, tmp_path):
        # 同等条件下，掐半句的 ref_text 必须排在完整句之后（毒参考事故的根源）
        w1 = _voice(tmp_path / "a.wav", 8.0, 110, 0.7, seed=1)
        w2 = _voice(tmp_path / "b.wav", 8.0, 110, 0.7, seed=2)
        queue = [
            {"role": "clone", "ref_wav": w1, "ref_text": "First you have got",
             "start_time": 0, "end_time": 8000},
            {"role": "clone", "ref_wav": w2, "ref_text": "This is a complete sentence here.",
             "start_time": 8000, "end_time": 16000},
        ]
        t = _f5(queue)
        cands = t._collect_candidates()
        ranked = sorted(cands, key=lambda c: c[0])
        assert ranked[0][2].startswith("This is a complete")


class TestComposeReference:
    def test_short_primary_gets_composited(self, tmp_path):
        w1 = _voice(tmp_path / "p.wav", 4.0, 110, 0.7, seed=1)
        w2 = _voice(tmp_path / "q.wav", 4.0, 110, 0.7, seed=2)
        t = _f5([])
        pool = [(0, w1, "Primary text.", 0, 4000), (1, w2, "Second text", 1, 4000)]
        wav, text = t._compose_reference(pool, tag="t1")
        assert "f5-composite-ref-t1" in wav
        assert 7000 <= len(pydub.AudioSegment.from_file(wav)) <= 12500
        assert text == "Primary text. Second text."

    def test_long_primary_used_as_is(self, tmp_path):
        w1 = _voice(tmp_path / "long.wav", 8.0, 110, 0.7, seed=3)
        t = _f5([])
        wav, text = t._compose_reference([(0, w1, "Long enough already.", 0, 8000)])
        assert wav == w1 and text == "Long enough already."


class TestValidateCandidates:
    def test_no_validator_returns_empty(self):
        t = _f5([])
        assert t._validate_candidates([(0, "/x.wav", "text", 0, 8000)], None) == []

    def test_filters_mismatched(self, tmp_path):
        w = _voice(tmp_path / "v.wav", 8.0, 110, 0.7)
        t = _f5([])

        class FakeValidator:
            pass
        t._transcribe_one_for_validation = lambda model, f: "totally different words spoken"
        good = (0, w, "totally different words spoken indeed", 0, 8000)
        bad = (1, w, "首先你必须要看到这个", 1, 8000)
        passed = t._validate_candidates([good, bad], FakeValidator())
        assert good in passed and bad not in passed


class TestClusterRefs:
    def _mk_queue(self, tmp_path, n_a=10, n_b=5):
        queue = []
        for i in range(n_a):
            queue.append({"role": "clone",
                          "ref_wav": _voice(tmp_path / f"A{i}.wav", 6.0, 110, 0.75, seed=i + 1),
                          "ref_text": f"Main speaker line number {i} content ok.",
                          "start_time": i * 6000, "end_time": i * 6000 + 6000})
        for i in range(n_b):
            queue.append({"role": "clone",
                          "ref_wav": _voice(tmp_path / f"B{i}.wav", 6.0, 220, 0.35, seed=100 + i),
                          "ref_text": f"Host speaker line number {i} content ok.",
                          "start_time": (n_a + i) * 6000, "end_time": (n_a + i) * 6000 + 6000})
        return queue

    def test_two_speakers_get_distinct_refs(self, tmp_path):
        queue = self._mk_queue(tmp_path)
        t = _f5(queue)
        t._build_cluster_refs(validator=None)
        refs_a = {it.get("cluster_ref") for it in queue[:10] if it.get("cluster_ref")}
        refs_b = {it.get("cluster_ref") for it in queue[10:] if it.get("cluster_ref")}
        assert refs_a and refs_b and refs_a.isdisjoint(refs_b)

    def test_single_speaker_no_cluster_refs(self, tmp_path):
        queue = [{"role": "clone",
                  "ref_wav": _voice(tmp_path / f"S{i}.wav", 6.0, 130, 0.6, seed=i + 1),
                  "ref_text": f"Single speaker line {i} content ok.",
                  "start_time": i * 6000, "end_time": i * 6000 + 6000} for i in range(14)]
        t = _f5(queue)
        t._build_cluster_refs(validator=None)
        # 单说话人：要么聚类判为不可靠，要么被无害地分簇——但两簇都是同一人，
        # 唯一硬性要求是不崩溃且 cluster_ref 若存在必须是合法路径
        for it in queue:
            cr = it.get("cluster_ref")
            assert cr is None or Path(cr).exists()

    def test_disabled_by_setting(self, tmp_path, monkeypatch):
        from videotrans.configure.config import settings
        monkeypatch.setitem(settings, "f5tts_multi_speaker", "false")
        queue = self._mk_queue(tmp_path)
        t = _f5(queue)
        t._build_cluster_refs(validator=None)
        assert not any(it.get("cluster_ref") for it in queue)


class TestRunUsesClusterRef:
    def test_cluster_ref_priority(self, tmp_path):
        import videotrans.tts._f5tts as f5mod
        runs = []
        f5mod.handle_file = lambda p: p

        class FakeSeg:
            def __len__(self):
                return 8000
        orig_seg = f5mod.AudioSegment
        f5mod.AudioSegment = type("A", (), {"from_file": staticmethod(lambda p: FakeSeg())})
        try:
            t = F5TTS.__new__(F5TTS)
            t.safe_ref_wav, t.safe_ref_text = "/refs/global.wav", "Global reference text."
            t.ref_backups = []
            t.get_ref_wav = lambda item: ("/refs/line.wav", "Line reference text.")
            t.get_speed = lambda: 1.0
            t.MAX_REF_AUDIO_MS = 999999
            t._send = lambda kw, item: runs.append(kw)
            t._run({"role": "clone", "text": "你好",
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text."}, 0)
            t._run({"role": "clone", "text": "你好"}, 1)
            assert runs[0]["ref_audio_input"] == "/refs/spk1.wav"
            assert runs[1]["ref_audio_input"] == "/refs/global.wav"
        finally:
            f5mod.AudioSegment = orig_seg
