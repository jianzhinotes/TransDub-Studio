"""视频悬浮控件：点视频播放/暂停 + 常驻中央播放钮 + 悬停底部控制条。

叠放方案：覆盖层作为 video_widget 的**子控件**并 raise_()。
macOS 上 QVideoWidget 是原生视频层，会盖住兄弟控件，因此覆盖层必须是它的
子控件才能渲染在视频画面之上；geometry 通过事件过滤器随视频控件同步。
"""
from PySide6.QtCore import (
    QEvent, QPropertyAnimation, Qt, QTimer,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

_BAR_BG = 'rgba(20, 25, 35, 0.78)'
_ACCENT = '#2E7CF6'

_GLYPH_PLAY = '▶'
_GLYPH_PAUSE = '⏸'

_AUTOHIDE_MS = 1500


def _fmt(ms: int) -> str:
    s = max(int(ms), 0) // 1000
    return f'{s // 60:02d}:{s % 60:02d}.{max(int(ms), 0) % 1000 // 100}'


class _OverlayLayer(QWidget):
    """透明交互层：承载中央图标与底部控制条，点击空白处切换播放。"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        # ---- 中央常驻播放按钮（暂停时可见，明确提示可点击播放） ----
        self.center_btn = QPushButton(_GLYPH_PLAY, self)
        self.center_btn.setFixedSize(72, 72)
        self.center_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.center_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.center_btn.setStyleSheet(
            'QPushButton { background:rgba(20,25,35,0.72); border:none;'
            '  border-radius:36px; color:#FFFFFF; font-size:30px; }'
            'QPushButton:hover { background:rgba(30,38,52,0.85); }')
        self.center_btn.clicked.connect(self._toggle)

        # ---- 底部控制条 ----
        self.bar = QWidget(self)
        self.bar.setObjectName('overlayBar')
        self.bar.setFixedHeight(44)
        self.bar.setStyleSheet(
            f'#overlayBar {{ background:{_BAR_BG}; border-radius:10px; }}'
            '#overlayBar QLabel { color:#FFFFFF; font-size:12px; background:transparent; }'
            '#overlayBar QPushButton { color:#FFFFFF; font-size:15px; border:none;'
            '  background:transparent; }'
            '#overlayBar QPushButton:hover { color:#9CC4FF; }'
            '#overlayBar QSlider::groove:horizontal { height:4px; border-radius:2px;'
            '  background:rgba(255,255,255,0.25); }'
            f'#overlayBar QSlider::sub-page:horizontal {{ background:{_ACCENT};'
            '  border-radius:2px; }}'
            '#overlayBar QSlider::handle:horizontal { width:12px; height:12px;'
            '  margin:-4px 0; border-radius:6px; background:#FFFFFF; }')
        bar_layout = QHBoxLayout(self.bar)
        bar_layout.setContentsMargins(12, 4, 12, 4)
        bar_layout.setSpacing(10)

        self.play_btn = QPushButton(_GLYPH_PLAY)
        self.play_btn.setFixedSize(28, 28)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # 不抢焦点：空格键继续由外层对话框的 keyPressEvent 处理
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.clicked.connect(self._toggle)
        bar_layout.addWidget(self.play_btn)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.setRange(0, 0)
        self._dragging = False
        self.slider.sliderPressed.connect(lambda: setattr(self, '_dragging', True))
        self.slider.sliderReleased.connect(self._on_slider_released)
        bar_layout.addWidget(self.slider, stretch=1)

        self.time_label = QLabel('00:00.0 / 00:00.0')
        bar_layout.addWidget(self.time_label)

        self._bar_fx = QGraphicsOpacityEffect(self.bar)
        self._bar_fx.setOpacity(1.0)
        self.bar.setGraphicsEffect(self._bar_fx)
        self._bar_anim = QPropertyAnimation(self._bar_fx, b'opacity')
        self._bar_anim.setDuration(200)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_AUTOHIDE_MS)
        self._hide_timer.timeout.connect(self._maybe_hide_bar)

        # ---- 播放器联动 ----
        self._duration_ms = 0
        player.positionChanged.connect(self._on_position)
        player.durationChanged.connect(self._on_duration)
        player.playStateChanged.connect(self._on_play_state)

    # ---- 布局 ----
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.center_btn.move((self.width() - 72) // 2, (self.height() - 72) // 2)
        self.bar.setGeometry(12, self.height() - 44 - 12, max(self.width() - 24, 50), 44)

    # ---- 交互 ----
    def _toggle(self):
        self.player.toggle()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton \
                and not self.bar.geometry().contains(event.position().toPoint()) \
                and not self.center_btn.geometry().contains(event.position().toPoint()):
            self._toggle()

    def mouseMoveEvent(self, event):
        self._show_bar()
        super().mouseMoveEvent(event)

    def enterEvent(self, event):
        self._show_bar()
        super().enterEvent(event)

    def _show_bar(self):
        self._bar_anim.stop()
        self._bar_anim.setStartValue(self._bar_fx.opacity())
        self._bar_anim.setEndValue(1.0)
        self._bar_anim.start()
        self._hide_timer.start()

    def _maybe_hide_bar(self):
        # 暂停时常显，播放中才自动隐藏
        if self.player.is_playing() and not self._dragging:
            self._bar_anim.stop()
            self._bar_anim.setStartValue(self._bar_fx.opacity())
            self._bar_anim.setEndValue(0.0)
            self._bar_anim.start()

    # ---- 播放器状态 ----
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
        # 暂停时露出中央大播放钮；播放时隐藏，避免遮挡画面
        self.center_btn.setVisible(not playing)
        if not playing:
            self._show_bar()

    def _on_slider_released(self):
        self._dragging = False
        self.player.seek(self.slider.value())


class VideoOverlay(QWidget):
    """视频区容器：video_widget 铺满，控制层作为其子控件浮在视频之上。"""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(player.video_widget)
        # 覆盖层 parent 到 video_widget，确保在 macOS 原生视频层之上渲染
        self.overlay = _OverlayLayer(player, parent=player.video_widget)
        self.overlay.raise_()
        player.video_widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.player.video_widget and event.type() in (
                QEvent.Type.Resize, QEvent.Type.Show):
            self.overlay.setGeometry(self.player.video_widget.rect())
            self.overlay.raise_()
        return super().eventFilter(obj, event)
