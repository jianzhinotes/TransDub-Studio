import time
from pathlib import Path

import pytest

from videotrans.configure.config import _prune_old_logs


class TestNotImplementedError:
    """基类未实现方法应抛出 NotImplementedError（而不是 TypeError）"""

    def test_tts_base_run(self):
        from videotrans.tts._base import BaseTTS
        btts = BaseTTS(queue_tts=[{"text": "hello"}])
        with pytest.raises(NotImplementedError):
            btts._run(None)

    def test_translator_base_item_task(self):
        from videotrans.translator._base import BaseTrans
        with pytest.raises(NotImplementedError):
            BaseTrans.__new__(BaseTrans)._item_task([])

    def test_recognition_base_exec(self):
        from videotrans.recognition._base import BaseRecogn
        with pytest.raises(NotImplementedError):
            BaseRecogn.__new__(BaseRecogn)._exec()


class TestPruneOldLogs:
    def test_removes_old_keeps_recent(self, tmp_path):
        old = tmp_path / "20240101.log"
        recent = tmp_path / "recent.log"
        other = tmp_path / "keep.txt"
        for f in (old, recent, other):
            f.write_text("x")
        import os
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))

        _prune_old_logs(str(tmp_path), days=30)

        assert not old.exists()
        assert recent.exists()
        assert other.exists()  # 只清理 *.log*

    def test_missing_dir_is_noop(self, tmp_path):
        _prune_old_logs(str(tmp_path / "nonexistent"), days=30)
