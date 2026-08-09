"""Flow UI 首页：拖放/浏览导入 + 最近任务 + 高级模式入口。"""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from videotrans import VERSION
from videotrans.configure import contants
from videotrans.configure.config import params, tr
from videotrans.flowui import recent_tasks
from videotrans.flowui.recent_card import RecentCard
from videotrans.styles import tokens

_ALLOWED_EXTS = contants.VIDEO_EXTS + contants.AUDIO_EXITS

_QSS = f"""
#pageHome QFrame#dropZone {{
    border: 2px dashed {tokens.BORDER}; border-radius: 14px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {tokens.SURFACE}, stop:0.5 #1C2A3A, stop:1 #201F38);
}}
#pageHome QFrame#dropZone[drag="1"] {{ border-color: {tokens.ACCENT}; background: #1E2C3A; }}
#pageHome QLabel#heroTitle {{ font-size: 24px; color: {tokens.TEXT}; font-weight: bold; }}
#pageHome QLabel#heroSub {{ font-size: 13px; color: {tokens.TEXT_SECONDARY}; }}
#pageHome QLabel#heroStar {{ color: #6C7FD8; font-size: 15px; }}
#pageHome QLabel#appTitle {{ font-size: 16px; color: {tokens.TEXT}; font-weight: bold; }}
#pageHome QLabel#recentTitle {{ color: {tokens.TEXT}; font-size: 14px; font-weight: bold; }}
#pageHome QPushButton#linkBtn {{
    border: none; background: transparent; color: {tokens.ACCENT}; text-align: left;
}}
#pageHome QPushButton#linkBtn:hover {{ text-decoration: underline; }}
#pageHome QLabel#authorBar {{ color: #60798B; font-size: 12px; }}
#pageHome QScrollArea#recentScroll {{ background: transparent; border: none; }}
#pageHome QFrame#recentCard {{ border: 1px solid {tokens.BORDER}; border-radius: 8px;
    background: {tokens.SURFACE}; }}
#pageHome QFrame#recentCard:hover {{ border-color: {tokens.ACCENT}; }}
#pageHome QLabel#recentName {{ color: {tokens.TEXT}; font-size: 13px; font-weight: bold; }}
#pageHome QLabel#recentMeta {{ color: {tokens.TEXT_SECONDARY}; font-size: 12px; }}
#pageHome QLabel#recentEmpty {{ color: {tokens.TEXT_SECONDARY}; font-size: 13px; }}
#pageHome QPushButton#recentBtn {{ background: {tokens.ELEVATED}; color: {tokens.TEXT};
    border: 1px solid {tokens.BORDER}; border-radius: 6px; padding: 3px 10px; font-size: 12px; }}
#pageHome QPushButton#recentBtn:hover {{ border-color: {tokens.ACCENT}; }}
#pageHome QPushButton#recentPrimaryBtn {{ background: {tokens.ACCENT}; color: #fff;
    border: none; border-radius: 6px; padding: 3px 10px; font-size: 12px; }}
#pageHome QPushButton#recentPrimaryBtn:hover {{ background: {tokens.ACCENT_HOVER}; }}
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
    # 后台解析完成（reconcile + find_project 都会扫盘，不能占 UI 线程）
    _recentResolved = Signal(list)

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
        # 3:4 让最近任务拿到更多空间；原先拖放区是唯一 stretch 项，
        # 窗口越高它占比越大（实测 55%），最近任务却被 200px 硬顶住
        layout.addWidget(self.drop_zone, stretch=3)

        recent_head = QHBoxLayout()
        recent_title = QLabel(tr('flow_recent_tasks'))
        recent_title.setObjectName('recentTitle')
        recent_head.addWidget(recent_title)
        recent_head.addStretch(1)
        self.clear_done_btn = QPushButton(tr('flow_recent_clear'))
        self.clear_done_btn.setObjectName('linkBtn')
        self.clear_done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_done_btn.clicked.connect(self._clear_done)
        recent_head.addWidget(self.clear_done_btn)
        layout.addLayout(recent_head)

        self.recent_scroll = QScrollArea()
        self.recent_scroll.setObjectName('recentScroll')
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.recent_scroll.setMinimumHeight(160)
        container = QWidget()
        self.recent_box = QVBoxLayout(container)
        self.recent_box.setContentsMargins(0, 0, 0, 0)
        self.recent_box.setSpacing(8)
        self.recent_box.addStretch(1)
        self.recent_scroll.setWidget(container)
        layout.addWidget(self.recent_scroll, stretch=4)
        self._recentResolved.connect(self._render_recent)

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
    def refresh_recent(self):
        """先用已知字段秒画一版，再把扫盘工作放后台，避免首页卡顿。"""
        self._render_recent(recent_tasks.load())
        from videotrans.task.simple_runnable_qt import run_in_threadpool
        run_in_threadpool(self._resolve_recent)

    def _resolve_recent(self):
        """后台线程：状态自愈 + 工程定位（两者都会递归扫盘）。"""
        try:
            entries = recent_tasks.reconcile_run_states()
            for entry in entries:
                project = self._find_project(entry)
                entry['_project'] = project or ''
                if project and entry.get('project_dir') != project:
                    recent_tasks.update_fields(entry['video_path'], project_dir=project)
            self._recentResolved.emit(entries)
        except RuntimeError:
            pass          # 页面已销毁（应用退出），丢弃结果

    def _render_recent(self, entries):
        while self.recent_box.count() > 1:
            item = self.recent_box.takeAt(0)
            widget = item.widget()
            if widget:
                # 只 deleteLater 不够：控件在事件循环处理删除前仍挂在容器上并
                # 继续绘制，会和新卡片叠在一起
                widget.setParent(None)
                widget.deleteLater()
        if not entries:
            hint = QLabel(tr('flow_no_recent'))
            hint.setObjectName('recentEmpty')
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recent_box.insertWidget(0, hint)
            return
        for index, entry in enumerate(entries):
            project = entry.get('_project')
            if project is None:
                # 首帧只信任已回填的路径，扫盘留给后台
                project = entry.get('project_dir') or ''
                if project and not Path(project).is_dir():
                    project = ''
            card = RecentCard(entry, project_dir=project)
            card.editRequested.connect(self.edit_requested)
            card.rerunRequested.connect(lambda p: self.files_chosen.emit([p]))
            card.openRequested.connect(
                lambda d: QDesktopServices.openUrl(QUrl.fromLocalFile(d)))
            card.removeRequested.connect(self._remove_recent)
            self.recent_box.insertWidget(index, card)

    def _find_project(self, e) -> str:
        # 优先用回填的真实工程路径；否则在输出目录按视频名实时查找（兜底）
        proj = e.get('project_dir')
        if proj and Path(proj).is_dir():
            return proj
        from videotrans.task.project import find_project
        return find_project(e.get('target_dir', ''), Path(e.get('video_path', '')).stem)

    def _remove_recent(self, video_path: str):
        self._render_recent(recent_tasks.remove(video_path))

    def _clear_done(self):
        self._render_recent(recent_tasks.prune())
