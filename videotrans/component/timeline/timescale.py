from PySide6.QtCore import QObject, Signal


class TimeScale(QObject):
    """时间轴唯一坐标源：所有轨道共享同一实例，保证像素对齐。

    ms <-> 像素 换算由 pixels_per_second(缩放) 与 scroll_ms(可视窗左缘) 决定。
    """
    changed = Signal()

    MIN_PPS = 0.5
    MAX_PPS = 200.0

    def __init__(self, duration_ms: int = 0, parent=None):
        super().__init__(parent)
        self.duration_ms = max(int(duration_ms), 1)
        self.pixels_per_second = 20.0
        self.scroll_ms = 0

    def ms_to_x(self, ms: float) -> float:
        return (ms - self.scroll_ms) * self.pixels_per_second / 1000.0

    def x_to_ms(self, x: float) -> int:
        ms = self.scroll_ms + x * 1000.0 / self.pixels_per_second
        return int(min(max(ms, 0), self.duration_ms))

    def visible_range(self, width_px: int) -> tuple:
        end = self.scroll_ms + width_px * 1000.0 / self.pixels_per_second
        return self.scroll_ms, int(min(end, self.duration_ms))

    def set_duration(self, duration_ms: int):
        self.duration_ms = max(int(duration_ms), 1)
        self.changed.emit()

    def set_scroll(self, ms: int):
        ms = int(min(max(ms, 0), self.duration_ms))
        if ms != self.scroll_ms:
            self.scroll_ms = ms
            self.changed.emit()

    def set_zoom(self, pps: float, anchor_x: float = None):
        """anchor_x 给定时，保持该像素位置下的时间点在缩放前后不动。"""
        pps = min(max(pps, self.MIN_PPS), self.MAX_PPS)
        if pps == self.pixels_per_second:
            return
        if anchor_x is not None:
            anchor_ms = self.scroll_ms + anchor_x * 1000.0 / self.pixels_per_second
            self.scroll_ms = int(max(0, anchor_ms - anchor_x * 1000.0 / pps))
        self.pixels_per_second = pps
        self.changed.emit()

    def fit(self, width_px: int):
        # 整段时长恰好占满 width_px
        if width_px > 0:
            self.scroll_ms = 0
            self.pixels_per_second = min(
                max(width_px * 1000.0 / self.duration_ms, self.MIN_PPS), self.MAX_PPS)
            self.changed.emit()
