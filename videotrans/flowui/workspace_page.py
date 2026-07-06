"""工作区页：导入→配置→处理→内嵌编辑工作台→导出，一体连贯（ElevenLabs 式）。

顶层 QStackedWidget：
  normal 视图 = 左视频预览 + 右栈（配置 / 进度）
  editing 视图 = 全区内嵌的配音/字幕校对工作台（非弹窗）
处理完成后自动进入编辑工作台校对，「导出成品」只重跑对齐+合成，「返回」回到完成态。
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from videotrans.configure.config import tr
from videotrans.flowui.config_page import ConfigPage
from videotrans.flowui.progress_page import ProgressPage
from videotrans.flowui.video_preview_panel import VideoPreviewPanel


class WorkspacePage(QWidget):
    back_requested = Signal()

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.setObjectName('pageWorkspace')
        self._realign = None
        self._editor = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._top = QStackedWidget()
        outer.addWidget(self._top)

        # normal 视图：左预览 + 右栈
        self._normal = QWidget()
        nlay = QHBoxLayout(self._normal)
        nlay.setContentsMargins(0, 0, 0, 0)
        nlay.setSpacing(0)
        self.preview = VideoPreviewPanel()
        nlay.addWidget(self.preview, stretch=52)
        self.right_stack = QStackedWidget()
        self.config_page = ConfigPage(flow=flow)
        self.progress_page = ProgressPage(flow=flow)
        self.right_stack.addWidget(self.config_page)
        self.right_stack.addWidget(self.progress_page)
        nlay.addWidget(self.right_stack, stretch=48)
        self._top.addWidget(self._normal)   # index 0

        # 态切换接线
        self.config_page.started.connect(self.show_processing)
        self.config_page.start_failed.connect(self.show_configure)
        self.config_page.back_requested.connect(self._on_back)
        self.progress_page.back_home.connect(self._on_back)
        self.progress_page.editRequested.connect(self.open_editor)

    # ---- 载入 ----
    def load(self, files: list):
        self._top.setCurrentWidget(self._normal)
        self.preview.load(files)
        self.config_page.load(files)
        self.show_configure()

    def set_workers_ready(self, ready: bool):
        self.config_page.set_workers_ready(ready)

    # ---- normal 视图三态 ----
    def show_configure(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.config_page)

    def show_processing(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.progress_page)

    def show_done(self):
        self._top.setCurrentWidget(self._normal)
        self.right_stack.setCurrentWidget(self.progress_page)

    # ---- 任务消息（FlowWidget 把 win_action.flow_observer 指向这里） ----
    def on_message(self, uuid: str, d: dict):
        t = d.get('type')
        self.progress_page.on_message(uuid, d)
        if t == 'succeed' and uuid:
            self._load_result(uuid)
            self._enter_editing_for(uuid)   # 处理完自动进入内嵌编辑工作台校对

    def _load_result(self, uuid: str):
        """把成品视频加载进左侧预览区（返回完成态时可与原片切换对比）。"""
        info = getattr(self.flow.win_action, 'uuid_queue_mp4', {}).get(uuid)
        if not info:
            return
        source, target_dir = info[0], info[1]
        output = None
        if target_dir and Path(target_dir).is_dir():
            vids = sorted(Path(target_dir).glob('*.mp4'), key=lambda p: p.stat().st_mtime)
            output = vids[-1].as_posix() if vids else None
        self.preview.show_result(source, output)

    def _enter_editing_for(self, uuid: str):
        from videotrans.task.project import project_dir_for
        info = getattr(self.flow.win_action, 'uuid_queue_mp4', {}).get(uuid)
        if not info:
            return
        source, target_dir = info[0], info[1]
        pd = project_dir_for(target_dir, Path(source).stem)
        if Path(pd).is_dir():
            self.show_editing(pd)

    # ---- 内嵌编辑工作台 ----
    def open_editor(self, proj_dir: str):
        self.show_editing(proj_dir)

    def show_editing(self, proj_dir: str):
        if not proj_dir or not Path(proj_dir).is_dir():
            return
        self.preview.stop()
        self._destroy_editor()
        from videotrans.component.timeline.studio import DubbingStudioDialog
        self._editor = DubbingStudioDialog(project_dir=proj_dir, embedded=True, parent=self)
        self._editor.regenerate_requested.connect(self._start_realign)
        self._editor.back_requested.connect(self._exit_editing)
        self._top.addWidget(self._editor)
        self._top.setCurrentWidget(self._editor)

    def _destroy_editor(self):
        if self._editor:
            self._top.removeWidget(self._editor)
            self._editor.deleteLater()
            self._editor = None

    def _exit_editing(self):
        self._destroy_editor()
        self.show_done()

    # ---- 导出（只重跑对齐+合成） ----
    def _start_realign(self, proj_dir: str):
        from videotrans.task.realign import RealignWorker
        if self._realign and self._realign.isRunning():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._realign = RealignWorker(proj_dir)
        self._realign.succeeded.connect(self._on_realign_done)
        self._realign.failed.connect(self._on_realign_failed)
        self._realign.start()

    def _on_realign_done(self, new_mp4: str):
        QApplication.restoreOverrideCursor()
        self._exit_editing()
        self.preview.set_video_hidden(False)
        self.preview.load_output(new_mp4)
        QMessageBox.information(self, tr('Dubbing Studio'), tr('flow_regenerate_done'))

    def _on_realign_failed(self, msg: str):
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(self, tr('anerror'), msg)

    def _on_back(self):
        self.preview.stop()
        self.back_requested.emit()
