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
    border: 2px dashed #2E3947; border-radius: 14px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #1C232D, stop:0.5 #1C2A3A, stop:1 #201F38);
}
#pageHome QFrame#dropZone[drag="1"] { border-color: #2E7CF6; background: #1E2C3A; }
#pageHome QLabel#heroTitle { font-size: 24px; color: #E6E9EC; font-weight: bold; }
#pageHome QLabel#heroSub { font-size: 13px; color: #9AA7B4; }
#pageHome QLabel#heroStar { color: #6C7FD8; font-size: 15px; }
#pageHome QLabel#appTitle { font-size: 16px; color: #E6E9EC; font-weight: bold; }
#pageHome QPushButton#linkBtn {
    border: none; background: transparent; color: #2E7CF6; text-align: left;
}
#pageHome QPushButton#linkBtn:hover { text-decoration: underline; }
#pageHome QLabel#authorBar { color: #60798B; font-size: 12px; }
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
        # 四角点缀星光
        corner_top = QHBoxLayout()
        for text, align in (('✦', Qt.AlignmentFlag.AlignLeft), ('✧', Qt.AlignmentFlag.AlignRight)):
            star = QLabel(text)
            star.setObjectName('heroStar')
            star.setAlignment(align)
            corner_top.addWidget(star)
        layout.addLayout(corner_top)
        layout.addStretch(1)
        title = QLabel('✨ ' + tr('flow_drop_headline'))
        title.setObjectName('heroTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        sub = QLabel(tr('flow_drop_sub'))
        sub.setObjectName('heroSub')
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        layout.addWidget(sub)
        layout.addStretch(1)
        corner_bottom = QHBoxLayout()
        for text, align in (('✧', Qt.AlignmentFlag.AlignLeft), ('✦', Qt.AlignmentFlag.AlignRight)):
            star = QLabel(text)
            star.setObjectName('heroStar')
            star.setAlignment(align)
            corner_bottom.addWidget(star)
        layout.addLayout(corner_bottom)

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
    edit_requested = Signal(str)   # 最近任务里可编辑工程 → 打开工作台重新编辑
    open_advanced = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('pageHome')
        self.setStyleSheet(_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 20, 32, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(f"✦ {tr('softname')}  {VERSION}")
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
        recent_head.setStyleSheet('color:#E6E9EC;font-size:14px;font-weight:bold;')
        layout.addWidget(recent_head)
        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(200)
        self.recent_list.itemClicked.connect(self._on_recent_clicked)
        layout.addWidget(self.recent_list)

        # 作者声明页脚
        from videotrans.component.about_dialog import AUTHOR, EMAIL, GITHUB_URL
        author_bar = QLabel(
            f"✨ TransDub Studio · {tr('flow_author')} <b>{AUTHOR}</b> · "
            f"<a style='color:#2E7CF6' href='mailto:{EMAIL}'>{EMAIL}</a> · "
            f"<a style='color:#2E7CF6' href='{GITHUB_URL}'>GitHub ⭐</a>")
        author_bar.setObjectName('authorBar')
        author_bar.setOpenExternalLinks(True)
        author_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(author_bar)

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
        recent_tasks.STATUS_STOPPED: ('flow_status_stopped', '#9AA7B4'),
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
        # 有可编辑工程 → 打开工作台重新编辑；成功任务 → 打开输出目录；否则重新发起
        proj = e.get('project_dir')
        if proj and Path(proj).is_dir():
            self.edit_requested.emit(proj)
        elif e.get('status') == recent_tasks.STATUS_SUCCEED and e.get('target_dir') \
                and Path(e['target_dir']).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(e['target_dir']))
        elif e.get('video_path') and Path(e['video_path']).exists():
            self.files_chosen.emit([e['video_path']])
