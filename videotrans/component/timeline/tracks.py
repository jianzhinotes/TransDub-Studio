"""时间轴轨道控件：标尺 / 波形 / 字幕块。

绘制策略：内容层渲染到 QPixmap 缓存（滚动、缩放、数据变化时才重建），
播放头每帧只在缓存之上叠画一条竖线，播放期间不重算波形。
"""
import bisect

import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QFontMetrics
from PySide6.QtWidgets import QWidget

from videotrans.component.timeline.peaks import PEAKS_PER_SECOND

# 与 styles/style.qss 深色主题一致
COLOR_BG = QColor('#161B22')
COLOR_TEXT = QColor('#E6E9EC')
COLOR_ACCENT = QColor('#2E7CF6')
COLOR_BORDER = QColor('#2E3947')
COLOR_BLOCK = QColor('#2A4A73')
COLOR_PLAYHEAD = QColor('#E6E9EC')


class _BaseTrack(QWidget):
    """共享 TimeScale 的轨道基类：内容 pixmap 缓存 + 播放头叠画 + 点击/拖动 seek。"""
    seekRequested = Signal(int)

    def __init__(self, scale, height=64, parent=None):
        super().__init__(parent)
        self._scale = scale
        self._playhead_ms = 0
        self._pixmap = None
        self._pixmap_key = None
        self._content_rev = 0
        self.setFixedHeight(height)
        self.setMinimumWidth(50)
        scale.changed.connect(self.update)

    def set_playhead(self, ms: int):
        if ms != self._playhead_ms:
            self._playhead_ms = ms
            self.update()

    def _invalidate(self):
        self._content_rev += 1
        self.update()

    # 子类实现：把内容画进缓存层
    def _paint_content(self, painter: QPainter):
        raise NotImplementedError

    def paintEvent(self, event):
        key = (self._scale.scroll_ms, self._scale.pixels_per_second,
               self.width(), self.height(), self._content_rev,
               self.devicePixelRatioF())
        if self._pixmap_key != key:
            dpr = self.devicePixelRatioF()
            pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(COLOR_BG)
            p = QPainter(pm)
            self._paint_content(p)
            p.end()
            self._pixmap = pm
            self._pixmap_key = key

        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        x = self._scale.ms_to_x(self._playhead_ms)
        if 0 <= x <= self.width():
            painter.setPen(QPen(COLOR_PLAYHEAD, 1))
            painter.drawLine(int(x), 0, int(x), self.height())
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self._scale.x_to_ms(event.position().x()))

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self._scale.x_to_ms(event.position().x()))


