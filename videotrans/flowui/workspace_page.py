"""工作区页：左侧常驻视频预览 + 右侧栈（配置/进度）三态切换。

视频画面贯穿导入→配置→处理→完成全程；开始处理时右栈原地切到进度视图，
视频不销毁（可继续拖看），符合剪映/ElevenLabs 的单工作区体验。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QWidget

from videotrans.flowui.config_page import ConfigPage
from videotrans.flowui.progress_page import ProgressPage
from videotrans.flowui.video_preview_panel import VideoPreviewPanel

# 会自带视频画面、需要隐藏背景预览以免叠加的编辑/对齐对话框消息
_EDIT_TYPES = {'edit_dubbing', 'edit_subtitle_source', 'edit_subtitle_target',
               'edit_recogn2_subtitle'}


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
        t = d.get('type')
        # 编辑/对齐等对话框会自带视频画面；macOS 的 QVideoWidget 是原生层，
        # 背景预览视频会穿透到对话框上层形成叠加，故弹窗前隐藏背景预览，其余消息恢复
        if t in _EDIT_TYPES:
            self.preview.set_video_hidden(True)
        elif t:
            self.preview.set_video_hidden(False)
        self.progress_page.on_message(uuid, d)
        if t in ('succeed', 'end'):
            self.show_done()
            if t == 'succeed' and uuid:
                self._load_result(uuid)

    def _load_result(self, uuid: str):
        """任务完成后，把成品视频加载进左侧预览区（可与原片切换对比）。"""
        from pathlib import Path
        info = getattr(self.flow.win_action, 'uuid_queue_mp4', {}).get(uuid)
        if not info:
            return
        source, target_dir = info[0], info[1]
        output = None
        if target_dir and Path(target_dir).is_dir():
            vids = sorted(Path(target_dir).glob('*.mp4'), key=lambda p: p.stat().st_mtime)
            output = vids[-1].as_posix() if vids else None
        self.preview.show_result(source, output)

    def _on_back(self):
        self.preview.stop()
        self.back_requested.emit()
