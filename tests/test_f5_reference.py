"""F5-TTS 自动参考质检/中文门禁/多说话人归属 的单测（不触网、不加载大模型）。"""
import sys
import types
from pathlib import Path

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")
pydub = pytest.importorskip("pydub")

from videotrans.tts._f5tts import F5TTS
from videotrans.configure.excepts import DubbingSrtError

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

    def test_choose_prefers_clean_single_clip_over_composite(self, tmp_path):
        short = _voice(tmp_path / "short.wav", 4.0, 110, 0.7, seed=4)
        ideal = _voice(tmp_path / "ideal.wav", 6.0, 110, 0.7, seed=5)
        t = _f5([])
        wav, text = t._choose_reference([
            (0, short, "Short but complete.", 0, 4000),
            (1, ideal, "One clean complete reference sentence.", 1, 6000),
        ])
        assert wav == ideal
        assert text == "One clean complete reference sentence."


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

    def test_reference_selection_stops_when_all_readbacks_mismatch(self, tmp_path):
        wav = _voice(tmp_path / "misaligned.wav", 6.0, 110, 0.7)
        t = _f5([{
            "role": "clone",
            "ref_wav": wav,
            "ref_text": "This is the expected reference sentence.",
        }])
        t._transcribe_one_for_validation = (
            lambda model, filename: "Completely unrelated audio from another timestamp."
        )

        with pytest.raises(DubbingSrtError, match="参考音频回读全部"):
            t._select_safe_reference(object())


