import json
import sys
import types

from videotrans.process.quality_validator import validate_faster_whisper_files
from videotrans.tts._f5tts import F5TTS


class _Segment:
    def __init__(self, text):
        self.text = text


def test_validator_resumes_after_worker_failure(tmp_path, monkeypatch):
    checkpoint = tmp_path / "validation-checkpoint.json"
    identity = {
        "validator_backend": "faster-whisper-cpu",
        "validator_model": "large-v3-turbo",
        "rules_version": "zh-v3",
    }
    calls = []

    class FailingModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, filename, **_kwargs):
            calls.append(filename)
            if filename.endswith("two.wav"):
                raise RuntimeError("simulated worker crash")
            return [_Segment("第一段")], None

    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=FailingModel),
    )
    files = [(0, "one.wav", "signature-one"), (1, "two.wav", "signature-two")]
    result, error = validate_faster_whisper_files(
        files=files,
        model_path=tmp_path,
        checkpoint_file=checkpoint,
        checkpoint_identity=identity,
    )
    assert result is False
    assert "simulated worker crash" in error
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["entries"]["signature-one"]["transcript"] == "第一段"

    resumed_calls = []

    class ResumedModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, filename, **_kwargs):
            resumed_calls.append(filename)
            return [_Segment("第二段")], None

    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=ResumedModel),
    )
    result, error = validate_faster_whisper_files(
        files=files,
        model_path=tmp_path,
        checkpoint_file=checkpoint,
        checkpoint_identity=identity,
    )

    assert error is None
    assert result == {0: "第一段", 1: "第二段"}
    assert resumed_calls == ["two.wav"]


def test_validator_checkpoint_identity_prevents_stale_reuse(tmp_path, monkeypatch):
    checkpoint = tmp_path / "validation-checkpoint.json"
    checkpoint.write_text(json.dumps({
        "identity": {"validator_model": "tiny"},
        "entries": {"same-signature": {"transcript": "过期结果"}},
    }), encoding="utf-8")
    calls = []

    class Model:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, filename, **_kwargs):
            calls.append(filename)
            return [_Segment("重新核验")], None

    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=Model),
    )
    result, error = validate_faster_whisper_files(
        files=[(0, "clip.wav", "same-signature")],
        model_path=tmp_path,
        checkpoint_file=checkpoint,
        checkpoint_identity={"validator_model": "large-v3-turbo"},
    )

    assert error is None
    assert result[0] == "重新核验"
    assert calls == ["clip.wav"]


def test_f5_isolated_validator_passes_content_signatures_and_stable_checkpoint(
        tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"audio-payload")
    task = F5TTS.__new__(F5TTS)
    task.queue_tts = [{
        "dub_unit_id": "unit-1",
        "text": "需要核验的中文。",
        "filename": str(clip),
    }]
    task.uuid = "checkpoint-test"
    task._get_validator_model_path = lambda: tmp_path / "model"
    captured = {}

    def run_process(**kwargs):
        captured.update(kwargs)
        return {0: "需要核验的中文"}

    task._new_process = run_process
    result = task._transcribe_isolated_for_validation(
        [0], backend="faster-whisper-cpu")

    assert result[0].startswith("需要")
    row = captured["kwargs"]["files"][0]
    assert row[:2] == (0, str(clip))
    assert len(row[2]) == 64
    checkpoint = captured["kwargs"]["checkpoint_file"]
    assert str(tmp_path) in checkpoint
    assert checkpoint.endswith(".json")
