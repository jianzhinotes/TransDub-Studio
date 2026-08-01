import importlib.util
from pathlib import Path

import pytest

from videotrans.dub.contextual_repair import contextual_chinese_anchor_bank


def _item(path, text, *, index, speaker="", failed=False):
    Path(path).write_bytes(b"RIFF-context-anchor")
    item = {
        "text": text,
        "filename": str(path),
        "dubbing_s": 4.0,
        "ref_wav": str(Path(path).with_name(f"ref-{index}.wav")),
        "speaker_cluster_id": speaker,
    }
    if failed:
        item.update({"lang_leak": "English", "quality_status": "needs_review"})
    return item


def test_contextual_bank_uses_clean_chinese_and_skips_failed(tmp_path):
    queue = [
        _item(tmp_path / "0.wav", "这是已经通过核验的中文片段", index=0),
        _item(tmp_path / "1.wav", "这里提到了XAI平台名称", index=1),
        _item(tmp_path / "2.wav", "这是需要修复的目标片段", index=2, failed=True),
        _item(tmp_path / "3.wav", "这是邻近且干净的中文音色参考", index=3),
        _item(tmp_path / "4.wav", "这个片段本身质量没有通过", index=4, failed=True),
    ]

    bank = contextual_chinese_anchor_bank(queue, 2)

    assert bank
    assert bank[0]["wav"] == queue[3]["filename"]
    assert all(entry["wav"] != queue[4]["filename"] for entry in bank)
    assert all(entry["text"].endswith("。") for entry in bank)


def test_contextual_bank_never_crosses_explicit_speaker_identity(tmp_path):
    queue = [
        _item(tmp_path / "a.wav", "甲说话人的干净中文参考内容", index=0, speaker="a"),
        _item(tmp_path / "target.wav", "甲说话人的待修复目标内容", index=1,
              speaker="a", failed=True),
        _item(tmp_path / "b.wav", "乙说话人的干净中文参考内容", index=2, speaker="b"),
    ]

    bank = contextual_chinese_anchor_bank(queue, 1)

    assert [entry["wav"] for entry in bank] == [queue[0]["filename"]]


if importlib.util.find_spec("PySide6") is None:
    pytest.skip("requires real PySide6", allow_module_level=False)


def test_redub_stages_candidate_and_preserves_original_until_promotion(
        tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from pydub import AudioSegment
    import videotrans.component.timeline.redub as redub
    from videotrans.component.timeline.studio_state import StudioState

    original = tmp_path / "target.wav"
    AudioSegment.silent(duration=3000).export(original, format="wav")
    old_bytes = original.read_bytes()
    anchor = tmp_path / "anchor.wav"
    AudioSegment.silent(duration=4000).export(anchor, format="wav")
    items = [
        {
            "text": "这是需要修复的中文目标片段", "filename": str(original),
            "dubbing_s": 3.0, "tts_type": 8, "quality_status": "needs_review",
            "lang_leak": "English", "role": "clone", "ref_wav": str(original),
        },
        {
            "text": "这是已经通过核验的中文音色参考", "filename": str(anchor),
            "dubbing_s": 4.0, "tts_type": 8, "role": "clone", "ref_wav": str(anchor),
        },
    ]
    state = StudioState(items, 8000)

    class FakeReDubb(QObject):
        uito = Signal(str)

        def __init__(self, *, parent=None, idx=0, tts_dict=None, language=None,
                     original_filename=None):
            super().__init__(parent)
            self.idx = idx
            self.tts_dict = tts_dict
            self.language = language
            self.original_filename = original_filename
            self.staged_filename = tts_dict["filename"]

        def start(self):
            return None

    monkeypatch.setattr(redub, "ReDubb", FakeReDubb)
    queue = redub.RedubQueue(state, "zh-cn")
    queue.enqueue(0)

    thread = queue._current[1]
    assert original.read_bytes() == old_bytes
    assert thread.staged_filename != str(original)
    assert thread.tts_dict["chinese_anchor_bank"][0]["wav"] == str(anchor)

    AudioSegment.silent(duration=2500).export(thread.staged_filename, format="wav")
    queue._on_done("ok:0")

    assert original.read_bytes() != old_bytes
    assert not state.quality_failed_indices()
    assert state.items[0]["dubbing_s"] == 2.5
