import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from videotrans.dub.quality_manifest import QualityManifest
from videotrans.tts._f5tts import F5TTS
from videotrans.util import resource_governor


def _clip(path: Path, payload=b"RIFF-quality-test") -> str:
    path.write_bytes(payload)
    return str(path)


def _item(tmp_path, *, text="这是需要核验的中文。", payload=b"audio-a"):
    return {
        "dub_unit_id": "unit-1",
        "line": 1,
        "text": text,
        "filename": _clip(tmp_path / "clip.wav", payload),
    }


def test_quality_cache_requires_audio_text_model_and_rules_match(tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    item = _item(tmp_path)
    manifest = QualityManifest(tmp_path)
    args = {
        "validator_backend": "faster-whisper-cpu",
        "validator_model": "large-v3-turbo",
        "rules_version": "zh-v1",
    }
    manifest.record(item, passed=True, transcript="这是正常中文", **args)

    assert manifest.lookup(item, **args)["passed"] is True
    assert manifest.lookup({**item, "text": "文本已经变化。"}, **args) is None
    assert manifest.lookup(item, **{**args, "validator_model": "tiny"}) is None
    assert manifest.lookup(item, **{**args, "rules_version": "zh-v2"}) is None

    Path(item["filename"]).write_bytes(b"audio-b")
    assert manifest.lookup(item, **args) is None


def test_quality_disposition_tracks_local_repair_state(tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    item = _item(tmp_path)
    manifest = QualityManifest(tmp_path)
    manifest.record(
        item, passed=False, transcript="English leak",
        validator_backend="mlx-whisper-mps", validator_model="large-v3-turbo")

    manifest.set_disposition(
        item, "needs_review", attempts=2, reason="automatic retries exhausted")

    entry = manifest.entries["unit-1"]
    assert entry["disposition"] == "needs_review"
    assert entry["attempts"] == 2
    assert manifest.summary()["failed"] == 1


def test_quality_manifest_status_merges_back_into_legacy_queue(tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality
    from videotrans.task.trans_create import TransCreate

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    item = _item(tmp_path)
    manifest = QualityManifest(tmp_path)
    manifest.record(
        item, passed=False, transcript="English leak",
        validator_backend="faster-whisper-cpu", validator_model="large-v3-turbo",
        disposition="needs_review")
    (tmp_path / 'lang_leak.json').write_text(
        json.dumps({'clip.wav': 'English leak'}), encoding='utf-8')
    fake = SimpleNamespace(
        cfg=SimpleNamespace(cache_folder=str(tmp_path)), queue_tts=[item])

    TransCreate._merge_lang_leak_marks(fake)

    assert item['quality_status'] == 'needs_review'
    assert item['lang_leak'] == 'English leak'


def test_global_quality_result_materializes_into_new_project(tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _item(first_dir)
    args = {
        "validator_backend": "faster-whisper-cpu",
        "validator_model": "large-v3-turbo",
    }
    QualityManifest(first_dir).record(first, passed=True, transcript="已验收", **args)

    second = {**first, "filename": _clip(second_dir / "clip.wav", b"audio-a")}
    manifest = QualityManifest(second_dir)
    assert manifest.lookup(second, **args)["transcript"] == "已验收"
    manifest.save()
    payload = json.loads((second_dir / "quality_manifest.json").read_text())
    assert payload["entries"]["unit-1"]["passed"] is True


def test_f5_second_pass_reuses_persisted_quality_without_loading_model(
        tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    item = _item(tmp_path)

    first = F5TTS.__new__(F5TTS)
    first.queue_tts = [item]
    first.use_cache = True
    first.safe_ref_text = ""
    first.uuid = "quality-first"
    first.signal = lambda **_kwargs: None
    first._transcribe_isolated_for_validation = (
        lambda _indices, backend=None: {0: "这是需要核验的中文"}
    )
    first._verify_chinese_outputs()

    second = F5TTS.__new__(F5TTS)
    second.queue_tts = [dict(item)]
    second.use_cache = True
    second.safe_ref_text = ""
    second.uuid = "quality-second"
    messages = []
    second.signal = lambda **kwargs: messages.append(kwargs.get("text", ""))
    second._transcribe_isolated_for_validation = (
        lambda _indices, backend=None: pytest.fail("cached quality must not load validator")
    )
    second._verify_chinese_outputs()

    assert any("复用 1 段" in message for message in messages)


def test_low_memory_profile_caps_quality_neutral_concurrency(monkeypatch):
    monkeypatch.setattr(resource_governor.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(resource_governor.platform, "machine", lambda: "arm64")
    values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 16 * 1024 ** 3 // 4096}
    monkeypatch.setattr(resource_governor.os, "sysconf", lambda key: values[key])
    monkeypatch.setattr(resource_governor.os, "cpu_count", lambda: 10)

    profile = resource_governor.current_profile()
    assert profile.low_memory_apple_silicon is True
    assert profile.reference_workers == 2
    assert profile.separation_threads == 2
    assert profile.validator_cpu_threads == 4


def test_queue_quality_coverage_rejects_stale_audio_or_rules(tmp_path):
    from videotrans.dub.quality_manifest import (
        QualityManifest, queue_quality_coverage,
    )

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"first")
    item = {
        "dub_unit_id": "unit-1",
        "text": "这是一段需要核验的中文",
        "filename": str(audio),
    }
    manifest = QualityManifest(tmp_path)
    manifest.record(
        item,
        validator_backend="faster-whisper-cpu",
        validator_model="large-v3-turbo",
        rules_version="rules-current",
        passed=True,
        transcript=item["text"],
    )

    current = queue_quality_coverage(
        [item], tmp_path, rules_version="rules-current"
    )
    assert current["covered"] == 1
    assert current["missing"] == 0

    audio.write_bytes(b"changed")
    fast_reopen = queue_quality_coverage(
        [item], tmp_path, rules_version="rules-current", verify_audio_hashes=False
    )
    stale_audio = queue_quality_coverage(
        [item], tmp_path, rules_version="rules-current"
    )
    stale_rules = queue_quality_coverage(
        [item], tmp_path, rules_version="rules-next"
    )
    assert fast_reopen["covered"] == 1
    assert stale_audio["missing"] == 1
    assert stale_rules["missing"] == 1


def test_mlx_validator_failure_falls_back_to_same_size_cpu_model():
    task = F5TTS.__new__(F5TTS)
    calls = []
    messages = []
    task.signal = lambda **kwargs: messages.append(kwargs.get("text", ""))

    def transcribe(indices, backend=None):
        calls.append(backend)
        if backend == "mlx-whisper-mps":
            raise RuntimeError("Metal unavailable")
        return {indices[0]: "这是 CPU 大模型的回退结果"}

    task._transcribe_isolated_for_validation = transcribe
    result, backend = task._transcribe_isolated_with_fallback([3], "mlx-whisper-mps")

    assert backend == "faster-whisper-cpu"
    assert result[3].startswith("这是")
    assert calls == ["mlx-whisper-mps", "faster-whisper-cpu"]
    assert any("不降低核验等级" in message for message in messages)


def test_reference_readback_is_content_cached(tmp_path, monkeypatch):
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(
        quality, 'GLOBAL_REFERENCE_QUALITY_DIR', tmp_path / 'reference-quality')
    wav = _clip(tmp_path / 'reference.wav', b'reference-audio')
    candidate = (0, wav, 'This is the matching reference sentence.', 0, 6000)

    first = F5TTS.__new__(F5TTS)
    first._transcribe_one_for_validation = (
        lambda _model, _filename: 'This is the matching reference sentence.'
    )
    assert first._validate_candidates([candidate], object(), need=1) == [candidate]

    second = F5TTS.__new__(F5TTS)
    second._transcribe_one_for_validation = (
        lambda _model, _filename: pytest.fail('reference cache should avoid ASR')
    )
    assert second._validate_candidates([candidate], object(), need=1) == [candidate]


def test_backend_fallback_is_recorded_in_quality_manifest(tmp_path, monkeypatch):
    """核验后端回退后，质量记录必须写入回退后的后端名。

    否则记录上写着 MLX、实际用的是 CPU，下次按后端查表就会错配，
    要么白白重新核验，要么误采信另一后端的结论。
    （分解质量门禁时的变异测试发现此处无覆盖。）
    """
    import videotrans.dub.quality_manifest as quality

    monkeypatch.setattr(quality, "GLOBAL_QUALITY_DIR", tmp_path / "global")
    item = _item(tmp_path)

    task = F5TTS.__new__(F5TTS)
    task.queue_tts = [item]
    task.use_cache = False
    task.safe_ref_text = ""
    task.uuid = "backend-fallback"
    task.signal = lambda **_kwargs: None
    task._validator_identity = lambda: ("mlx-whisper-mps", "large-v3-turbo")

    def transcribe(indices, backend=None):
        if backend == "mlx-whisper-mps":
            raise RuntimeError("Metal unavailable")
        return {indices[0]: "这是需要核验的中文"}

    task._transcribe_isolated_for_validation = transcribe
    task._verify_chinese_outputs()

    manifest = quality.QualityManifest.for_queue([item])
    entry = manifest.lookup(
        item, index=0, validator_backend="faster-whisper-cpu",
        validator_model="large-v3-turbo",
        rules_version=F5TTS.QUALITY_RULES_VERSION)
    assert entry is not None, "回退后的后端未被写入质量记录"
    assert entry["passed"] is True
