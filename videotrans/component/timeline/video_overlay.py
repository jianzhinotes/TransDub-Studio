"""视频播放控件：视频画面 + 下方常驻控制条（播放/进度/时间）。

macOS 的 QVideoWidget 是原生视频层：在其上叠加 Qt 子控件会互相遮挡，甚至
破坏视频渲染（视频出不来）。因此控制条放在视频**下方**而非叠加其上——
这也与剪映预览窗（播放键在画面下方）一致。类名沿用 VideoOverlay 以保持
对 dialog.py / studio.py 的接口兼容。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from videotrans.styles import tokens

_GLYPH_PLAY = '▶'
_GLYPH_PAUSE = '⏸'

_CONTROLS_QSS = f"""
#videoControls {{ background: {tokens.SURFACE}; border-top: 1px solid {tokens.BORDER}; }}
#videoControls QLabel {{ color: {tokens.TEXT_SECONDARY}; font-size: 12px; background: transparent; }}
#videoPlayBtn {{
    background: {tokens.ACCENT}; border: none; border-radius: 20px;
    color: #FFFFFF; font-size: 15px;
}}
#videoPlayBtn:hover {{ background: {tokens.ACCENT_HOVER}; }}
#videoControls QSlider::groove:horizontal {{
    height: 4px; border-radius: 2px; background: {tokens.BORDER};
}}
#videoControls QSlider::sub-page:horizontal {{ background: {tokens.ACCENT}; border-radius: 2px; }}
#videoControls QSlider::handle:horizontal {{
    width: 13px; height: 13px; margin: -5px 0; border-radius: 6px; background: #FFFFFF;
}}
"""


def _fmt(ms: int) -> str:
    s = max(int(ms), 0) // 1000
    return f'{s // 60:02d}:{s % 60:02d}.{max(int(ms), 0) % 1000 // 100}'


class VideoOverlay(QWidget):
    """视频区容器：视频画面在上、控制条在下。"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self._duration_ms = 0
        self._dragging = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(player.video_widget, stretch=1)

        # ---- 下方控制条 ----
        bar = QWidget()
        bar.setObjectName('videoControls')
        bar.setStyleSheet(_CONTROLS_QSS)
        bar.setFixedHeight(52)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(12)

        self.play_btn = QPushButton(_GLYPH_PLAY)
        self.play_btn.setObjectName('videoPlayBtn')
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # 不抢空格键
        self.play_btn.clicked.connect(self.player.toggle)
        h.addWidget(self.play_btn)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(lambda: setattr(self, '_dragging', True))
        self.slider.sliderReleased.connect(self._on_slider_released)
        h.addWidget(self.slider, stretch=1)

        self.time_label = QLabel('00:00.0 / 00:00.0')
        h.addWidget(self.time_label)

        layout.addWidget(bar)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playStateChanged.connect(self._on_play_state)

    # ---- 播放器联动 ----
    def _on_position(self, ms: int):
        if not self._dragging:
            self.slider.blockSignals(True)
            self.slider.setValue(int(ms))
            self.slider.blockSignals(False)
        self.time_label.setText(f'{_fmt(ms)} / {_fmt(self._duration_ms)}')

    def _on_duration(self, ms: int):
        if ms > 0:
            self._duration_ms = int(ms)
            self.slider.setRange(0, int(ms))
            self.time_label.setText(f'{_fmt(self.player.position())} / {_fmt(ms)}')

    def _on_play_state(self, playing: bool):
        self.play_btn.setText(_GLYPH_PAUSE if playing else _GLYPH_PLAY)

    def _on_slider_released(self):
        self._dragging = False
        self.player.seek(self.slider.value())