class RulerTrack(_BaseTrack):
    """自适应刻度标尺：保证相邻主刻度间距不小于 ~70px。"""
    _STEPS_MS = (100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000, 300000, 600000)

    def __init__(self, scale, parent=None):
        super().__init__(scale, height=24, parent=parent)

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000
        if ms % 1000:
            return f'{s // 60:02d}:{s % 60:02d}.{ms % 1000 // 100}'
        return f'{s // 60:02d}:{s % 60:02d}'

    def _paint_content(self, painter: QPainter):
        pps = self._scale.pixels_per_second
        step = next((s for s in self._STEPS_MS if s * pps / 1000.0 >= 70),
                    self._STEPS_MS[-1])
        start_ms, end_ms = self._scale.visible_range(self.width())
        painter.setPen(QPen(COLOR_BORDER, 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        first = (start_ms // step) * step
        ms = first
        fm = QFontMetrics(painter.font())
        while ms <= end_ms + step:
            x = self._scale.ms_to_x(ms)
            painter.setPen(QPen(COLOR_BORDER, 1))
            painter.drawLine(int(x), self.height() - 8, int(x), self.height() - 1)
            # 次刻度(5等分)
            sub = step / 5.0
            for i in range(1, 5):
                sx = self._scale.ms_to_x(ms + sub * i)
                painter.drawLine(int(sx), self.height() - 4, int(sx), self.height() - 1)
            painter.setPen(COLOR_TEXT)
            label = self._fmt(int(ms))
            painter.drawText(int(x) + 3, fm.ascent() + 2, label)
            ms += step


class WaveformTrack(_BaseTrack):
    """波形轨。set_clips 接受 [(offset_ms, peaks[N,2])]，为将来逐段/编辑模式预留；
    v1 始终传单个 clip(offset 0)。"""

    def __init__(self, scale, label='', parent=None):
        super().__init__(scale, height=64, parent=parent)
        self._clips = []
        self._label = label
        self._placeholder = ''

    def set_clips(self, clips):
        self._clips = [(int(off), peaks) for off, peaks in clips if peaks is not None]
        self._placeholder = ''
        self._invalidate()

    def set_placeholder(self, text: str):
        # 波形尚未生成时显示的占位文本
        self._placeholder = text
        self._invalidate()

    def _paint_content(self, painter: QPainter):
        mid = self.height() / 2.0
        painter.setPen(QPen(COLOR_BORDER, 1))
        painter.drawLine(0, int(mid), self.width(), int(mid))

        pps = self._scale.pixels_per_second
        painter.setPen(QPen(COLOR_ACCENT, 1))
        for offset_ms, peaks in self._clips:
            if len(peaks) == 0:
                continue
            # 每个可见像素列 -> 该列覆盖的峰值桶区间，向量化聚合 min/max
            cols = np.arange(self.width() + 1, dtype=np.float64)
            col_ms = self._scale.scroll_ms + cols * 1000.0 / pps - offset_ms
            col_bucket = col_ms * PEAKS_PER_SECOND / 1000.0
            b0 = np.clip(np.floor(col_bucket[:-1]), 0, len(peaks)).astype(np.int64)
            b1 = np.clip(np.ceil(col_bucket[1:]), 0, len(peaks)).astype(np.int64)
            valid = b1 > b0
            if not valid.any():
                continue
            idx = np.where(valid)[0]
            # reduceat 的段为 [bounds[i], bounds[i+1])，相邻列的 b0 天然衔接；
            # 末尾补一个哨兵边界并丢弃其结果，避免最后一列聚合到数组末尾
            bounds = np.append(b0[idx], min(int(b1[idx[-1]]), len(peaks) - 1))
            lo = np.minimum.reduceat(peaks[:, 0], bounds)[:-1]
            hi = np.maximum.reduceat(peaks[:, 1], bounds)[:-1]
            amp = mid - 2
            for i, px in enumerate(idx):
                y0 = mid - float(hi[i]) * amp
                y1 = mid - float(lo[i]) * amp
                painter.drawLine(int(px), int(y0), int(px), int(max(y1, y0 + 1)))

        if self._label or self._placeholder:
            painter.setPen(QColor(COLOR_TEXT.red(), COLOR_TEXT.green(), COLOR_TEXT.blue(), 150))
            text = self._label + ('  ' + self._placeholder if self._placeholder else '')
            painter.drawText(6, 14, text)


class SubtitleTrack(_BaseTrack):
    """字幕块轨：圆角块 + 省略文本；点击块跳转。

    编辑预留：块几何统一走 _block_rect/_index_at，将来加拖拽只需接管鼠标状态。
    """
    blockClicked = Signal(int)

    def __init__(self, scale, parent=None):
        super().__init__(scale, height=40, parent=parent)
        self._items = []
        self._starts = []
        self._active = -1

    def set_items(self, items):
        # 兼容 SrtItem 与 dict（两者都支持 ['key'] 访问）
        self._items = list(items or [])
        self._starts = [int(it['start_time']) for it in self._items]
        self._active = -1
        self._invalidate()

    def set_active(self, idx: int):
        if idx != self._active:
            self._active = idx
            self._invalidate()

    def index_for_ms(self, ms: int) -> int:
        """返回 ms 所处字幕行索引；不在任何行内则返回最近已开始的行，早于首行返回 -1。"""
        i = bisect.bisect_right(self._starts, ms) - 1
        return i

    def _block_rect(self, idx) -> QRectF:
        it = self._items[idx]
        x0 = self._scale.ms_to_x(int(it['start_time']))
        x1 = self._scale.ms_to_x(int(it['end_time']))
        return QRectF(x0, 4.0, max(x1 - x0, 2.0), self.height() - 8.0)

    def _index_at(self, pos) -> int:
        # 一列只有一个字幕块，按 x 命中即可（不要求 y 落在块内，便于点选整块）
        x = pos.x()
        for idx in range(len(self._items)):
            r = self._block_rect(idx)
            if r.right() < 0:
                continue
            if r.left() > self.width():
                break
            if r.left() <= x <= r.right():
                return idx
        return -1

    def _paint_content(self, painter: QPainter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = QFontMetrics(painter.font())
        for idx in range(len(self._items)):
            r = self._block_rect(idx)
            if r.right() < 0:
                continue
            if r.left() > self.width():
                break
            fill = COLOR_ACCENT if idx == self._active else COLOR_BLOCK
            painter.setPen(QPen(COLOR_ACCENT if idx == self._active else COLOR_BORDER, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(r, 3, 3)
            if r.width() > 20:
                painter.setPen(COLOR_TEXT)
                text = str(self._items[idx]['text']).replace('\n', ' ')
                painter.drawText(r.adjusted(4, 0, -4, 0),
                                 Qt.AlignmentFlag.AlignVCenter,
                                 fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                               int(r.width()) - 8))

    def mousePressEvent(self, event):
        idx = self._index_at(event.position())
        if idx >= 0:
            self.blockClicked.emit(idx)
            self.seekRequested.emit(int(self._items[idx]['start_time']))
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 字幕轨拖动不做连续 seek，避免与点击块跳转冲突
        pass
