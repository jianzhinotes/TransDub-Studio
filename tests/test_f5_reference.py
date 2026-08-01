"""F5-TTS 自动参考质检/中文门禁/多说话人归属 的单测（不触网、不加载大模型）。"""
import json
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
    def test_service_environment_overrides_app_huggingface_cache(self, tmp_path, monkeypatch):
        script = tmp_path / "f5-service" / "start_service.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/zsh\n")
        monkeypatch.setenv("HF_HOME", "/app/asr-models")
        monkeypatch.setenv("HF_HUB_CACHE", "/app/asr-models")

        env = F5TTS._local_service_environment(script)

        expected_hub = script.parent / "cache" / "huggingface" / "hub"
        assert env["HF_HOME"] == str(script.parent / "cache" / "huggingface")
        assert env["HF_HUB_CACHE"] == str(expected_hub)
        assert env["HUGGINGFACE_HUB_CACHE"] == str(expected_hub)
        assert env["CACHED_PATH_CACHE_ROOT"] == str(expected_hub)

    def test_service_error_summary_names_vocos_cache_problem(self):
        detail = F5TTS._summarize_local_service_error(
            "Download Vocos charactr/vocos-mel-24khz\n"
            "OfflineModeIsEnabled\nLocalEntryNotFoundError"
        )

        assert "Vocos" in detail
        assert "缓存" in detail

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
        t._should_run_preflight = lambda: False
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

    def test_incomplete_synthesis_never_starts_quality_validator(self, tmp_path, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        ready = _voice(tmp_path / "ready.wav", 1.0, 110, 0.7, seed=50)
        queue = [
            {"text": "已生成", "filename": ready},
            {"text": "未生成", "filename": str(tmp_path / "missing.wav")},
        ]
        t = _f5(queue)
        t.api_url = "https://remote.example.test"
        t.language = "zh-cn"
        t.is_test = False
        t._low_memory_profile = False
        t._should_run_preflight = lambda: False
        checked = []
        t._verify_chinese_outputs = lambda: checked.append(True)
        monkeypatch.setattr(f5mod.GradioBase, "_exec", lambda self: None)

        with pytest.raises(DubbingSrtError, match="仍缺少 1 段"):
            t._exec()

        assert checked == []

    def test_service_recovery_failure_opens_circuit_for_remaining_lines(self, monkeypatch):
        import videotrans.tts._f5tts as f5mod
        t = _f5([])
        t.api_url = "http://127.0.0.1:7860"
        t._service_circuit_error = ""
        calls = []
        monkeypatch.setattr(
            f5mod.GradioBase, "_item_task",
            lambda self, item, idx: calls.append(idx) or "MPS backend out of memory",
        )
        t._recover_local_service = lambda: False
        t.signal = lambda **kwargs: None

        first = t._item_task({"text": "一", "filename": "/tmp/a.wav"}, 0)
        second = t._item_task({"text": "二", "filename": "/tmp/b.wav"}, 1)

        assert "out of memory" in str(first)
        assert second == first
        assert calls == [0]


class TestChineseLanguageGate:
    def test_batch_false_positives_do_not_trigger_redub(self, tmp_path):
        queue = [
            {
                "text": "这是逐文件复核后的中文配音。",
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

    def test_preflight_budget_expands_to_cover_speakers(self, tmp_path):
        t = self._task(tmp_path, count=10)
        for idx, item in enumerate(t.queue_tts):
            item["cluster_ref"] = f"/refs/speaker-{idx % 5}.wav"
        assert t._preflight_sample_limit() == 7
        indices = t._preflight_indices(t._preflight_sample_limit())
        assert len({t.queue_tts[idx]["cluster_ref"] for idx in indices}) == 5

    def test_preflight_always_includes_highest_compute_risk(self, tmp_path):
        t = self._task(tmp_path, count=12)
        danger = t.queue_tts[6]
        danger.update({
            "text": "这是一个必须在很短时间槽内说完的异常超长中文句子" * 8,
            "ref_text": "Short source.",
            "start_time_source": 0,
            "end_time_source": 8000,
            "start_time": 0,
            "end_time": 1200,
            "rate": "-50%",
        })

        assert 6 in t._preflight_indices(3)

    def test_repetition_detector(self):
        assert F5TTS._has_pathological_repetition("一种一种一种一种一种一种") is True
        assert F5TTS._has_pathological_repetition("这是一句正常且完整的中文配音") is False

    def test_zero_tolerance_flags_any_unexpected_latin(self, monkeypatch):
        from videotrans.configure.config import settings
        t = self._task(Path('/tmp'), count=1)
        monkeypatch.setitem(settings, 'f5tts_zero_unexpected_latin', True)

        assert t._has_unexpected_english('好的，不错', 'Harder不错') is True
        assert t._has_unexpected_english('SpaceX AI卫星', 'SpaceX AI卫星') is False

    def test_content_gate_catches_extra_and_truncated_chinese(self):
        t = self._task(Path('/tmp'), count=1)

        extra = t._hard_quality_failures(
            '是什么改变了，让我们觉得现在是时候开始',
            '浩瀚无垠的是什么改变了让我们觉得现在是时候开始')
        truncated = t._hard_quality_failures(
            '这是一个需要完整读出来的中文长句内容', '这是中文')

        assert 'unexpected_chinese_content' in extra
        assert 'truncated_chinese_content' in truncated

    def test_anchor_bank_matches_style_and_rotates_on_retry(self):
        t = self._task(Path('/tmp'), count=1)
        bank = [
            t._anchor_entry('/a.wav', '这是普通陈述句。', 6000),
            t._anchor_entry('/b.wav', '这是一个问题吗？', 6500),
            t._anchor_entry('/c.wav', '真的太好了！', 6200),
        ]
        item = {'text': '我们现在应该怎么做？'}

        first = t._choose_chinese_anchor(bank, item, retry_no=0)
        second = t._choose_chinese_anchor(bank, item, retry_no=1)

        assert first['wav'] == '/b.wav'
        assert second['wav'] != first['wav']

    def test_clean_preflight_becomes_chinese_anchor(self, tmp_path):
        t = self._task(tmp_path, count=3)
        for item in t.queue_tts:
            item['cluster_ref'] = '/refs/speaker-a.wav'
        _voice(Path(t.queue_tts[0]['filename']), 6.0, 110, 0.7, seed=91)
        samples = [(0, t.queue_tts[0], {'filename': str(tmp_path / 'sample.wav')})]

        count = t._bootstrap_chinese_anchors(
            samples, {0: '这是已经通过核验的中文音色锚点'}, [])

        assert count == 1
        assert t.queue_tts[1]['chinese_anchor_ref'] == t.queue_tts[0]['filename']
        assert t.queue_tts[1]['chinese_anchor_text'].endswith('。')
        assert len(t.queue_tts[1]['chinese_anchor_bank']) == 1

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
        report = json.loads((tmp_path / "preflight_report.json").read_text())
        assert report["status"] == "ready"
        assert report["sample_count"] == 2

    def test_failed_preflight_is_preserved_for_local_repair(self, tmp_path, monkeypatch):
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

        t._run_preflight()

        assert all(Path(item["filename"]).exists() for item in t.queue_tts)
        report = json.loads((tmp_path / "preflight_report.json").read_text())
        assert report["status"] == "needs_review"
        assert report["failed"] == 2

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

    def test_resource_pressure_recycles_before_next_clip(self, monkeypatch):
        from videotrans.dub.synthesis_supervisor import ResourceDecision
        from videotrans.util.resource_governor import ResourceSnapshot

        t = _f5([])
        t.api_url = "http://127.0.0.1:7860"
        t._resource_recycle_pending = False
        t._service_circuit_error = ""
        t._exit = lambda: False
        t.signal = lambda **kwargs: None
        events = []
        decisions = iter([
            ResourceDecision(
                False, True, "本轮 Swap 增长 2048 MB",
                ResourceSnapshot(memory_percent=84, available_mb=2200, swap_used_mb=4000),
                2048,
            ),
            ResourceDecision(
                True, False, "资源正常",
                ResourceSnapshot(memory_percent=60, available_mb=6000, swap_used_mb=4000),
                2048,
            ),
        ])

        class FakeSupervisor:
            def resource_decision(self):
                return next(decisions)

            def mark_recycle(self):
                events.append("recycle")

        t._synthesis_supervisor_obj = FakeSupervisor()
        t._local_service_is_ready = lambda: True
        t._stop_local_service = lambda: events.append("stop") or True
        t._start_local_service = lambda recovery=False: events.append("start") or True
        t._release_memory_pressure = lambda: events.append("release")
        monkeypatch.setattr("videotrans.tts._f5tts.time.sleep", lambda _seconds: None)

        assert t._wait_for_synthesis_resources(7) is True
        assert events == ["stop", "recycle", "release", "start"]

    def test_single_clip_sidecar_does_not_erase_other_failures(self, tmp_path):
        first = {
            "text": "第一个异常片段", "filename": str(tmp_path / "first.wav")}
        second = {
            "text": "另一个异常片段", "filename": str(tmp_path / "second.wav")}
        sidecar = tmp_path / "lang_leak.json"
        sidecar.write_text(
            '{"second.wav": "existing failure"}', encoding="utf-8")
        t = _f5([first])

        t._write_leak_sidecar([(0, first, "new failure")])

        import json
        marks = json.loads(sidecar.read_text(encoding="utf-8"))
        assert marks == {
            "first.wav": "new failure",
            "second.wav": "existing failure",
        }

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

    def test_remaining_leak_is_deferred_and_writes_marker(self, tmp_path, monkeypatch):
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
        # Candidate generation is staged: the first infrastructure failure
        # leaves the original clip intact and the next retry round can proceed.
        assert recovered == []
        assert len(validation_reads) == 2
        assert Path(item["filename"]).is_file()
        assert live_models == 0

    def test_service_disconnect_preserves_original_and_defers_repair(self, tmp_path, monkeypatch):
        wav = _voice(tmp_path / "failed-service.wav", 2.0, 110, 0.7, seed=42)
        original_audio = Path(wav).read_bytes()
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

        t._verify_chinese_outputs()

        assert Path(wav).read_bytes() == original_audio
        assert (tmp_path / "lang_leak.json").is_file()
        assert not list(tmp_path.glob(".*.quality-retry-*.wav"))

    def test_critical_memory_defers_automatic_repair_without_starting_f5(
            self, tmp_path):
        wav = _voice(tmp_path / "pressure.wav", 2.0, 110, 0.7, seed=43)
        original_audio = Path(wav).read_bytes()
        item = {
            "dub_unit_id": "pressure-unit",
            "text": "这是需要稍后返工的中文。",
            "filename": wav,
            "role": "clone",
        }
        t = _f5([item])
        t.uuid = "pressure-test"
        t.language = "zh-cn"
        t.is_test = False
        t.use_cache = False
        t.safe_ref_text = "English reference sentence."
        t._low_memory_profile = True
        t.signal = lambda **kwargs: None
        t._validator_identity = lambda: ("faster-whisper-cpu", "large-v3-turbo")
        t._transcribe_isolated_for_validation = (
            lambda indices, backend=None: {indices[0]: "This is leaked English speech"}
        )
        t._assign_chinese_anchors = lambda failed, transcripts: 0
        t._wait_for_f5_headroom = lambda: False
        starts = []
        t._start_local_service = lambda recovery=False: starts.append(recovery) or True

        t._verify_chinese_outputs()

        assert starts == []
        assert Path(wav).read_bytes() == original_audio
        assert (tmp_path / "lang_leak.json").is_file()


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
        assert all(it.get('speaker_cluster_id') for it in queue)
        assert all(1 <= len(it.get('cluster_ref_bank') or []) <= 3 for it in queue)

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
    def test_identity_contract_rejects_cross_speaker_reference(self, tmp_path):
        line_ref = _voice(tmp_path / "line.wav", 6.0, 110, 0.7, seed=201)
        identity_ref = _voice(
            tmp_path / "identity.wav", 6.0, 160, 0.7, seed=202
        )
        t = F5TTS.__new__(F5TTS)
        t.get_ref_wav = lambda item: (line_ref, "Line reference text.")

        with pytest.raises(DubbingSrtError, match="音色绑定冲突"):
            t._run({
                "role": "clone",
                "text": "这是中文配音。",
                "speaker_id": "musk",
                "speaker_identity_required": True,
                "cluster_ref": identity_ref,
                "cluster_ref_speaker_id": "host",
            }, 0)

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
            t._run({"role": "clone", "text": "你好",
                    "reference_mode": "source_clone",
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text.",
                    "chinese_anchor_ref": "/refs/zh.wav", "chinese_anchor_text": "中文参考。"}, 4)
            t._run({"role": "clone", "text": "这是问题。",
                    "reference_mode": "source_clone",
                    "cluster_ref": "/refs/spk1.wav", "cluster_ref_text": "Speaker one text.",
                    "prosody_plan": {"speech_act": "question"}}, 5)
            assert runs[0]["ref_audio_input"] == "/refs/spk1.wav"
            assert runs[1]["ref_audio_input"] == "/refs/global.wav"
            assert runs[2]["ref_audio_input"] == "/refs/zh.wav"
            assert runs[2]["ref_text_input"] == "中文参考。"
            assert runs[3]["ref_audio_input"] == "/refs/resume-zh.wav"
            assert runs[3]["ref_text_input"] == "恢复用中文参考。"
            assert runs[4]["ref_audio_input"] == "/refs/spk1.wav"
            assert runs[5]["gen_text_input"].endswith("？")
            t.resume_chinese_anchors = {}
            t.resume_chinese_anchor_ref = None
            t.resume_chinese_anchor_text = None
            with pytest.raises(DubbingSrtError, match="纯中文锚点模式缺少"):
                t._run({"role": "clone", "text": "没有锚点",
                        "reference_mode": "chinese_anchor_only",
                        "cluster_ref": "/refs/spk1.wav",
                        "cluster_ref_text": "Speaker one text."}, 6)
        finally:
            f5mod.AudioSegment = orig_seg


class TestSlotAwareSpeed:
    def test_global_slow_rate_is_preserved_when_slot_has_room(self):
        speed = F5TTS._slot_aware_speed(
            requested_speed=0.5,
            ref_text="A reasonably complete source sentence.",
            gen_text="短句。",
            ref_duration_ms=5000,
            target_duration_ms=6000,
            fit_to_slot=True,
        )
        assert speed == 0.5

    def test_long_chinese_line_is_generated_near_slot_not_at_half_speed(self):
        speed = F5TTS._slot_aware_speed(
            requested_speed=0.5,
            ref_text="of kardashov who thought about this and",
            gen_text="卡尔达肖夫考虑过这个问题，这是一个很好的分类方式，你可以评估。",
            ref_duration_ms=2120,
            target_duration_ms=5600,
            fit_to_slot=True,
        )
        assert 0.9 <= speed <= 1.3

    def test_non_smart_flow_keeps_requested_speed(self):
        speed = F5TTS._slot_aware_speed(
            requested_speed=0.5,
            ref_text="short",
            gen_text="很长的一段中文文本。",
            ref_duration_ms=5000,
            target_duration_ms=1000,
            fit_to_slot=False,
        )
        assert speed == 0.5
