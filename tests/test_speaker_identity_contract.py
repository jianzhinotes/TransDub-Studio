import wave

from videotrans.dub.legacy_adapter import make_project_id, project_from_queue
from videotrans.dub.planner import JointDubPlanner
from videotrans.dub.speaker_identity import prepare_speaker_contract


def _source(path, seconds=36):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * seconds)
    return str(path)


def _rows():
    return [
        {
            "line": index + 1,
            "text": f"第{index + 1}句中文。",
            "ref_text": f"Complete source sentence number {index + 1}.",
            "start_time": index * 6000,
            "end_time": (index + 1) * 6000,
            "start_time_source": index * 6000,
            "end_time_source": (index + 1) * 6000,
            "role": "clone",
            "tts_type": 8,
            "filename": "",
        }
        for index in range(6)
    ]


def test_auto_contract_persists_distinct_speaker_anchors(tmp_path, monkeypatch):
    source = _source(tmp_path / "source.wav")
    rows = _rows()
    monkeypatch.setattr(
        "videotrans.util.speaker_cluster.label_speakers",
        lambda paths: {index: 0 if index < 3 else 1 for index in range(len(paths))},
    )

    report = prepare_speaker_contract(
        rows, source_audio=source, work_dir=tmp_path / "contract"
    )

    assert report["status"] == "ready"
    assert report["method"] == "auto_mfcc"
    assert [row["speaker_id"] for row in rows] == [
        "spk0", "spk0", "spk0", "spk1", "spk1", "spk1"
    ]
    assert rows[0]["cluster_ref"] != rows[-1]["cluster_ref"]
    assert all(row["speaker_identity_required"] for row in rows)
    assert all(
        row["cluster_ref_speaker_id"] == row["speaker_id"] for row in rows
    )


def test_explicit_diarization_is_authoritative(tmp_path, monkeypatch):
    source = _source(tmp_path / "source.wav")
    rows = _rows()
    for index, row in enumerate(rows):
        row["speaker_id"] = "guest" if index < 2 else "host"
    monkeypatch.setattr(
        "videotrans.util.speaker_cluster.label_speakers",
        lambda _paths: (_ for _ in ()).throw(AssertionError("must not recluster")),
    )

    report = prepare_speaker_contract(
        rows, source_audio=source, work_dir=tmp_path / "contract"
    )

    assert report["method"] == "explicit"
    assert [row["speaker_id"] for row in rows[:2]] == ["guest", "guest"]
    assert {row["speaker_id"] for row in rows[2:]} == {"host"}


def test_tiny_explicit_diarization_artifacts_merge_into_single_presenter(tmp_path):
    source = _source(tmp_path / "source.wav")
    rows = _rows()
    for row in rows:
        row["speaker_id"] = "presenter"
    # A 1.5-second false-positive label amid 30 seconds of the presenter must
    # not fail clone preflight for lack of its own reference anchor.
    rows[-1]["speaker_id"] = "noise"
    rows[-1]["start_time"] = rows[-1]["start_time_source"] = 30_000
    rows[-1]["end_time"] = rows[-1]["end_time_source"] = 31_500

    report = prepare_speaker_contract(
        rows, source_audio=source, work_dir=tmp_path / "contract")

    assert report["status"] == "ready"
    assert report["method"] == "explicit_transient_collapsed"
    assert set(row["speaker_id"] for row in rows) == {"presenter"}


def test_user_or_curated_identity_anchor_is_not_replaced(tmp_path):
    source = _source(tmp_path / "source.wav")
    curated = _source(tmp_path / "curated-musk.wav", seconds=8)
    rows = _rows()
    for row in rows:
        row["speaker_id"] = "musk"
        row["speaker_identity_ref"] = curated
        row["speaker_identity_text"] = "A curated clean Musk reference."

    report = prepare_speaker_contract(
        rows, source_audio=source, work_dir=tmp_path / "contract"
    )

    assert report["speakers"]["musk"]["anchor"]["wav"] == curated
    assert report["speakers"]["musk"]["anchor"]["provided"] is True
    assert all(row["cluster_ref"] == curated for row in rows)


def test_identity_contract_reaches_planner_request(tmp_path):
    source = _source(tmp_path / "source.wav")
    rows = _rows()
    for row in rows:
        row["speaker_id"] = "musk"
        row["speaker_identity_ref"] = source
        row["speaker_identity_text"] = "A clean identity reference sentence."
        row["speaker_identity_required"] = True
        row["speaker_identity_source"] = "diarization"
    project = project_from_queue(
        rows,
        project_id=make_project_id("/video/interview.mp4", "zh-cn"),
        name="interview",
        source_language="en",
        target_language="zh-cn",
    )
    planner = JointDubPlanner()
    plan = planner.optimize(project, limit=1)
    segment = plan.segments[0]
    candidate = next(
        item for item in segment.text_candidates
        if item.id == segment.selected_text_candidate_id
    )

    request = planner._request(
        project, segment, candidate, 1, tmp_path / "candidates",
        {unit.id: unit for unit in project.units},
    )

    assert project.speakers[0].identity_reference == source
    assert request.legacy_payload["cluster_ref"] == source
    assert request.legacy_payload["cluster_ref_speaker_id"] == "musk"
    assert request.legacy_payload["speaker_identity_required"] is True
