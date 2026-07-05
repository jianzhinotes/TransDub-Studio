"""工作区页：左侧常驻视频预览 + 右侧栈（配置/进度）三态切换。

视频画面贯穿导入→配置→处理→完成全程；开始处理时右栈原地切到进度视图，
视频不销毁（可继续拖看），符合剪映/ElevenLabs 的单工作区体验。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QWidget

from videotrans.flowui.config_page import ConfigPage
from videotrans.flowui.progress_page import ProgressPage
from videotrans.flowui.video_preview_panel import VideoPreviewPanel


class WorkspacePage(QWidget):
    back_requested = Signal()

    def __init__(self, *, flow, parent=None):
        super().__init__(parent)
        self.flow = flow
        self.setObjectName('pageWorkspace')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.preview = VideoPreviewPanel()
        layout.addWidget(self.preview, stretch=52)

        self.right_stack = QStackedWidget()
        self.config_page = ConfigPage(flow=flow)
        self.progress_page = ProgressPage(flow=flow)
        self.right_stack.addWidget(self.config_page)
        self.right_stack.addWidget(self.progress_page)
        layout.addWidget(self.right_stack, stretch=48)

        # 态切换接线
        self.config_page.started.connect(self.show_processing)
        self.config_page.start_failed.connect(self.show_configure)
        self.config_page.back_requested.connect(self._on_back)
        self.progress_page.back_home.connect(self._on_back)

    # ---- 载入 ----
    def load(self, files: list):
        self.preview.load(files)
        self.config_page.load(files)
        self.show_configure()

    def set_workers_ready(self, ready: bool):
        self.config_page.set_workers_ready(ready)

    # ---- 三态 ----
    def show_configure(self):
        self.right_stack.setCurrentWidget(self.config_page)

    def show_processing(self):
        self.right_stack.setCurrentWidget(self.progress_page)

    def show_done(self):
        # 进度页 TaskCard 自带完成态（打开文件夹/时间轴预览），右栈保持进度视图
        self.right_stack.setCurrentWidget(self.progress_page)

    # ---- 任务消息（FlowWidget 把 win_action.flow_observer 指向这里） ----
    def on_message(self, uuid: str, d: dict):
        self.progress_page.on_message(uuid, d)
        if d.get('type') in ('succeed', 'end'):
            self.show_done()

    def _on_back(self):
        self.preview.stop()
        self.back_requested.emit()
