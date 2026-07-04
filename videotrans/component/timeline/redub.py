"""单句重配：ReDubb 线程 + 串行调度队列。

串行原因：多数 TTS 渠道有并发/限流约束，且旧弹窗即为单发模式；
队列去重，完成后实测新音频时长回写 StudioState 并清除待重配标记。
"""
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from pydub import AudioSegment

from videotrans import tts
from videotrans.configure.config import logger


class ReDubb(QThread):
    uito = Signal(str)

    def __init__(self, *, parent=None, idx=0, tts_dict=None, language=None):
        super().__init__(parent=parent)
        self.tts_dict = tts_dict
        self.language = language
        self.idx = idx

    def run(self):
        try:
            tts.run(
                queue_tts=[self.tts_dict],
                language=self.language,
                tts_type=self.tts_dict['tts_type']
            )
            self.uito.emit(f"ok:{self.idx}")
        except Exception as e:
            from videotrans.configure.excepts import get_msg_from_except
            except_msg = get_msg_from_except(e)
            msg = f'{except_msg}:\n' + traceback.format_exc()
            self.uito.emit(msg)


class RedubQueue(QObject):
    started = Signal(int)                 # idx 开始重配
    finished = Signal(int, bool, str)     # idx, ok, error_msg

    def __init__(self, state, language: str, parent=None):
        super().__init__(parent)
        self._state = state
        self._language = language
        self._pending = []
        self._current = None   # (idx, ReDubb)

    def enqueue(self, idx: int):
        if idx in self._pending or (self._current and self._current[0] == idx):
            return
        self._pending.append(idx)
        self._start_next()

    def pending(self) -> list:
        result = list(self._pending)
        if self._current:
            result.insert(0, self._current[0])
        return result

    def is_queued(self, idx: int) -> bool:
        return idx in self._pending

    def _start_next(self):
        if self._current or not self._pending:
            return
        idx = self._pending.pop(0)
        item = self._state.items[idx]
        # 删除旧音频并复位时长，与旧弹窗行为一致
        try:
            Path(item['filename']).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f'删除旧配音文件失败: {e}')
        self._state.set_dubbing_s(idx, 0.0)

        thread = ReDubb(parent=self, idx=idx, tts_dict=dict(item),
                        language=self._language)
        thread.uito.connect(self._on_done)
        self._current = (idx, thread)
        self.started.emit(idx)
        thread.start()

    def _on_done(self, msg: str):
        idx, thread = self._current
        self._current = None
        thread.deleteLater()

        if msg.startswith('ok:'):
            item = self._state.items[idx]
            try:
                if Path(item['filename']).exists():
                    seconds = len(AudioSegment.from_file(item['filename'])) / 1000.0
                else:
                    seconds = 0.0
            except Exception:
                seconds = 0.0
            self._state.set_dubbing_s(idx, seconds)
            self._state.mark_clean(idx)
            self.finished.emit(idx, True, '')
        else:
            self.finished.emit(idx, False, msg)
        self._start_next()
