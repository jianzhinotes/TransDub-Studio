from types import SimpleNamespace

from videotrans.mainwin._actions import WinAction


def test_translated_subtitle_only_job_opens_proof(tmp_path):
    from videotrans.task.only_one import (
        is_translated_subtitle_only, should_pause_for_subtitle_proof)

    target = tmp_path / 'zh.srt'
    target.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n', encoding='utf-8')
    trk = SimpleNamespace(
        should_trans=True,
        should_dubbing=False,
        should_hebing=True,
        cfg=SimpleNamespace(target_sub=str(target)),
    )
    assert is_translated_subtitle_only(trk) is True
    assert should_pause_for_subtitle_proof(trk) is True


def test_dubbed_or_untranslated_job_skips_subtitle_only_proof(tmp_path):
    from videotrans.task.only_one import (
        is_translated_subtitle_only, should_pause_for_subtitle_proof)

    target = tmp_path / 'zh.srt'
    target.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n', encoding='utf-8')
    base = dict(should_hebing=True, cfg=SimpleNamespace(target_sub=str(target)))
    dubbed = SimpleNamespace(should_trans=True, should_dubbing=True, **base)
    untranslated = SimpleNamespace(should_trans=False, should_dubbing=False, **base)
    assert is_translated_subtitle_only(dubbed) is False
    assert is_translated_subtitle_only(untranslated) is False
    assert should_pause_for_subtitle_proof(dubbed) is False
    assert should_pause_for_subtitle_proof(untranslated) is False


def test_source_signature_ignores_srt_formatting_only(tmp_path):
    from videotrans.task.only_one import _subtitle_signature

    path = tmp_path / 'source.srt'
    path.write_text(
        '1\n00:00:00,000 --> 00:00:01,000\nHello  \n\n', encoding='utf-8')
    first = _subtitle_signature(str(path))
    path.write_text(
        '1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
    assert _subtitle_signature(str(path)) == first


def test_no_voice_skips_tts_provider_validation(monkeypatch):
    """Subtitle-only videos must not depend on a configured TTS backend."""
    main = SimpleNamespace(
        voice_role=SimpleNamespace(currentText=lambda: 'No'),
        tts_type=SimpleNamespace(currentIndex=lambda: 8),
        target_language=SimpleNamespace(currentText=lambda: '简体中文'),
    )
    action = SimpleNamespace(main=main)
    monkeypatch.setattr('videotrans.mainwin._actions.tts.is_input_api',
                        lambda **_kwargs: False)

    assert WinAction.check_tts(action) is True
