from videotrans.dub.llm_candidates import (
    has_latin_speech_token,
    localize_chinese_spoken_terms,
)


def test_interview_proper_nouns_are_localized_for_chinese_tts():
    text = (
        "你可能在《SharkTank》见过我。还有NBC和Fox News。"
        "我要回SpyRanch，把书送交CIA审核，或者写个R。"
    )

    localized = localize_chinese_spoken_terms(text)

    assert "创智赢家" in localized
    assert "美国全国广播公司" in localized
    assert "福克斯新闻" in localized
    assert "斯派牧场" in localized
    assert "美国中央情报局" in localized
    assert "阿尔" in localized
    assert not has_latin_speech_token(localized)


def test_unknown_acronym_uses_chinese_letter_names_instead_of_failing():
    localized = localize_chinese_spoken_terms("这是 SDR 和 Q 的测试。")

    assert "艾斯迪阿尔" in localized
    assert "丘" in localized
    assert not has_latin_speech_token(localized)
