"""单句重配：ReDubb 线程 + 串行调度队列。

串行原因：多数 TTS 渠道有并发/限流约束，且旧弹窗即为单发模式；
队列去重，完成后实测新音频时长回写 StudioState 并清除待重配标记。
"""
import json
import os
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from pydub import AudioSegment

from videotrans import tts
from videotrans.configure.config import logger


class ReDubb(QThread):
    uito = Signal(str)

    def __init__(self, *, parent=None, idx=0, tts_dict=None, language=None,
                 original_filename=None):
        super().__init__(parent=parent)
        self.tts_dict = tts_dict
        self.language = language
        self.idx = idx
        self.original_filename = str(original_filename or tts_dict.get('filename') or '')
        self.staged_filename = str(tts_dict.get('filename') or '')

    def run(self):
        try:
            tts.run(
                queue_tts=[self.tts_dict],
                language=self.language,
                tts_type=self.tts_dict['tts_type'],
                use_cache=False,
            )
            sidecar = Path(self.staged_filename).parent / 'lang_leak.json'
            failure = ''
            try:
                marks = json.loads(sidecar.read_text(encoding='utf-8'))
                if isinstance(marks, dict):
                    failure = str(marks.get(Path(self.tts_dict['filename']).name) or '')
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            if failure:
                self.uito.emit(f"quality:{self.idx}:{failure[:300]}")
            elif not Path(self.staged_filename).is_file():
                self.uito.emit(f"error:{self.idx}:配音后端没有生成候选音频")
            else:
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
        original = Path(item['filename'])
        suffix = original.suffix or '.wav'
        staged = original.with_name(
            f'.{original.stem}.studio-repair-{time.time_ns()}{suffix}')
        repair_item = dict(item)
        repair_item['filename'] = str(staged)
        repair_item.pop('lang_leak', None)
        repair_item.pop('quality_status', None)
        repair_item.pop('quality_failures', None)
        if int(item.get('tts_type') or 0) == 8 and str(self._language or '').startswith('zh'):
            from videotrans.dub.contextual_repair import contextual_chinese_anchor_bank
            bank = contextual_chinese_anchor_bank(self._state.items, idx)
            if not bank:
                self.finished.emit(
                    idx, False,
                    '没有找到同说话人的已验收中文音色锚点；原音频已保留，请先核验其他片段。')
                self._start_next()
                return
            repair_item['chinese_anchor_bank'] = bank
            repair_item['chinese_anchor_ref'] = bank[0]['wav']
            repair_item['chinese_anchor_text'] = bank[0]['text']

        thread = ReDubb(
            parent=self, idx=idx, tts_dict=repair_item,
            language=self._language, original_filename=str(original))
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
                staged = Path(thread.staged_filename)
                original = Path(thread.original_filename)
                if not staged.is_file():
                    raise OSError('候选音频不存在')
                # Decode before promotion. A truncated candidate must never
                # replace the last playable clip.
                seconds = len(AudioSegment.from_file(staged)) / 1000.0
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, original)
                self._clear_sidecar_marks(original, staged)
            except Exception as error:
                Path(thread.staged_filename).unlink(missing_ok=True)
                self.finished.emit(idx, False, f'安全替换候选音频失败，原音频已保留：{error}')
                self._start_next()
                return
            # 只有强模型复核通过才清除质量标记。
            self._state.clear_quality_failure(idx)
            self._state.set_dubbing_s(idx, seconds)
            self._state.mark_clean(idx)
            self.finished.emit(idx, True, '')
        elif msg.startswith('quality:'):
            _prefix, _line, transcript = msg.split(':', 2)
            item = self._state.items[idx]
            try:
                seconds = len(AudioSegment.from_file(item['filename'])) / 1000.0
            except Exception:
                seconds = 0.0
            self._state.set_dubbing_s(idx, seconds)
            self._state.set_quality_failure(idx, transcript)
            Path(thread.staged_filename).unlink(missing_ok=True)
            self.finished.emit(
                idx, False,
                f"更换中文锚点后强模型复核仍未通过；原音频已保留：{transcript[:240]}")
        else:
            Path(thread.staged_filename).unlink(missing_ok=True)
            self.finished.emit(idx, False, msg)
        self._start_next()

    @staticmethod
    def _clear_sidecar_marks(*paths: Path):
        if not paths:
            return
        sidecar = paths[0].parent / 'lang_leak.json'
        try:
            marks = json.loads(sidecar.read_text(encoding='utf-8'))
            if not isinstance(marks, dict):
                return
            for path in paths:
                marks.pop(path.name, None)
            if marks:
                from videotrans.dub.store import atomic_write_json
                atomic_write_json(sidecar, marks)
            else:
                sidecar.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
