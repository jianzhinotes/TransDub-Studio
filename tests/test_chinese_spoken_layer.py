from types import SimpleNamespace

from videotrans.dub.chinese_quality import hard_quality_failures
from videotrans.dub.chinese_spoken import prepare_chinese_spoken_text
from videotrans.task.trans_create import TransCreate


def test_decimal_dimension_uses_unambiguous_tts_text_but_keeps_subtitle_form():
    subtitle = "你会说那张纸在合页上方6.5英寸的地方"
    spoken = prepare_chinese_spoken_text(subtitle)

    assert spoken == "你会说那张纸在合页上方六点五英寸的地方"
    # QA already treats the readable subtitle notation and spoken notation as
    # the same number, so it will still catch a real error such as 4%.
    assert hard_quality_failures(spoken, subtitle) == []
    assert "numeric_content_mismatch" in hard_quality_failures(
        spoken, "你会说那张纸在合页上方4%的地方")


def test_percentages_ranges_and_large_integers_are_rendered_for_speech():
    assert prepare_chinese_spoken_text("每平方米1400瓦，效率85%") == (
        "每平方米一千四百瓦，效率百分之八十五")
    assert prepare_chinese_spoken_text("距离5-8厘米") == "距离五到八厘米"


def test_task_keeps_viewer_subtitles_and_invalidates_old_decimal_audio(tmp_path):
    old_filename = tmp_path / "smart-38-old.wav"
    item = {
        "line": 39,
        "text": "你会说那张纸在合页上方6.5英寸的地方",
        "filename": str(old_filename),
        "dub_unit_id": "dimension-line",
        "role": "clone",
        "tts_type": 8,
    }
    fake = SimpleNamespace(
        cfg=SimpleNamespace(target_language_code="zh-cn", cache_folder=str(tmp_path)),
        queue_tts=[item],
    )

    TransCreate._prepare_chinese_spoken_payloads(fake)

    assert item["text"] == "你会说那张纸在合页上方6.5英寸的地方"
    assert item["spoken_text"] == "你会说那张纸在合页上方六点五英寸的地方"
    assert item["filename"] != str(old_filename)
    assert "/spoken-39-" in item["filename"]
