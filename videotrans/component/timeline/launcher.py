"""时间轴预览独立入口（工具菜单）：选视频 + SRT + 可选配音音频后打开预览。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGridLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton,
)

from videotrans.configure.config import ROOT_DIR, TEMP_ROOT, params, tr
from videotrans.util import tools

_VIDEO_FILTER = "Video/Audio (*.mp4 *.mov *.mkv *.avi *.mts *.webm *.wav *.mp3 *.m4a *.flac *.aac)"
_SRT_FILTER = "Subtitles (*.srt)"
_AUDIO_FILTER = "Audio (*.wav *.mp3 *.m4a *.flac *.aac)"


class TimelineLauncherWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Timeline Preview"))
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        self.setMinimumWidth(560)
        self._preview = None

        layout = QGridLayout(self)
        self.video_edit = self._row(layout, 0, tr("Select video file"), _VIDEO_FILTER)
        self.srt_edit = self._row(layout, 1, tr("Select subtitle file"), _SRT_FILTER)
        self.dub_edit = self._row(layout, 2, tr("Select dubbed audio (optional)"), _AUDIO_FILTER)

        open_btn = QPushButton(tr("Timeline Preview"))
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setMinimumHeight(35)
        open_btn.clicked.connect(self._open_preview)
        layout.addWidget(open_btn, 3, 0, 1, 3)

    def _row(self, layout, row, label, name_filter):
        layout.addWidget(QLabel(label), row, 0)
        edit = QLineEdit()
        edit.setReadOnly(True)
        layout.addWidget(edit, row, 1)
        btn = QPushButton('...')
        btn.setFixedWidth(40)
        btn.clicked.connect(lambda: self._pick(edit, label, name_filter))
        layout.addWidget(btn, row, 2)
        return edit

    def _pick(self, edit, title, name_filter):
        last_dir = params.get('last_opendir') or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, title, last_dir, name_filter)
        if path:
            edit.setText(path)

    def _open_preview(self):
        video = self.video_edit.text().strip()
        srt = self.srt_edit.text().strip()
        dub = self.dub_edit.text().strip() or None
        if not video or not Path(video).exists() or not srt or not Path(srt).exists():
            QMessageBox.warning(self, tr("Timeline Preview"),
                                tr("Select video file") + ' / ' + tr("Select subtitle file"))
            return
        try:
            items = tools.get_subtitle_from_srt(srt)
        except Exception as e:
            QMessageBox.warning(self, tr("Timeline Preview"), f'{tr("anerror")}: {e}')
            return

        from videotrans.component.timeline.dialog import TimelinePreviewDialog
        cache_dir = Path(TEMP_ROOT) / 'timeline_cache'
        self._preview = TimelinePreviewDialog(
            video_path=video,
            subtitle_items=items,
            dubbed_audio=dub,
            cache_folder=str(cache_dir),
            parent=self,
        )
        self._preview.show()
