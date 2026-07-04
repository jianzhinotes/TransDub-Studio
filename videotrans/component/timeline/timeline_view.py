"""TimelineView：标尺 + 原声/配音波形 + 字幕块 + 水平滚动条 的组装容器。

所有轨道共享一个 TimeScale，像素级对齐；Ctrl+滚轮缩放（锚定光标），
滚轮/滚动条平移，播放头越界时自动滚动跟随。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QScrollBar, QVBoxLayout, QWidget

from videotrans.component.timeline.timescale import TimeScale
from videotrans.component.timeline.tracks import RulerTrack, SubtitleTrack, WaveformTrack

_ZOOM_STEP = 1.25


class TimelineView(QWidget):
    seekRequested = Signal(int)
    blockClicked = Signal(int)

    def __init__(self, duration_ms: int, parent=None, subtitle_track_cls=SubtitleTrack):
        super().__init__(parent)
        self.scale = TimeScale(duration_ms, parent=self)
        self._playhead_ms = 0
        self._tracks = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        self.ruler = RulerTrack(self.scale)
        self._add_track(self.ruler)

        self.subtitle_track = subtitle_track_cls(self.scale)
        self.subtitle_track.blockClicked.connect(self.blockClicked)

        self._scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self._scrollbar.valueChanged.connect(self._on_scrollbar)
        self._layout.addWidget(self._scrollbar)

        self.scale.changed.connect(self._sync_scrollbar)
        self._sync_scrollbar()

    # ---- 轨道组装 ----
    def _add_track(self, track):
        track.seekRequested.connect(self.seekRequested)
        # 滚动条永远在最下方
        self._layout.insertWidget(max(self._layout.count() - 1, 0), track)
        self._tracks.append(track)

    def add_waveform_track(self, label: str) -> WaveformTrack:
        track = WaveformTrack(self.scale, label=label)
        self._add_track(track)
        return track

    def set_subtitles(self, items):
        if self.subtitle_track not in self._tracks:
            self._add_track(self.subtitle_track)
        self.subtitle_track.set_items(items)

    # ---- 播放头 ----
    def set_position(self, ms: int):
        self._playhead_ms = ms
        for t in self._tracks:
            t.set_playhead(ms)
        # 播放头驶出可视区时翻页跟随
        start_ms, end_ms = self.scale.visible_range(self._viewport_width())
        if ms > end_ms or ms < start_ms:
            self.scale.set_scroll(ms)

    # ---- 缩放 ----
    def zoom_in(self, anchor_x=None):
        self.scale.set_zoom(self.scale.pixels_per_second * _ZOOM_STEP, anchor_x)

    def zoom_out(self, anchor_x=None):
        self.scale.set_zoom(self.scale.pixels_per_second / _ZOOM_STEP, anchor_x)

    def zoom_fit(self):
        self.scale.fit(self._viewport_width())

    def _viewport_width(self) -> int:
        # 布局未完成时 ruler.width() 是默认值，退回容器自身宽度
        return max(self.ruler.width(), self.width(), 50)

    # ---- 滚动 ----
    def _visible_span_ms(self) -> int:
        return int(self._viewport_width() * 1000.0 / self.scale.pixels_per_second)

    def _sync_scrollbar(self):
        span = self._visible_span_ms()
        maximum = max(self.scale.duration_ms - span, 0)
        self._scrollbar.blockSignals(True)
        self._scrollbar.setRange(0, maximum)
        self._scrollbar.setPageStep(span)
        self._scrollbar.setValue(min(self.scale.scroll_ms, maximum))
        self._scrollbar.blockSignals(False)

    def _on_scrollbar(self, value):
        self.scale.set_scroll(value)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            anchor_x = event.position().x()
            if delta > 0:
                self.zoom_in(anchor_x)
            elif delta < 0:
                self.zoom_out(anchor_x)
        else:
            step = int(self._visible_span_ms() * 0.1)
            self.scale.set_scroll(self.scale.scroll_ms + (-step if delta > 0 else step))
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_scrollbar()


if __name__ == '__main__':  # 开发预览: uv run python -m videotrans.component.timeline.timeline_view a.wav b.srt
    import re
    import sys

    from PySide6.QtWidgets import QApplication

    from videotrans.component.timeline.peaks import extract_peaks

    def _parse_srt(path):
        text = open(path, encoding='utf-8').read()
        items = []
        pat = re.compile(
            r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)\s*\n(.*?)(?:\n\n|\Z)',
            re.S)
        for m in pat.finditer(text):
            g = [int(x) for x in m.groups()[:8]]
            items.append({
                'start_time': g[0] * 3600000 + g[1] * 60000 + g[2] * 1000 + g[3],
                'end_time': g[4] * 3600000 + g[5] * 60000 + g[6] * 1000 + g[7],
                'text': m.group(9).strip(),
            })
        return items

    app = QApplication(sys.argv)
    peaks, duration_ms = extract_peaks(sys.argv[1], '/tmp/timeline_dev')
    view = TimelineView(duration_ms)
    wave = view.add_waveform_track('原声')
    wave.set_clips([(0, peaks)])
    if len(sys.argv) > 2:
        view.set_subtitles(_parse_srt(sys.argv[2]))
    view.seekRequested.connect(lambda ms: (view.set_position(ms), print('seek', ms)))
    view.resize(1000, 220)
    view.show()
    sys.exit(app.exec())