class TestValidatorModel:
    def test_large_model_has_priority_over_tiny(self, tmp_path, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        large = tmp_path / "models/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
        tiny = tmp_path / "models/faster-whisper-tiny"
        large.mkdir(parents=True)
        tiny.mkdir(parents=True)
        (large / "model.bin").write_bytes(b"large")
        (tiny / "model.bin").write_bytes(b"tiny")
        monkeypatch.setattr(f5mod, "ROOT_DIR", str(tmp_path))
        assert _f5([])._get_validator_model_path() == large


class TestLowMemoryProfile:
    def test_16gb_apple_silicon_is_enabled(self, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        monkeypatch.setattr(f5mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(f5mod.platform, "machine", lambda: "arm64")
        values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 16 * 1024 ** 3 // 4096}
        monkeypatch.setattr(f5mod.os, "sysconf", lambda key: values[key])
        assert F5TTS._is_low_memory_apple_silicon() is True

    def test_24gb_apple_silicon_keeps_normal_profile(self, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        monkeypatch.setattr(f5mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(f5mod.platform, "machine", lambda: "arm64")
        values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 24 * 1024 ** 3 // 4096}
        monkeypatch.setattr(f5mod.os, "sysconf", lambda key: values[key])
        assert F5TTS._is_low_memory_apple_silicon() is False

    def test_exec_stops_f5_before_whisper_gate(self, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        events = []
        t = _f5([])
        t.api_url = "http://127.0.0.1:7860"
        t._low_memory_profile = True
        t.is_test = False
        t.language = "zh-cn"
        t._start_local_service = lambda recovery=False: events.append("start-f5") or True
        t._stop_local_service = lambda: events.append("stop-f5") or True
        t._verify_chinese_outputs = lambda: events.append("start-whisper")
        monkeypatch.setattr(f5mod.GradioBase, "_exec", lambda self: events.append("synthesize"))

        t._exec()

        assert events == ["start-f5", "synthesize", "stop-f5", "start-whisper", "stop-f5"]

    def test_exec_releases_memory_and_retries_transient_start_failure(self, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        events = []
        starts = iter([False, True])
        t = _f5([])
        t.api_url = "http://127.0.0.1:7860"
        t._low_memory_profile = False
        t.is_test = True
        t.language = "zh-cn"
        t.signal = lambda **kwargs: events.append("signal")
        t._start_local_service = lambda recovery=False: (
            events.append(f"start-{recovery}") or next(starts)
        )
        t._stop_local_service = lambda: events.append("stop") or True
        t._release_memory_pressure = lambda: events.append("release")
        monkeypatch.setattr(f5mod.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(f5mod.GradioBase, "_exec", lambda self: events.append("synthesize"))

        t._exec()

        assert events == [
            "start-False", "signal", "stop", "release", "start-True", "synthesize"
        ]


class TestChineseLanguageGate:
    def test_batch_false_positives_do_not_trigger_redub(self, tmp_path):
        queue = [
            {
                "text": f"这是第 {idx + 1} 句中文配音。",
                "filename": _voice(tmp_path / f"batch-{idx}.wav", 1.0, 110, 0.7,
                                   seed=10 + idx),
            }
            for idx in range(3)
        ]
        t = _f5(queue)
        t.safe_ref_text = "English reference sentence."
        messages = []
        t.signal = lambda **kwargs: messages.append(kwargs.get("text", ""))
        t._transcribe_one_for_validation = (
            lambda model, filename: "这是逐文件复核后的中文配音"
        )
        transcripts = {idx: "This batch timestamp was incorrectly aligned" for idx in range(3)}
        failed = [(idx, item, transcripts[idx]) for idx, item in enumerate(queue)]

        confirmed = t._confirm_batch_failures(object(), failed, transcripts)

        assert confirmed == []
        assert all(value.startswith("这是") for value in transcripts.values())
        assert any("确认 0/3 段需要重配" in message for message in messages)

    def test_tts_progress_includes_eta(self):
        t = _f5([])
        message = t._format_tts_progress(completed=2, total=10, elapsed=20)
        assert "2/10" in message
        assert "10.0秒/段" in message
        assert "1分20秒" in message

    def test_systemic_failure_requires_ratio_and_minimum_count(self, tmp_path):
        wav = _voice(tmp_path / "systemic.wav", 1.0, 110, 0.7)
        queue = [
            {"text": f"第 {idx} 句中文", "filename": wav}
            for idx in range(20)
        ]
        t = _f5(queue)
        failed = [(idx, queue[idx], "English leak") for idx in range(10)]
        assert t._is_systemic_language_failure(failed) is True
        assert t._is_systemic_language_failure(failed[:9]) is False


class TestLongVideoPreflight:
    def _task(self, tmp_path, count=3):
        queue = [
            {
                "text": "短句" if idx == 0 else ("这是需要预飞验证的中文句子" * (idx + 1)),
                "filename": str(tmp_path / f"final-{idx}.wav"),
                "cluster_ref": f"/refs/speaker-{idx % 2}.wav",
            }
            for idx in range(count)
        ]
        t = _f5(queue)
        t.uuid = "preflight-test"
        t.language = "zh-cn"
        t.is_test = False
        t.safe_ref_wav = "/refs/main.wav"
        t.safe_ref_text = "English reference sentence."
        t._low_memory_profile = False
        t.signal = lambda **kwargs: None
        t._get_validator_model_path = lambda: tmp_path
        return t

    def test_risk_samples_cover_short_long_and_speakers(self, tmp_path):
        t = self._task(tmp_path, count=12)
        indices = t._preflight_indices(5)
        assert 0 in indices
        assert 11 in indices
        assert {t.queue_tts[idx]["cluster_ref"] for idx in indices} == {
            "/refs/speaker-0.wav", "/refs/speaker-1.wav"
        }

    def test_repetition_detector(self):
        assert F5TTS._has_pathological_repetition("一种一种一种一种一种一种") is True
        assert F5TTS._has_pathological_repetition("这是一句正常且完整的中文配音") is False

    def test_passed_preflight_audio_is_reused_by_full_run(self, tmp_path, monkeypatch):
        t = self._task(tmp_path, count=2)

        def synthesize(item, idx):
            _voice(Path(item["filename"]), 1.0, 110, 0.7, seed=idx + 70)
            return None

        t._item_task = synthesize
        t._transcribe_one_for_validation = lambda model, filename: "这是正常的中文配音"
        monkeypatch.setitem(
            sys.modules,
            "faster_whisper",
            types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: object()),
        )

        t._run_preflight()

        assert all(Path(item["filename"]).is_file() for item in t.queue_tts)

    def test_failed_preflight_stops_before_copying_outputs(self, tmp_path, monkeypatch):
        t = self._task(tmp_path, count=2)
        t._item_task = lambda item, idx: (
            _voice(Path(item["filename"]), 1.0, 110, 0.7, seed=idx + 80) and None
        )
        t._transcribe_one_for_validation = lambda model, filename: "English reference leaked again"
        monkeypatch.setitem(
            sys.modules,
            "faster_whisper",
            types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: object()),
        )

        with pytest.raises(DubbingSrtError, match="预飞质量核对未通过"):
            t._run_preflight()

        assert not any(Path(item["filename"]).exists() for item in t.queue_tts)

    def test_hidden_gradio_oom_restarts_and_retries_only_one_item(self, tmp_path, monkeypatch):
        import videotrans.tts._gradio as gradiomod

        item = {"text": "需要重试", "filename": str(tmp_path / "retry.wav")}
        t = _f5([item])
        t.api_url = "http://127.0.0.1:7860"
        t.signal = lambda **kwargs: None
        t._exit = lambda: False
        recovered = []
        t._recover_local_service = lambda: recovered.append(True) or True
        calls = []

        def base_item_task(self, data_item, idx):
            calls.append(idx)
            if len(calls) == 1:
                return "The upstream Gradio app has raised an exception"
            _voice(Path(data_item["filename"]), 1.0, 110, 0.7, seed=90)
            return None

        monkeypatch.setattr(gradiomod.GradioBase, "_item_task", base_item_task)

        assert t._item_task(item, 3) is None
        assert calls == [3, 3]
        assert recovered == [True]
        assert Path(item["filename"]).is_file()

    def test_resume_selects_asr_verified_chinese_anchor(self, tmp_path):
        good = _voice(tmp_path / "good-anchor.wav", 6.5, 110, 0.7, seed=91)
        bad = _voice(tmp_path / "bad-anchor.wav", 6.0, 110, 0.7, seed=92)
        queue = [
            {"text": "这是一段干净可用的中文音色参考。", "filename": good},
            {"text": "这一段其实仍然夹杂了英文参考。", "filename": bad},
        ]
        t = _f5(queue)
        t.safe_ref_text = "English reference sentence."
        t.signal = lambda **kwargs: None
        t._transcribe_one_for_validation = lambda model, filename: (
            "这是正常清晰的中文配音"
            if filename == good else "English reference sentence leaked"
        )

        wav, text = t._select_existing_chinese_anchor(object())

        assert wav == good
        assert text.endswith("。")

    def test_resume_chinese_anchors_stay_with_same_speaker(self, tmp_path):
        a = _voice(tmp_path / "speaker-a.wav", 6.5, 110, 0.7, seed=93)
        b = _voice(tmp_path / "speaker-b.wav", 6.5, 160, 0.7, seed=94)
        queue = [
            {"text": "这是嘉宾已经完成的干净中文配音。", "filename": a,
             "cluster_ref": "/refs/guest.wav"},
            {"text": "这是主持人已经完成的干净中文配音。", "filename": b,
             "cluster_ref": "/refs/host.wav"},
        ]
        t = _f5(queue)
        t.safe_ref_text = ""
        t.signal = lambda **kwargs: None
        t._transcribe_one_for_validation = lambda model, filename: "这是清晰正常的中文配音"

        t._select_existing_chinese_anchor(object())

        assert t.resume_chinese_anchors["/refs/guest.wav"][0] == a
        assert t.resume_chinese_anchors["/refs/host.wav"][0] == b

    def test_assigns_same_speaker_clean_chinese_anchor(self, tmp_path):
        good = _voice(tmp_path / "good.wav", 6.0, 110, 0.7, seed=20)
        bad = _voice(tmp_path / "bad.wav", 6.0, 110, 0.7, seed=21)
        queue = [
            {"text": "这是同一个说话人的干净中文参考。", "filename": good,
             "cluster_ref": "/refs/speaker-a.wav"},
            {"text": "这一句需要重新生成。", "filename": bad,
             "cluster_ref": "/refs/speaker-a.wav"},
        ]
        t = _f5(queue)
        t.safe_ref_text = ""
        failed = [(1, queue[1], "This is leaked English speech")]
        assert t._assign_chinese_anchors(failed, {0: "这是干净的中文参考", 1: failed[0][2]}) == 1
        assert queue[1]["chinese_anchor_ref"] == good
        assert queue[1]["chinese_anchor_text"].endswith("。")

    def test_remaining_leak_raises_and_writes_marker(self, tmp_path, monkeypatch):
        wav = _voice(tmp_path / "failed.wav", 2.0, 110, 0.7, seed=22)
        item = {"text": "这是需要生成的中文。", "filename": wav, "role": "clone"}
        t = _f5([item])
        t.uuid = "strict-test"
        t.language = "zh-cn"
        t.is_test = False
        t.safe_ref_text = "English reference sentence."
        t.signal = lambda **kwargs: None
        t._get_validator_model_path = lambda: tmp_path
        t._transcribe_batch_for_validation = lambda model: {0: "This is leaked English speech"}
        t._transcribe_one_for_validation = lambda model, filename: "This is leaked English speech"
        t._assign_chinese_anchors = lambda failed, transcripts: 0

        def regenerate(data_item, idx):
            _voice(Path(data_item["filename"]), 2.0, 110, 0.7, seed=30 + idx)
            return None

        t._item_task = regenerate
        fake_fw = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: object())
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

        with pytest.raises(DubbingSrtError, match="质量门禁未通过"):
            t._verify_chinese_outputs()
        assert (tmp_path / "lang_leak.json").is_file()

    def test_service_disconnect_recovers_only_failed_item_and_releases_validator(
            self, tmp_path, monkeypatch):
        wav = _voice(tmp_path / "retry.wav", 2.0, 110, 0.7, seed=40)
        item = {"text": "这是需要重新生成的中文。", "filename": wav, "role": "clone"}
        t = _f5([item])
        t.uuid = "recover-test"
        t.language = "zh-cn"
        t.is_test = False
        t.safe_ref_text = "English reference sentence."
        t.signal = lambda **kwargs: None
        t._get_validator_model_path = lambda: tmp_path
        t._transcribe_batch_for_validation = lambda model: {0: "This is leaked English speech"}
        t._assign_chinese_anchors = lambda failed, transcripts: 0

        validation_reads = []

        def read_back(model, filename):
            validation_reads.append(filename)
            # First standalone read confirms the initial leak; the read after
            # regeneration confirms that recovery produced Chinese.
            return "This is leaked English speech" if len(validation_reads) == 1 else "这是重新生成的中文"

        t._transcribe_one_for_validation = read_back

        live_models = 0

        class FakeModel:
            def __init__(self, *args, **kwargs):
                nonlocal live_models
                live_models += 1

            def __del__(self):
                nonlocal live_models
                live_models -= 1

        calls = []

        def regenerate(data_item, idx):
            # The large validator must not coexist with F5 inference on MPS.
            assert live_models == 0
            calls.append(idx)
            if len(calls) == 1:
                return "[Errno 61] Connection refused"
            _voice(Path(data_item["filename"]), 2.0, 110, 0.7, seed=41)
            return None

        recovered = []
        t._item_task = regenerate
        t._recover_local_service = lambda: recovered.append(True) or True
        fake_fw = types.SimpleNamespace(WhisperModel=FakeModel)
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

        t._verify_chinese_outputs()

        assert calls == [0, 0]
        assert recovered == [True]
        assert len(validation_reads) == 2
        assert Path(item["filename"]).is_file()
        assert live_models == 0

    def test_service_disconnect_is_not_reported_as_language_leak(self, tmp_path, monkeypatch):
        wav = _voice(tmp_path / "failed-service.wav", 2.0, 110, 0.7, seed=42)
        item = {"text": "这是需要重新生成的中文。", "filename": wav, "role": "clone"}
        t = _f5([item])
        t.uuid = "service-error-test"
        t.language = "zh-cn"
        t.is_test = False
        t.safe_ref_text = "English reference sentence."
        t.signal = lambda **kwargs: None
        t._get_validator_model_path = lambda: tmp_path
        t._transcribe_batch_for_validation = lambda model: {0: "This is leaked English speech"}
        t._assign_chinese_anchors = lambda failed, transcripts: 0
        t._item_task = lambda data_item, idx: "MPS backend out of memory"
        t._recover_local_service = lambda: False
        fake_fw = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: object())
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

        with pytest.raises(DubbingSrtError, match="本地服务在质量复核重配时失败") as error:
            t._verify_chinese_outputs()
        assert "质量门禁未通过" not in str(error.value)


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
            t.resume_chinese_anchor_ref = None
            t.resume_chinese_anchor_text = None
            t.resume_chinese_anchors = {}
            t.ref_backups = []
            t.get_ref_wav = lambda item: ("/refs/line.wav", "Line reference text.")
            t.get_speed = lambda: 1.0
            t.MAX_REF_AUDIO_MS = 999999
            t._send = lambda kw, item: runs.append(kw)
            t._run({"role": "clone", "text": "你好",
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text."}, 0)
            t._run({"role": "clone", "text": "你好"}, 1)
            t._run({"role": "clone", "text": "你好", "lang_leak_retry": 1,
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text.",
                    "chinese_anchor_ref": "/refs/zh.wav", "chinese_anchor_text": "中文参考。"}, 2)
            t.resume_chinese_anchor_ref = "/refs/resume-zh.wav"
            t.resume_chinese_anchor_text = "恢复用中文参考。"
            t.resume_chinese_anchors = {
                "/refs/spk1.wav": ("/refs/resume-zh.wav", "恢复用中文参考。")
            }
            t._run({"role": "clone", "text": "你好",
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text."}, 3)
            assert runs[0]["ref_audio_input"] == "/refs/spk1.wav"
            assert runs[1]["ref_audio_input"] == "/refs/global.wav"
            assert runs[2]["ref_audio_input"] == "/refs/zh.wav"
            assert runs[2]["ref_text_input"] == "中文参考。"
            assert runs[3]["ref_audio_input"] == "/refs/resume-zh.wav"
            assert runs[3]["ref_text_input"] == "恢复用中文参考。"
        finally:
            f5mod.AudioSegment = orig_seg
