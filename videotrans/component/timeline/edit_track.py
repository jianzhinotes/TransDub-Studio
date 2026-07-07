"""可编辑字幕轨：在 SubtitleTrack 基础上支持拖动块（移动）与拖拽端点（拉伸）。

编辑不直接改数据：拖动期间只在本地预览，释放时发 timesEditRequested，
由 Studio 经 StudioState.set_times 提交后再 set_items 刷新。
"""
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QToolTip

from videotrans.component.timeline.edit_logic import EDGE_PX, clamp_block, ms_to_srt
from videotrans.component.timeline.tracks import SubtitleTrack

_CLICK_SLOP_PX = 4


class EditableSubtitleTrack(SubtitleTrack):
    timesEditRequested = Signal(int, int, int)   # idx, new_start_ms, new_end_ms

    def __init__(self, scale, parent=None):
        super().__init__(scale, parent=parent)
        self.setMouseTracking(True)
        self._drag = None   # {'idx','mode','press_x','orig':(s,e),'cur':(s,e),'moved':bool}

    # ---- 命中检测 ----
    def _hit_test(self, pos):
        """返回 (idx, mode)；mode ∈ 'left'|'right'|'move'|None。
        一列只有一个字幕块，按 x 命中即可（不要求 y 落在块内，便于点选/拖动整块）。"""
        x = pos.x()
        for idx in range(len(self._items)):
            r = self._block_rect(idx)
            if r.right() < x - EDGE_PX:
                continue
            if r.left() > x + EDGE_PX:
                break
            if abs(x - r.left()) <= EDGE_PX:
                return idx, 'left'
            if abs(x - r.right()) <= EDGE_PX:
                return idx, 'right'
            if r.left() <= x <= r.right():
                return idx, 'move'
        return -1, None

    def _block_rect(self, idx) -> QRectF:
        # 被拖动的块显示拖动中的预览位置
        if self._drag and self._drag['idx'] == idx:
            s, e = self._drag['cur']
            x0 = self._scale.ms_to_x(s)
            x1 = self._scale.ms_to_x(e)
            return QRectF(x0, 4.0, max(x1 - x0, 2.0), self.height() - 8.0)
        return super()._block_rect(idx)

    # ---- 鼠标交互 ----
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        idx, mode = self._hit_test(event.position())
        if idx < 0:
            # 空白处沿用基类：seek
            self.seekRequested.emit(self._scale.x_to_ms(event.position().x()))
            return
        it = self._items[idx]
        times = (int(it['start_time']), int(it['end_time']))
        self._drag = {'idx': idx, 'mode': mode, 'press_x': event.position().x(),
                      'orig': times, 'cur': times, 'moved': False}
        self.grabKeyboard()

    def mouseMoveEvent(self, event):
        if not self._drag:
            idx, mode = self._hit_test(event.position())
            if mode in ('left', 'right'):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif mode == 'move':
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.unsetCursor()
            return

        d = self._drag
        dx = event.position().x() - d['press_x']
        if abs(dx) > _CLICK_SLOP_PX:
            d['moved'] = True
        delta_ms = dx * 1000.0 / self._scale.pixels_per_second
        s0, e0 = d['orig']
        if d['mode'] == 'move':
            cand = (s0 + delta_ms, e0 + delta_ms)
        elif d['mode'] == 'left':
            cand = (s0 + delta_ms, e0)
        else:
            cand = (s0, e0 + delta_ms)
        d['cur'] = clamp_block(self._items, d['idx'], cand[0], cand[1],
                               d['mode'], self._scale.duration_ms)
        QToolTip.showText(QCursor.pos(),
                          f"{ms_to_srt(d['cur'][0])} → {ms_to_srt(d['cur'][1])}", self)
        self._invalidate()

    def mouseReleaseEvent(self, event):
        if not self._drag:
            return super().mouseReleaseEvent(event)
        d, self._drag = self._drag, None
        self.releaseKeyboard()
        QToolTip.hideText()
        if d['moved'] and d['cur'] != d['orig']:
            self.timesEditRequested.emit(d['idx'], d['cur'][0], d['cur'][1])
        else:
            # 视为点击：跳转该行
            self.blockClicked.emit(d['idx'])
            self.seekRequested.emit(int(self._items[d['idx']]['start_time']))
        self._invalidate()

    def keyPressEvent(self, event):
        if self._drag and event.key() == Qt.Key.Key_Escape:
            self._drag = None
            self.releaseKeyboard()
            QToolTip.hideText()
            self._invalidate()
            return
        super().keyPressEvent(event)
