import json
from types import SimpleNamespace

import pytest

from videotrans.configure.excepts import DubbingSrtError
from videotrans.dub.semantic_guard import audit_translation_pair
from videotrans.task._base import BaseTask
from videotrans.task.trans_create import TransCreate


def _row(text, start, end):
    return {
        "line": 1, "text": text, "start_time": start, "end_time": end,
        "startraw": "00:00:00,000", "endraw": "00:00:01,000",
        "time": "00:00:00,000 --> 00:00:01,000",
    }


def test_source_aligned_translation_checkpoint_survives_final_resegmentation(tmp_path):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        target_dir=str(tmp_path), noextname="demo", target_language_code="zh-cn")
    source = [_row("One", 0, 1000), _row("Two", 1000, 2000)]
    target = [_row("一", 0, 1000), _row("二", 1000, 2000)]

    task._save_translation_checkpoint(source, target)
    # The public final subtitle may now contain one merged segment.  Loading
    # the internal checkpoint must still recover the two source-aligned rows.
    recovered = task._load_translation_checkpoint(source)

    assert [item["text"] for item in recovered] == ["一", "二"]
    assert task._load_translation_checkpoint([_row("Changed", 0, 1000)]) is None


def test_blank_translated_tail_remains_aligned_and_joins_previous_voice_unit():
    source = [
        _row("because Earth is seventy percent", 0, 1000),
        _row("water", 1000, 1400),
        _row("Next thought", 1600, 2400),
    ]
    target = [
        _row("因为地球百分之七十是水", 0, 1000),
        _row("", 1000, 1400),
        _row("下一个观点", 1600, 2400),
    ]

    assert TransCreate._translation_is_source_aligned(source, target)
    groups = TransCreate._group_source_aligned_subtitles(target, source)

    assert len(groups) == 2
    assert [row["text"] for row in groups[0][1]] == [
        "because Earth is seventy percent", "water"]
    assert groups[0][0]["text"] == "因为地球百分之七十是水"


def test_translation_stage_rebuilds_an_existing_resegmented_output(tmp_path, monkeypatch):
    import videotrans.task.trans_create as transmod

    source_path = tmp_path / "en.srt"
    target_path = tmp_path / "zh-cn.srt"
    source_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nOne\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nTwo\n",
        encoding="utf-8")
    # This represents a final smart-dubbing subtitle and must not be accepted
    # as a one-to-one translation cache merely because the file exists.
    target_path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n一、二\n",
        encoding="utf-8")
    calls = []

    def fake_translate(**kwargs):
        calls.append(kwargs)
        rows = kwargs["text_list"]
        rows[0]["text"] = "一"
        rows[1]["text"] = "二"
        return rows

    monkeypatch.setattr(transmod, "run_trans", fake_translate)
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        source_sub=str(source_path), target_sub=str(target_path),
        target_dir=str(tmp_path), noextname="demo", smart_orchestration=True,
        translate_type=1, source_language_code="en", target_language_code="zh-cn",
        app_mode="normal", fix_punc=0, output_srt=0,
    )
    task.should_trans = True
    task.should_dubbing = True
    task.precent = 0
    task.uuid = "test"
    task.signal = lambda **_kwargs: None
    task._exit = lambda: False

    task.trans()

    assert len(calls) == 1
    assert len(transmod.get_subtitle_from_srt(target_path)) == 2
    assert (tmp_path / "demo.tdproj/checkpoints/translation/source_aligned.json").is_file()


def test_translation_timestamp_typo_is_rebound_to_source_timeline():
    source = [_row(f"source-{index}", index * 1000, (index + 1) * 1000)
              for index in range(10)]
    target = [_row(f"译文-{index}", index * 1000, (index + 1) * 1000)
              for index in range(10)]
    target[6]["start_time"] = 20

    aligned = BaseTask.check_target_sub(SimpleNamespace(), source, target)

    assert [item["text"] for item in aligned] == [f"译文-{index}" for index in range(10)]
    assert [(item["start_time"], item["end_time"]) for item in aligned] == [
        (item["start_time"], item["end_time"]) for item in source]


