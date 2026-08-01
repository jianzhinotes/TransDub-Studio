from videotrans.dub.chinese_quality import (
    chinese_similarity,
    hard_quality_failures,
    quality_metrics,
)


def test_spoken_percentages_match_asr_digits():
    expected = "我觉得目前大约在百分之八十五到百分之九十之间。"
    transcript = "我觉得目前大约在85%到90%之间"

    assert hard_quality_failures(expected, transcript) == []
    assert chinese_similarity(expected, transcript) == 1.0
    assert quality_metrics(expected, transcript)["expected_numbers"] == ["85%", "90%"]


def test_spoken_integer_matches_asr_digits():
    assert hard_quality_failures("每平方米一千四百瓦", "每平方米1400瓦") == []
    assert chinese_similarity("每平方米一千四百瓦", "每平方米1400瓦") == 1.0


def test_changed_number_is_a_hard_failure():
    failures = hard_quality_failures("每平方米一千四百瓦", "每平方米250瓦")

    assert "numeric_content_mismatch" in failures


def test_mixed_arabic_chinese_large_number_matches_spoken_form():
    expected = "每年一百万吨左右"
    transcript = "每年100万吨左右"

    assert hard_quality_failures(expected, transcript) == []
    assert chinese_similarity(expected, transcript) == 1.0


def test_mixed_large_number_change_is_still_rejected():
    failures = hard_quality_failures("每年一百万吨", "每年120万吨")

    assert "numeric_content_mismatch" in failures
