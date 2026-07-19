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
            cache_folder=str(tmp_path / 'new-cache'), clear_cache=False),
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
    assert '智能配音' in page.start_btn.text() or 'smart dubbing' in page.start_btn.text()
    page._toggle_advanced()
    assert not page.advanced_scroll.isHidden()
    page.deleteLater()
    qapp.processEvents()