def test_translation_rejects_a_grossly_unrelated_timeline():
    source = [_row(f"source-{index}", index * 1000, (index + 1) * 1000)
              for index in range(10)]
    target = [_row(f"译文-{index}", 100_000 + index * 1000, 101_000 + index * 1000)
              for index in range(10)]

    with pytest.raises(DubbingSrtError, match="时间轴可信度过低"):
        BaseTask.check_target_sub(SimpleNamespace(), source, target)


def test_srt_translation_migrates_legacy_cache_without_api_call(tmp_path, monkeypatch):
    import videotrans.translator._base as basemod
    from videotrans.translator._base import BaseTrans

    monkeypatch.setattr(basemod, "TEMP_ROOT", str(tmp_path))
    source = [_row("One", 0, 1000), _row("Two", 1000, 2000)]
    translator = object.__new__(BaseTrans)
    translator.text_list = source
    translator.trans_thread = 100
    translator.wait_sec = 0
    translator.is_test = False
    translator.translate_type = 1
    translator.api_url = "https://example.invalid"
    translator.aisendsrt = True
    translator.model_name = "test"
    translator.source_code = "en"
    translator.target_code = "zh-cn"
    translator.prompt = "prompt"
    translator.cache_dir = str(tmp_path / "translate_cache")
    translator.signal = lambda **_kwargs: None
    translator._exit = lambda: False
    legacy_result = (
        "1\n00:00:00,000 --> 00:00:01,000\n一\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n二\n")
    (tmp_path / "translate_cache").mkdir()
    translator._set_cache(source, legacy_result)
    translator._item_task = lambda _data: pytest.fail("API must not be called")

    result = translator._run_srt([source])

    assert [item["text"] for item in result] == ["一", "二"]
    srt_key = "\n\n".join(
        f"{item['line']}\n{item['time']}\n{item['text']}" for item in source)
    assert translator._get_cache(srt_key) == translator._srt_batch_text(result)


def _translator_for_srt_test(tmp_path, source):
    from videotrans.translator._base import BaseTrans

    translator = object.__new__(BaseTrans)
    translator.text_list = source
    translator.trans_thread = len(source)
    translator.wait_sec = 0
    translator.is_test = False
    translator.translate_type = 4
    translator.api_url = "https://example.invalid"
    translator.aisendsrt = True
    translator.model_name = "test"
    translator.source_code = "en"
    translator.target_code = "zh-cn"
    translator.prompt = "prompt"
    translator.cache_dir = str(tmp_path / "translate_cache")
    translator.signal = lambda **_kwargs: None
    translator._exit = lambda: False
    return translator


def test_srt_translation_splits_a_batch_that_drops_a_block(tmp_path):
    source = [_row("First", 0, 1000), _row("Second", 1000, 2000)]
    source[0]["line"], source[0]["time"] = 1, "00:00:00,000 --> 00:00:01,000"
    source[1]["line"], source[1]["time"] = 2, "00:00:01,000 --> 00:00:02,000"
    translator = _translator_for_srt_test(tmp_path, source)
    calls = []

    def fake_item_task(srt):
        calls.append(srt)
        rows = [row for row in source if row["time"] in srt]
        # Simulate an LLM that drops/merges a block only for a larger batch.
        if len(rows) > 1:
            return "1\n00:00:00,000 --> 00:00:01,000\n第一第二"
        return translator._srt_batch_text([
            dict(rows[0], text="第一" if rows[0]["line"] == 1 else "第二")])

    translator._item_task = fake_item_task
    result = translator._run_srt([source])

    assert [item["text"] for item in result] == ["第一", "第二"]
    assert len(calls) == 3
    # The repaired parent cache must now be resumable without another API call.
    translator._item_task = lambda _srt: pytest.fail("validated cache should be reused")
    assert [item["text"] for item in translator._run_srt([source])] == ["第一", "第二"]


