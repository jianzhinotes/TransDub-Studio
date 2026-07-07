import importlib.util
import pytest

_HAS_QT = importlib.util.find_spec('PySide6') is not None
pytestmark = pytest.mark.skipif(not _HAS_QT, reason='needs real PySide6')

_SRT = ("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nworld\n\n")
_TGT = ("1\n00:00:00,000 --> 00:00:01,000\n你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n世界\n\n")


@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    return app


def test_source_mode_saves_edits(qapp, tmp_path):
    from videotrans.flowui.inline_subtitle_editor import InlineSubtitleEditor, MODE_SOURCE
    p = tmp_path / 'src.srt'
    p.write_text(_SRT, encoding='utf-8')
    ed = InlineSubtitleEditor(mode=MODE_SOURCE, sub_path=str(p))
    assert ed.table.rowCount() == 2
    ed.table.item(0, 2).setText('HELLO EDITED')
    ed._collect_and_save()
    saved = p.read_text(encoding='utf-8')
    assert 'HELLO EDITED' in saved
    assert '00:00:00,000 --> 00:00:01,000' in saved   # 时间保持


def test_target_mode_shows_source_and_saves(qapp, tmp_path):
    from videotrans.flowui.inline_subtitle_editor import InlineSubtitleEditor, MODE_TARGET
    src = tmp_path / 'src.srt'; src.write_text(_SRT, encoding='utf-8')
    tgt = tmp_path / 'tgt.srt'; tgt.write_text(_TGT, encoding='utf-8')
    ed = InlineSubtitleEditor(mode=MODE_TARGET, sub_path=str(tgt), source_sub=str(src))
    assert ed.table.columnCount() == 4
    assert ed.table.item(0, 1).text() == 'hello'   # 原文并排
    assert ed.table.item(0, 2).text() == '你好'
    ed.table.item(0, 2).setText('你好啊')
    ed._collect_and_save()
    assert '你好啊' in tgt.read_text(encoding='utf-8')


def test_proof_signals(qapp, tmp_path):
    from videotrans.flowui.inline_subtitle_editor import InlineSubtitleEditor, MODE_SOURCE
    p = tmp_path / 's.srt'; p.write_text(_SRT, encoding='utf-8')
    ed = InlineSubtitleEditor(mode=MODE_SOURCE, sub_path=str(p))
    fired = {}
    ed.proofDone.connect(lambda: fired.setdefault('done', True))
    ed.proofTerminate.connect(lambda: fired.setdefault('term', True))
    ed._on_next()
    ed._on_terminate()
    assert fired == {'done': True, 'term': True}
