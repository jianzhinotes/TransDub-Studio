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

    def _on_pick(self, row: int):
        if 0 <= row < len(self.files):
            self.player.load(self.files[row])

    def load_output(self, path: str):
        """完成态可加载成品视频。"""
        if path and Path(path).exists():
            self.player.load(path)

    def stop(self):
        self.player.stop()
