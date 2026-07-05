"""VideoOverlay 离屏测试：手动驱动播放器信号，断言标签/滑杆/图标状态。"""
import importlib.util
import os

import pytest

if importlib.util.find_spec('PySide6') is None:
    pytest.skip('PySide6 not installed', allow_module_level=True)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakePlayer(QObject):
    """最小可用的 PreviewPlayer 替身。"""
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playStateChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.video_widget = QWidget()
        self._playing = False
        self.seeked_to = None

    def is_playing(self):
        return self._playing

    def toggle(self):
        self._playing = not self._playing
        self.playStateChanged.emit(self._playing)

    def position(self):
        return 0

    def seek(self, ms):
        self.seeked_to = ms


class TestVideoOverlay:
    def test_signals_update_ui(self, qapp):
        from videotrans.component.timeline.video_overlay import VideoOverlay, _GLYPH_PAUSE, _GLYPH_PLAY
        player = _FakePlayer()
        ov = VideoOverlay(player)

        player.durationChanged.emit(90000)
        assert ov.slider.maximum() == 90000
        assert '01:30.0' in ov.time_label.text()

        player.positionChanged.emit(45500)
        assert ov.slider.value() == 45500
        assert ov.time_label.text().startswith('00:45.5')

        player.playStateChanged.emit(True)
        assert ov.play_btn.text() == _GLYPH_PAUSE
        player.playStateChanged.emit(False)
        assert ov.play_btn.text() == _GLYPH_PLAY

    def test_play_button_click_toggles(self, qapp):
        from videotrans.component.timeline.video_overlay import VideoOverlay
        player = _FakePlayer()
        ov = VideoOverlay(player)
        ov.play_btn.click()
        assert player.is_playing() is True

    def test_seek(self, qapp):
        from videotrans.component.timeline.video_overlay import VideoOverlay
        player = _FakePlayer()
        ov = VideoOverlay(player)
        player.durationChanged.emit(10000)
        ov.slider.setValue(7000)
        ov._on_slider_released()
        assert player.seeked_to == 7000
        assert ov._dragging is False

    def test_drag_blocks_position_updates(self, qapp):
        from videotrans.component.timeline.video_overlay import VideoOverlay
        player = _FakePlayer()
        ov = VideoOverlay(player)
        player.durationChanged.emit(10000)

        ov._dragging = True
        ov.slider.setValue(3000)
        player.positionChanged.emit(9000)
        assert ov.slider.value() == 3000   # 拖动中不被播放位置顶掉
