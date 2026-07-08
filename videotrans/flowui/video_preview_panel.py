"""常驻视频预览面板：贯穿配置/处理/完成全程的视频画面 + 多文件切换缩略条。

单个 PreviewPlayer + VideoOverlay（含常驻播放钮）；多文件时下方显示可点缩略条，
点击即预览对应文件（QMediaPlayer 暂停自动显示首帧）。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QListWidget, QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget,
)

from videotrans.component.timeline.player import PreviewPlayer
from videotrans.component.timeline.video_overlay import VideoOverlay
from videotrans.configure.config import tr
from videotrans.styles import tokens

_QSS = f"""
#previewPanel {{ background: {tokens.WINDOW_BG}; }}
#videoCard {{ background: #05070A; border-radius: 12px; }}
#previewFilmstrip {{
    background: transparent;
    border: none;
}}
#previewFilmstrip::item {{
    color: {tokens.TEXT_SECONDARY};
    border: 1px solid {tokens.BORDER};
    border-radius: 8px;
    padding: 5px 12px;
    margin: 2px 3px;
}}
#previewFilmstrip::item:selected {{
    color: #FFFFFF;
    background: {tokens.ACCENT};
    border-color: {tokens.ACCENT};
}}
"""


class VideoPreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('previewPanel')
        self.setStyleSheet(_QSS)
        self.files = []
        self._result_paths = None   # 完成态：[成品, 原片] 切换用

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 12, 20)
        layout.setSpacing(12)

        self.player = PreviewPlayer(self)
        self.video_area = VideoOverlay(self.player)
        # 视频卡片：圆角深底，视频四周留白，避免黑块贴边生硬
        self.video_card = QFrame()
        self.video_card.setObjectName('videoCard')
        self.video_card.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
        card_layout = QVBoxLayout(self.video_card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(self.video_area)
        self.video_area.setMinimumSize(480, 270)
        layout.addWidget(self.video_card, stretch=1)

        # 多文件缩略条：横向可点文件名
        self.filmstrip = QListWidget()
        self.filmstrip.setObjectName('previewFilmstrip')
        self.filmstrip.setFlow(QListWidget.Flow.LeftToRight)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setFixedHeight(40)
        self.filmstrip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.filmstrip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filmstrip.currentRowChanged.connect(self._on_pick)
        self.filmstrip.setVisible(False)
        layout.addWidget(self.filmstrip)

    def load(self, files: list):
        self._result_paths = None
        self.files = list(files or [])
        self.filmstrip.blockSignals(True)
        self.filmstrip.clear()
        for f in self.files:
            self.filmstrip.addItem(QListWidgetItem(Path(f).name))
        if self.files:
            self.filmstrip.setCurrentRow(0)
        self.filmstrip.blockSignals(False)
        self.filmstrip.setVisible(len(self.files) > 1)
        if self.files:
            self.player.load(self.files[0])

    def show_result(self, source: str, output: str = None):
        """完成态：缩略条切换为【成品 / 原片】，默认预览成品视频。"""
        items = []
        if output and Path(output).exists():
            items.append((tr('flow_result_output'), output))
        if source and Path(source).exists():
            items.append((tr('flow_result_source'), source))
        if not items:
            return
        self._result_paths = [p for _, p in items]
        self.filmstrip.blockSignals(True)
        self.filmstrip.clear()
        for label, _ in items:
            self.filmstrip.addItem(QListWidgetItem(label))
        self.filmstrip.setCurrentRow(0)
        self.filmstrip.blockSignals(False)
        self.filmstrip.setVisible(len(items) > 1)
        self.player.load(self._result_paths[0])

    def _on_pick(self, row: int):
        paths = self._result_paths if self._result_paths is not None else self.files
        if 0 <= row < len(paths):
            self.player.load(paths[row])

    def set_video_hidden(self, hidden: bool):
        """隐藏/显示视频画面。编辑对话框（自带视频）弹出时隐藏，避免 macOS
        原生视频层穿透到对话框上层形成叠加。"""
        if hidden and not self.video_card.isHidden():
            self.player.pause()
            self.video_card.setVisible(False)
        elif not hidden and self.video_card.isHidden():
            self.video_card.setVisible(True)

    def stop(self):
        self.player.stop()

    def release_video(self):
        """进入内嵌编辑工作台前解绑视频输出并隐藏——macOS 上两个 QVideoWidget
        原生视频层同时活着会段错误，故编辑期间只保留工作台那一个。"""
        try:
            self.player.stop()
            self.player.video_player.setVideoOutput(None)
        except Exception:
            pass
        self.video_card.setVisible(False)

    def resume_video(self):
        try:
            self.player.video_player.setVideoOutput(self.player.video_widget)
        except Exception:
            pass
        self.video_card.setVisible(True)
