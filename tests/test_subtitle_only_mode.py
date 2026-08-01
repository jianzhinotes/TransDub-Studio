from types import SimpleNamespace

from videotrans.mainwin._actions import WinAction


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
