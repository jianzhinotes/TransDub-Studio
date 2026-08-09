"""字幕校对的**往返契约**：编辑器写盘 → worker 据此决策。

这块连续修了 5 次（allow editing bilingual / allow source edits before
translation / persist source edits / preserve unmatched rows / preserve
translation edits）。此前两侧各有单测，却没有一个测试覆盖它们之间的契约，
于是每次修好一侧都可能打破另一侧。

契约（worker 侧决策见 only_one.py 的两处 _subtitle_signature 比较）：
  C1 只改译文 → 源文签名不变 → **不得重译**（重译会丢弃用户刚写的译文）
  C2 改了原文 → 源文签名改变 → 必须重译（否则渲染出与原文对不上的旧译文）
  C3 只改排版/空白 → 签名不变 → 不得重译
  C4 未匹配上的原文行必须原样保留，不能在回写时丢行
  C5 非 subtitle_only（配音任务）时原文列只读，签名恒定不变
"""
import importlib.util

import pytest

_HAS_QT = importlib.util.find_spec('PySide6') is not None
pytestmark = pytest.mark.skipif(not _HAS_QT, reason='needs real PySide6')

_SRC = ("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nworld\n\n")
_TGT = ("1\n00:00:00,000 --> 00:00:01,000\n你好\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n世界\n\n")


@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def subs(tmp_path):
    src = tmp_path / 'en.srt'
    tgt = tmp_path / 'zh.srt'
    src.write_text(_SRC, encoding='utf-8')
    tgt.write_text(_TGT, encoding='utf-8')
    return src, tgt


def _editor(src, tgt, subtitle_only=True):
    from videotrans.flowui.inline_subtitle_editor import InlineSubtitleEditor, MODE_TARGET
    return InlineSubtitleEditor(mode=MODE_TARGET, sub_path=str(tgt),
                                source_sub=str(src), subtitle_only=subtitle_only)


def _signature(path):
    from videotrans.task.only_one import _subtitle_signature
    return _subtitle_signature(str(path))


def _rows(path):
    from videotrans.util.tools import get_subtitle_from_srt
    return get_subtitle_from_srt(str(path), is_file=True)


class TestTranslationOnlyEdit:
    """C1：只改译文不得触发重译，否则用户刚写的译文会被覆盖。"""

    def test_source_signature_unchanged(self, qapp, subs):
        src, tgt = subs
        before = _signature(src)
        ed = _editor(src, tgt)
        ed.table.item(0, 2).setText('你好啊，世界')
        ed._collect_and_save()
        assert _signature(src) == before, '译文编辑不该改变原文签名（会误触发重译）'

    def test_translation_actually_persisted(self, qapp, subs):
        src, tgt = subs
        ed = _editor(src, tgt)
        ed.table.item(0, 2).setText('你好啊，世界')
        ed._collect_and_save()
        assert '你好啊，世界' in tgt.read_text(encoding='utf-8')


class TestSourceEdit:
    """C2：改了原文必须重译，否则会渲染出与原文对不上的旧译文。"""

    def test_source_signature_changes(self, qapp, subs):
        src, tgt = subs
        before = _signature(src)
        ed = _editor(src, tgt)
        ed.table.item(0, 1).setText('hello corrected')
        ed._collect_and_save()
        assert _signature(src) != before, '原文编辑必须改变签名以触发重译'

    def test_both_columns_persisted(self, qapp, subs):
        src, tgt = subs
        ed = _editor(src, tgt)
        ed.table.item(0, 1).setText('hello corrected')
        ed.table.item(0, 2).setText('你好，修正')
        ed._collect_and_save()
        assert 'hello corrected' in src.read_text(encoding='utf-8')
        assert '你好，修正' in tgt.read_text(encoding='utf-8')


class TestNoopEdit:
    """C3：什么都不改（或只动排版）不得触发重译。"""

    def test_untouched_editor_keeps_signature(self, qapp, subs):
        src, tgt = subs
        before = _signature(src)
        ed = _editor(src, tgt)
        ed._collect_and_save()
        assert _signature(src) == before

    def test_whitespace_only_edit_keeps_signature(self, qapp, subs):
        src, tgt = subs
        before = _signature(src)
        ed = _editor(src, tgt)
        ed.table.item(0, 1).setText('  hello  ')     # 仅前后空白
        ed._collect_and_save()
        assert _signature(src) == before


class TestRowPreservation:
    """C4：回写不得丢行——原文比译文多/行号对不上时尤其危险。"""

    def test_unmatched_source_rows_survive(self, qapp, tmp_path):
        src = tmp_path / 'en.srt'
        tgt = tmp_path / 'zh.srt'
        # 原文 3 行，译文只有 2 行（合并/丢句后的常见状态）
        src.write_text(_SRC + "3\n00:00:04,000 --> 00:00:05,000\nthird line\n\n",
                       encoding='utf-8')
        tgt.write_text(_TGT, encoding='utf-8')
        ed = _editor(src, tgt)
        # 必须真的改动原文列：否则不触发原文回写，这条测试会因为"根本没写盘"
        # 而假性通过（变异测试证实过）
        ed.table.item(0, 1).setText('hello edited')
        ed._collect_and_save()
        texts = [r['text'].strip() for r in _rows(src)]
        assert 'hello edited' in texts, '原文编辑未落盘'
        assert 'third line' in texts, '未匹配的原文行在回写时被丢掉了'
        assert len(texts) == 3

    def test_row_count_stable_after_save(self, qapp, subs):
        src, tgt = subs
        ed = _editor(src, tgt)
        ed._collect_and_save()
        assert len(_rows(src)) == 2
        assert len(_rows(tgt)) == 2


class TestDubbingJobContract:
    """C5：配音任务的原文列只读，签名必须恒定。"""

    def test_source_column_is_read_only(self, qapp, subs):
        from PySide6.QtCore import Qt
        src, tgt = subs
        ed = _editor(src, tgt, subtitle_only=False)
        flags = ed.table.item(0, 1).flags()
        assert not (flags & Qt.ItemFlag.ItemIsEditable)

    def test_save_never_touches_source(self, qapp, subs):
        src, tgt = subs
        before = _signature(src)
        ed = _editor(src, tgt, subtitle_only=False)
        ed.table.item(0, 2).setText('配音译文修改')
        ed._collect_and_save()
        assert _signature(src) == before
        assert '配音译文修改' in tgt.read_text(encoding='utf-8')


class TestWorkerLoopDecision:
    """把 worker 的循环判据直接跑一遍：签名相等即退出，不相等则重译再开。"""

    def _loop_would_retranslate(self, src_path, mutate):
        before = _signature(src_path)
        mutate()
        return _signature(src_path) != before

    def test_translation_edit_exits_loop(self, qapp, subs):
        src, tgt = subs

        def only_translation():
            ed = _editor(src, tgt)
            ed.table.item(0, 2).setText('改译文')
            ed._collect_and_save()

        assert self._loop_would_retranslate(src, only_translation) is False

    def test_source_edit_reenters_loop(self, qapp, subs):
        src, tgt = subs

        def edit_source():
            ed = _editor(src, tgt)
            ed.table.item(0, 1).setText('brand new source')
            ed._collect_and_save()

        assert self._loop_would_retranslate(src, edit_source) is True