def test_srt_translation_rejects_semantically_shifted_numeric_batch(tmp_path):
    source = [
        _row("How large is it", 0, 1000),
        _row("It is 99.86 percent", 1000, 2000),
    ]
    source[0]["line"], source[0]["time"] = 1, "00:00:00,000 --> 00:00:01,000"
    source[1]["line"], source[1]["time"] = 2, "00:00:01,000 --> 00:00:02,000"
    translator = _translator_for_srt_test(tmp_path, source)
    calls = []

    def fake_item_task(srt):
        calls.append(srt)
        if source[0]["time"] in srt and source[1]["time"] in srt:
            return (
                "1\n00:00:00,000 --> 00:00:01,000\n它占99.86%\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n它很大")
        if source[0]["time"] in srt:
            return "1\n00:00:00,000 --> 00:00:01,000\n它有多大"
        return "2\n00:00:01,000 --> 00:00:02,000\n它占99.86%"

    translator._item_task = fake_item_task
    result = translator._run_srt([source])

    assert [item["text"] for item in result] == ["它有多大", "它占99.86%"]
    assert len(calls) == 3


def test_bad_translation_checkpoint_is_not_resumed(tmp_path):
    task = object.__new__(TransCreate)
    task.cfg = SimpleNamespace(
        target_dir=str(tmp_path), noextname="demo", target_language_code="zh-cn")
    source = [_row("How large", 0, 1000), _row("99.86 percent", 1000, 2000)]
    target = [_row("它占99.86%", 0, 1000), _row("它很大", 1000, 2000)]
    root = tmp_path / "demo.tdproj/checkpoints/translation"
    root.mkdir(parents=True)
    (root / "source_aligned.json").write_text(json.dumps(target), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "source_fingerprint": task._subtitle_fingerprint(source),
        "target_fingerprint": task._subtitle_fingerprint(target),
        "source_count": 2,
        "target_count": 2,
    }), encoding="utf-8")

    assert task._load_translation_checkpoint(source) is None


def test_smart_mapping_requires_complete_ordered_source_coverage():
    fake = SimpleNamespace(queue_tts=[
        {"dub_unit_id": "source-a"}, {"dub_unit_id": "source-b"},
        {"dub_unit_id": "source-c"},
    ])
    valid = [
        {"source_unit_ids": ["source-a", "source-b"]},
        {"source_unit_ids": ["source-c"]},
    ]
    TransCreate._assert_smart_mapping(fake, valid)

    with pytest.raises(DubbingSrtError, match="映射不完整"):
        TransCreate._assert_smart_mapping(
            fake, [{"source_unit_ids": ["source-a", "source-c"]}])
    with pytest.raises(DubbingSrtError, match="映射不完整"):
        TransCreate._assert_smart_mapping(fake, [
            {"source_unit_ids": ["source-a", "source-b", "source-b", "source-c"]},
        ])


def test_checkpoint_fingerprint_rejects_a_different_translation(tmp_path):
    fake = SimpleNamespace(queue_tts=[{
        "dub_unit_id": "source-a", "ref_text": "source", "text": "译文",
        "start_time_source": 0, "end_time_source": 1000,
    }])
    cached = [{
        "source_unit_ids": ["source-a"], "ref_text": "source", "text": "译文",
    }]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 2, "input_fingerprint": "old-input",
    }), encoding="utf-8")

    assert not TransCreate._smart_checkpoint_matches(
        fake, cached, "new-input", manifest)


def test_semantic_guard_catches_unexplained_power_density():
    failures = audit_translation_pair(
        "Do people even really understand what mass to orbit becomes",
        "以及每平方米1400瓦以上",
    )

    assert "unexpected_number:1400" in failures
    assert "unexpected_unit:square_meter" in failures
    assert "unexpected_unit:watt" in failures


def test_semantic_guard_accepts_matching_number_and_units():
    assert audit_translation_pair(
        "and above 1400 watts per square meter",
        "以及每平方米1400瓦以上",
    ) == []


@pytest.mark.parametrize("source,target", [
    ("put 100 gigawatts or ultimately a terawatt into space", "输送100吉瓦，最终是1太瓦"),
    ("a 150 kW peak and 120 MW average", "峰值150千瓦，平均120兆瓦"),
])
def test_semantic_guard_accepts_prefixed_watt_units(source, target):
    assert audit_translation_pair(source, target) == []
