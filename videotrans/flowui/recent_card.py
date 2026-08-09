"""最近任务卡片：彩色状态徽章 + 真按钮（重新编辑 / 打开 / 重跑 / 删除）。

原先整行只是一段拼接文本，状态靠 [方括号] 区分、"重新编辑"也只是行尾的
几个字符——状态色其实早就备好了却被丢弃。这里把它做成真正的卡片。
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from videotrans.configure.config import tr
from videotrans.flowui import recent_tasks
from videotrans.styles import tokens

STATUS_STYLE = {
    recent_tasks.STATUS_RUNNING: ('flow_status_running', tokens.WARNING),
    recent_tasks.STATUS_SUCCEED: ('flow_status_succeed', tokens.SUCCESS),
    recent_tasks.STATUS_ERROR: ('flow_status_error', tokens.ERROR),
    recent_tasks.STATUS_STOPPED: ('flow_status_stopped', tokens.TEXT_SECONDARY),
}


class RecentCard(QFrame):
    editRequested = Signal(str)      # project_dir
    rerunRequested = Signal(str)     # video_path
    openRequested = Signal(str)      # target_dir
    removeRequested = Signal(str)    # video_path

    def __init__(self, entry: dict, project_dir: str = '', parent=None):
        super().__init__(parent)
        self.entry = dict(entry or {})
        self.setObjectName('recentCard')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(4)

        video_path = self.entry.get('video_path') or ''
        head = QHBoxLayout()
        name = QLabel(Path(video_path).name or video_path)
        name.setObjectName('recentName')
        name.setToolTip(video_path)
        head.addWidget(name)
        key, color = STATUS_STYLE.get(
            self.entry.get('status'), STATUS_STYLE[recent_tasks.STATUS_RUNNING])
        self.badge = QLabel(tr(key))
        self.badge.setStyleSheet(
            f'color:#fff;background:{color};border-radius:3px;'
            'padding:1px 6px;font-size:11px;')
        if self.entry.get('stale_reason'):
            self.badge.setToolTip(tr('flow_status_stale_tip'))
        head.addWidget(self.badge)
        head.addStretch(1)
        lay.addLayout(head)

        meta = QLabel(
            f"→{self.entry.get('target_language', '')}   "
            f"{time.strftime('%m-%d %H:%M', time.localtime(self.entry.get('ts', 0)))}")
        meta.setObjectName('recentMeta')
        lay.addWidget(meta)

        actions = QHBoxLayout()
        actions.addStretch(1)
        if project_dir:
            btn = self._btn('✏️ ' + tr('flow_reedit'), primary=True)
            btn.clicked.connect(lambda: self.editRequested.emit(project_dir))
            actions.addWidget(btn)
        target_dir = self.entry.get('target_dir') or ''
        if target_dir and Path(target_dir).is_dir():
            btn = self._btn('📂 ' + tr('flow_open_folder'))
            btn.clicked.connect(lambda: self.openRequested.emit(target_dir))
            actions.addWidget(btn)
        if video_path and Path(video_path).exists():
            btn = self._btn('↻ ' + tr('flow_recent_rerun'))
            btn.clicked.connect(lambda: self.rerunRequested.emit(video_path))
            actions.addWidget(btn)
        remove_btn = self._btn('✕')
        remove_btn.setToolTip(tr('flow_recent_remove'))
        remove_btn.clicked.connect(lambda: self.removeRequested.emit(video_path))
        actions.addWidget(remove_btn)
        lay.addLayout(actions)

    @staticmethod
    def _btn(text: str, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName('recentPrimaryBtn' if primary else 'recentBtn')
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn
