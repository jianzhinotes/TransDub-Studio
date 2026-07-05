"""Flow UI 首页：拖放/浏览导入 + 最近任务 + 高级模式入口。"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from videotrans import VERSION
from videotrans.configure import contants
from videotrans.configure.config import params, tr
from videotrans.flowui import recent_tasks

_ALLOWED_EXTS = contants.VIDEO_EXTS + contants.AUDIO_EXITS

_QSS = """
#pageHome QFrame#dropZone {
    border: 2px dashed #455364; border-radius: 12px; background: #1A2530;
}
#pageHome QFrame#dropZone[drag="1"] { border-color: #1A72BB; background: #1E2C3A; }
#pageHome QLabel#heroTitle { font-size: 22px; color: #DFE1E2; }
#pageHome QLabel#heroSub { font-size: 13px; color: #8a9ba8; }
#pageHome QLabel#appTitle { font-size: 16px; color: #DFE1E2; font-weight: bold; }
#pageHome QPushButton#linkBtn {
    border: none; background: transparent; color: #1A72BB; text-align: left;
}
#pageHome QPushButton#linkBtn:hover { text-decoration: underline; }
"""


class DropZone(QFrame):
    dropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('dropZone')
        self.setAcceptDrops(True)
        self.setMinimumSize(640, 220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        title = QLabel(tr('flow_drop_headline'))
        title.setObjectName('heroTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        sub = QLabel(tr('flow_drop_sub'))
        sub.setObjectName('heroSub')
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        layout.addWidget(sub)
        layout.addStretch(1)

    @staticmethod
    def _ok(url) -> bool:
        return url.isLocalFile() and Path(url.toLocalFile()).suffix[1:].lower() in _ALLOWED_EXTS

    def dragEnterEvent(self, event):
        if any(self._ok(u) for u in event.mimeData().urls()):
            event.acceptProposedAction()
            self.setProperty('drag', '1')
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event):
        self.setProperty('drag', '0')
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self.setProperty('drag', '0')
        self.style().unpolish(self)
        self.style().polish(self)
        files = [Path(u.toLocalFile()).as_posix()
                 for u in event.mimeData().urls() if self._ok(u)]
        if files:
            self.dropped.emit(files)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class HomePage(QWidget):
    files_chosen = Signal(list)
    open_advanced = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('pageHome')
        self.setStyleSheet(_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 20, 32, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(f"{tr('softname')}  {VERSION}")
        title.setObjectName('appTitle')
        head.addWidget(title)
        head.addStretch(1)
        adv = QPushButton(tr('flow_advanced_mode'))
        adv.setObjectName('linkBtn')
        adv.setCursor(Qt.CursorShape.PointingHandCursor)
        adv.clicked.connect(self.open_advanced)
        head.addWidget(adv)
        layout.addLayout(head)

        self.drop_zone = DropZone()
        self.drop_zone.dropped.connect(self.files_chosen)
        self.drop_zone.clicked.connect(self._browse)
        layout.addWidget(self.drop_zone, stretch=1)

        recent_head = QLabel(tr('flow_recent_tasks'))
        recent_head.setStyleSheet('color:#DFE1E2;font-size:14px;font-weight:bold;')
        layout.addWidget(recent_head)
        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(200)
        self.recent_list.itemClicked.connect(self._on_recent_clicked)
        layout.addWidget(self.recent_list)

        self.refresh_recent()

    def _browse(self):
        format_str = ' '.join('*.' + e for e in _ALLOWED_EXTS)
        files, _ = QFileDialog.getOpenFileNames(
            self, tr('Select one or more files'),
            params.get('last_opendir', ''), f'Files({format_str})')
        if files:
            files = [Path(f).as_posix() for f in files]
            params['last_opendir'] = Path(files[0]).parent.resolve().as_posix()
            self.files_chosen.emit(files)

    # ---- 最近任务 ----
    _STATUS_TEXT = {
        recent_tasks.STATUS_RUNNING: ('flow_status_running', '#f39c12'),
        recent_tasks.STATUS_SUCCEED: ('flow_status_succeed', '#2ecc71'),
        recent_tasks.STATUS_ERROR: ('flow_status_error', '#ff4d4d'),
        recent_tasks.STATUS_STOPPED: ('flow_status_stopped', '#8a9ba8'),
    }

    def refresh_recent(self):
        self.recent_list.clear()
        entries = recent_tasks.load()
        if not entries:
            item = QListWidgetItem(tr('flow_no_recent'))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent_list.addItem(item)
            return
        for e in entries:
            name = Path(e.get('video_path', '')).name
            key, _color = self._STATUS_TEXT.get(e.get('status'), ('flow_status_running', '#f39c12'))
            when = time.strftime('%m-%d %H:%M', time.localtime(e.get('ts', 0)))
            label = f"{name}   →{e.get('target_language', '')}   {when}   [{tr(key)}]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, e)
            item.setToolTip(e.get('video_path', ''))
            self.recent_list.addItem(item)

    def _on_recent_clicked(self, item):
        e = item.data(Qt.ItemDataRole.UserRole)
        if not e:
            return
        # 成功任务点击打开输出目录；其余情况若源文件仍在则重新发起
        if e.get('status') == recent_tasks.STATUS_SUCCEED and e.get('target_dir') \
                and Path(e['target_dir']).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(e['target_dir']))
        elif e.get('video_path') and Path(e['video_path']).exists():
            self.files_chosen.emit([e['video_path']])
