import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from videotrans.task.trans_create import TransCreate


def test_smart_orchestration_resumes_materialized_queue(tmp_path):
    checkpoint = tmp_path / 'output' / '.smart-plan'
    checkpoint.mkdir(parents=True)
    saved = [{
        'line': 1, 'text': '已经智能编排', 'ref_text': 'Already planned',
        'start_time': 0, 'end_time': 1200,
        'filename': '/expired/cache/smart-0.wav',
        'ref_wav': '/expired/cache/clone-smart-0.wav',
    }]
    (checkpoint / 'smart_queue.json').write_text(
        json.dumps(saved, ensure_ascii=False), encoding='utf-8')
    signals = []
    written = []
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            target_language_code='zh-cn', target_dir=str(tmp_path / 'output'),
            cache_folder=str(tmp_path / 'new-cache'), clear_cache=False,
            noextname='demo'),
        queue_tts=[{'text': '旧文案'}],
        signal=lambda **kwargs: signals.append(kwargs.get('text')),
        _save_srt_target=lambda rows, path: written.append((rows, path)),
    )
    fake.cfg.target_sub = str(tmp_path / 'output' / 'zh-cn.srt')

    TransCreate._smart_orchestrate_queue(fake)

    assert fake.queue_tts[0]['text'] == '已经智能编排'
    assert Path(fake.queue_tts[0]['filename']).parent == tmp_path / 'new-cache'
    assert Path(fake.queue_tts[0]['ref_wav']).parent == tmp_path / 'new-cache'
    assert written and written[0][1] == fake.cfg.target_sub
    assert signals
    migrated = checkpoint.parent / 'demo.tdproj' / 'checkpoints' / 'smart-plan' / 'smart_queue.json'
    assert migrated.is_file()


def test_smart_orchestration_prefers_project_checkpoint(tmp_path):
    checkpoint = (tmp_path / 'output' / 'demo.tdproj'
                  / 'checkpoints' / 'smart-plan')
    checkpoint.mkdir(parents=True)
    saved = [{
        'line': 1, 'text': '工程内断点', 'ref_text': 'Project checkpoint',
        'filename': '/old/smart-0.wav', 'ref_wav': '',
    }]
    (checkpoint / 'smart_queue.json').write_text(
        json.dumps(saved, ensure_ascii=False), encoding='utf-8')
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            target_language_code='zh-cn', target_dir=str(tmp_path / 'output'),
            cache_folder=str(tmp_path / 'cache'), clear_cache=False,
            noextname='demo', target_sub=str(tmp_path / 'zh.srt')),
        queue_tts=[{'text': '旧文案'}], signal=lambda **_kwargs: None,
        _save_srt_target=lambda _rows, _path: None,
    )

    TransCreate._smart_orchestrate_queue(fake)

    assert fake.queue_tts[0]['text'] == '工程内断点'


def test_non_chinese_target_keeps_existing_queue(tmp_path):
    original = [{'text': 'Bonjour'}]
    fake = SimpleNamespace(
        cfg=SimpleNamespace(target_language_code='fr'),
        queue_tts=list(original), signal=lambda **_kwargs: None)

    TransCreate._smart_orchestrate_queue(fake)

    assert fake.queue_tts == original


def test_clone_reference_is_cut_from_source_timeline(tmp_path, monkeypatch):
    import videotrans.task.trans_create as transmod

    source = tmp_path / 'source.wav'
    source.write_bytes(b'not-decoded-by-this-unit-test')
    calls = []
    monkeypatch.setattr(
        transmod,
        'cut_from_audio',
        lambda **kwargs: calls.append(kwargs) or True,
    )
    fake = SimpleNamespace(
        clone_ref=str(source),
        cfg=SimpleNamespace(
            source_wav=str(source),
            cache_folder=str(tmp_path),
            name=str(tmp_path / 'video.mp4'),
        ),
        queue_tts=[{
            'startraw': '00:20:00,000',
            'endraw': '00:20:08,000',
            'start_time': 1_200_000,
            'end_time': 1_208_000,
            'start_time_source': 615_900,
            'end_time_source': 620_800,
            'ref_wav': str(tmp_path / 'clone-smart-200.wav'),
        }],
    )

    TransCreate._create_ref_from_vocal(fake)

    assert len(calls) == 1
    assert calls[0]['ss'] == '00:10:15,900'
    assert calls[0]['to'] == '00:10:20,800'


def test_smart_clone_reference_is_rebuilt_from_output_timeline(tmp_path, monkeypatch):
    import videotrans.task.trans_create as transmod

    source_sub = tmp_path / 'en.srt'
    source_sub.write_text('placeholder', encoding='utf-8')
    source_rows = [
        {'start_time': 0, 'end_time': 4_000, 'text': 'Early source.'},
        {'start_time': 20_000, 'end_time': 25_000, 'text': 'Correct source.'},
    ]
    monkeypatch.setattr(transmod, 'get_subtitle_from_srt', lambda _path: source_rows)
    item = {
        'role': 'clone', 'start_time': 20_200, 'end_time': 24_800,
        'start_time_source': 0, 'end_time_source': 4_000,
        'ref_text': 'Wrong source.', 'ref_wav': str(tmp_path / 'old-ref.wav'),
    }
    signals = []
    fake = SimpleNamespace(
        cfg=SimpleNamespace(smart_orchestration=True, source_sub=str(source_sub),
                            cache_folder=str(tmp_path)),
        queue_tts=[item], signal=lambda **kwargs: signals.append(kwargs),
    )

    repaired = TransCreate._canonicalize_clone_references(fake)

    assert repaired == 1
    assert (item['start_time_source'], item['end_time_source']) == (20_000, 25_000)
    assert item['ref_text'] == 'Correct source.'
    assert '20000-25000.wav' in item['ref_wav']
    assert signals


@pytest.fixture(scope='module')
def qapp():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_default_config_page_hides_engine_settings(qapp):
    from videotrans.flowui.config_page import ConfigPage
    page = ConfigPage(flow=SimpleNamespace())

    assert page.advanced_scroll.isHidden()
    # 智能流程不能沿用经典页上一次“保留背景音”的状态；
    # 分离残留会直接污染下一条视频的中文配音。
    assert not page.keep_bgm.isChecked()
    assert page.keep_bgm.toolTip()
    assert '智能配音' in page.start_btn.text() or 'smart dubbing' in page.start_btn.text()
    page._toggle_advanced()
    assert not page.advanced_scroll.isHidden()
    page.deleteLater()
    qapp.processEvents()


def test_voice_reload_does_not_replace_recent_user_selection(qapp):
    """A slow role lookup must not silently turn dubbed delivery into No-TTS."""
    from videotrans.flowui.config_page import ConfigPage

    page = ConfigPage(flow=SimpleNamespace())
    tts_id = page.tts_card.current_channel_id()
    page.tts_card.set_secondary_items(['No', 'zh-CN-test-voice'], 'No')
    page.tts_card.secondary_box.setCurrentText('zh-CN-test-voice')
    page._voice_request_serial = 5

    # Old callbacks are ignored completely.
    page._apply_voices(tts_id, 4, ['No'])
    assert page.tts_card.current_secondary() == 'zh-CN-test-voice'

    # The current callback refreshes options but keeps the visible choice.
    page._apply_voices(tts_id, 5, ['No', 'zh-CN-test-voice'])
    assert page.tts_card.current_secondary() == 'zh-CN-test-voice'
    page.deleteLater()
    qapp.processEvents()
