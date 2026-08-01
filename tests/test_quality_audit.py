import json

from videotrans.component.timeline.quality_audit import QualityAuditWorker


def test_existing_audio_audit_persists_pass_and_failure(tmp_path, monkeypatch):
    clean = tmp_path / "clean.wav"
    mixed = tmp_path / "mixed.wav"
    clean.write_bytes(b"clean-audio")
    mixed.write_bytes(b"mixed-audio")
    queue = [
        {"line": 1, "dub_unit_id": "one", "text": "这是正常中文配音内容", "filename": str(clean)},
        {"line": 2, "dub_unit_id": "two", "text": "这是另一段中文配音", "filename": str(mixed)},
    ]
    worker = QualityAuditWorker(queue, tmp_path)
    monkeypatch.setattr(
        worker, "_validator_spec",
        lambda backend=None: (backend or "faster-whisper-cpu", "large-v3-turbo", "/model"),
    )
    monkeypatch.setattr(
        worker, "_run_validator",
        lambda files, backend, logs_file: ({
            0: "这是正常中文配音内容",
            1: "这是 another 中文配音",
        }, backend),
    )
    completed = []
    failures = []
    worker.done.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert failures == []
    assert completed[0][0]["passed"] is True
    assert completed[0][1]["passed"] is False
    assert "unexpected_english" in completed[0][1]["hard_failures"]
    payload = json.loads((tmp_path / "quality_manifest.json").read_text())
    assert payload["entries"]["one"]["passed"] is True
    assert payload["entries"]["two"]["passed"] is False
