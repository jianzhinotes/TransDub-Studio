"""FlowWidget：Flow UI 页面容器（首页 / 工作区）。

由 MainWindow._install_flow_ui 装入 QStackedWidget 的 0 号位；
持有 main(MainWindow) 与 win_action 引用供各页桥接旧提交链路。
工作区(WorkspacePage)承载常驻视频预览 + 配置/进度/完成三态。
"""
from PySide6.QtWidgets import QStackedWidget

PAGE_HOME = 0
PAGE_WORKSPACE = 1


class FlowWidget(QStackedWidget):
    def __init__(self, *, main, win_action, parent=None):
        super().__init__(parent)
        self.main = main
        self.win_action = win_action
        self._workers_ready = False

        from videotrans.flowui.home_page import HomePage
        from videotrans.flowui.workspace_page import WorkspacePage

        self.home_page = HomePage()
        self.workspace = WorkspacePage(flow=self)
        self.addWidget(self.home_page)
        self.addWidget(self.workspace)

        self.home_page.files_chosen.connect(self.show_workspace)
        self.home_page.open_advanced.connect(lambda: self.main.set_ui_mode('classic'))
        self.workspace.back_requested.connect(self.show_home)

        # 任务消息镜像（SignalHub 与 only_one uito 两条通道都经过 update_data）
        self.win_action.flow_observer = self.workspace.on_message

    # ---- 导航 ----
    def show_home(self):
        self.home_page.refresh_recent()
        self.setCurrentIndex(PAGE_HOME)

    def show_workspace(self, files: list):
        self.workspace.load(files)
        self.setCurrentIndex(PAGE_WORKSPACE)

    # ---- 状态 ----
    def set_workers_ready(self, ready: bool):
        self._workers_ready = ready
        self.workspace.set_workers_ready(ready)

    def workers_ready(self) -> bool:
        return self._workers_ready
