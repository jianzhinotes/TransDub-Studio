"""FlowWidget：Flow UI 页面容器（首页 / 配置 / 进度）。

由 MainWindow._install_flow_ui 装入 QStackedWidget 的 0 号位；
持有 main(MainWindow) 与 win_action 引用供各页桥接旧提交链路。
"""
from PySide6.QtWidgets import QStackedWidget

PAGE_HOME = 0
PAGE_CONFIG = 1
PAGE_PROGRESS = 2


class FlowWidget(QStackedWidget):
    def __init__(self, *, main, win_action, parent=None):
        super().__init__(parent)
        self.main = main
        self.win_action = win_action
        self._workers_ready = False

        from videotrans.flowui.home_page import HomePage
        from videotrans.flowui.config_page import ConfigPage
        from videotrans.flowui.progress_page import ProgressPage

        self.home_page = HomePage()
        self.config_page = ConfigPage(flow=self)
        self.progress_page = ProgressPage(flow=self)
        self.addWidget(self.home_page)
        self.addWidget(self.config_page)
        self.addWidget(self.progress_page)

        self.home_page.files_chosen.connect(self.show_config)
        self.home_page.open_advanced.connect(lambda: self.main.set_ui_mode('classic'))
        self.config_page.back_requested.connect(self.show_home)
        self.config_page.started.connect(self.show_progress)
        self.progress_page.back_home.connect(self.show_home)

        # 任务消息镜像（SignalHub 与 only_one uito 两条通道都会经过 update_data）
        self.win_action.flow_observer = self.progress_page.on_message

    # ---- 导航 ----
    def show_home(self):
        self.home_page.refresh_recent()
        self.setCurrentIndex(PAGE_HOME)

    def show_config(self, files: list):
        self.config_page.load(files)
        self.setCurrentIndex(PAGE_CONFIG)

    def show_progress(self):
        self.setCurrentIndex(PAGE_PROGRESS)

    # ---- 状态 ----
    def set_workers_ready(self, ready: bool):
        self._workers_ready = ready
        self.config_page.set_workers_ready(ready)

    def workers_ready(self) -> bool:
        return self._workers_ready
