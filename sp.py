"""
TransDub Studio: Translate videos from one language to another and add dubbing.

This is a customized build based on pyVideoTrans.

Home-page: https://github.com/jianzhinotes/TransDub-Studio
Author: jianzhinotes <jianzhi.notes@gmail.com>
Upstream: https://github.com/jianchang512/pyvideotrans (jianchang512@gmail.com)
License: GPL-V3

码不在雅，能跑则灵。
型不在秀，兼容就行。
斯是烂码，自得其乐。
全局变量乱如麻，if分支叠成塔。
线程队列八九个，传参全靠大字典。
可以塞硬件，怼系统。
无单元之测试，无类型之规整。
启动加载三百秒，界面UI丑到爆。
前有Whisper卡进程，后有FF猛报错。
三大平台皆可跑，上万星友亦成行。
AI嘲: 码之烂平生仅见
作者云：又不是不能跑。

"""

import os
import atexit, sys, time
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QMessageBox
from PySide6.QtCore import Qt, qInstallMessageHandler, QTimer
from PySide6.QtGui import QPixmap, QGuiApplication, QIcon
import argparse
import tempfile
from pathlib import Path
from PySide6.QtCore import QSize, QSettings
import traceback
from videotrans import VERSION
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT_PATH = Path(__file__).resolve().parent
MAC_APP_ICON_CANDIDATES = [
    ROOT_PATH / "TransDub Studio.app.noindex/Contents/Resources/app-icon.png",
    ROOT_PATH / "pyVideoTrans.app.noindex/Contents/Resources/app-icon.png",
    ROOT_PATH / "app-resources/app-icon.png",
    Path.home() / "Applications/TransDub Studio.app/Contents/Resources/app-icon.png",
    Path.home() / "Applications/pyVideoTrans.app/Contents/Resources/app-icon.png",
]
DEFAULT_APP_ICON = ROOT_PATH / "videotrans/styles/icon.ico"


def get_app_icon() -> QIcon:
    icon_path = DEFAULT_APP_ICON
    if sys.platform == "darwin":
        icon_path = next(
            (candidate for candidate in MAC_APP_ICON_CANDIDATES if candidate.is_file()),
            DEFAULT_APP_ICON,
        )
    return QIcon(str(icon_path))


# 抑制警告
def suppress_qt_warnings(msg_type, context, message):
    if "QThreadStorage" in message:
        return


def cleanup():
    """强制清理函数"""
    try:
        if 'app' in globals():
            app.quit()
            app.deleteLater()
    except:
        pass


def show_global_error_dialog(exctype, value, tb):
    tb_str = "".join(traceback.format_exception(exctype, value, tb))
    QMessageBox.critical(None, 'Error', tb_str)


# 启动画面
class StartWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.main_window = None
        self.LoadNotif = None
        self.start_time = time.time()
        self.loader = None
        self.setWindowTitle('TransDub Studio')

        self.resize(560, 350)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # 窗口背景透明

        # TransDub Studio 品牌闪屏：深色圆角卡片 + 应用图标 + 星光标题
        card = QLabel(self)
        card.setGeometry(self.rect())
        card.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #161B22, stop:0.6 #1C2A3A, stop:1 #241F38);"
            "border-radius: 18px; border: 1px solid #2E3947;")

        v_layout = QVBoxLayout(self)
        v_layout.setContentsMargins(24, 28, 24, 20)
        v_layout.addStretch(1)

        icon_label = QLabel()
        icon_path = Path('./app-resources/app-icon.png')
        if icon_path.is_file():
            self.pixmap = QPixmap(icon_path.as_posix()).scaled(
                120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            icon_label.setPixmap(self.pixmap)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet('background:transparent;')
        v_layout.addWidget(icon_label)

        title_label = QLabel('✨ TransDub Studio')
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(
            'font-size:26px; font-weight:bold; color:#E6E9EC; background:transparent;')
        v_layout.addWidget(title_label)

        slogan_label = QLabel(f'{VERSION} · AI 视频翻译配音工作台')
        slogan_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        slogan_label.setStyleSheet('font-size:13px; color:#9AA7B4; background:transparent;')
        v_layout.addWidget(slogan_label)

        v_layout.addStretch(1)
        self.status_label = QLabel("Loading...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-size:13px; color:#60798B; background-color:transparent;")
        v_layout.addWidget(self.status_label)

    def closeEvent(self, event):
        # 释放启动画面的资源
        if hasattr(self, 'pixmap') and self.pixmap:
            self.pixmap = None

        super().closeEvent(event)

    def update_lable(self, t):
        print(f'{int(time.time())}:{t}')
        if t == 'end':
            self.status_label.setText(f'Total time {int(time.time() - self.start_time)}s')
            QTimer.singleShot(1000, lambda: self.close())
        else:
            self.status_label.setText(f'{t}  {int(time.time() - self.start_time)}s')
        QApplication.processEvents()

    def center(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            center_point = screen.geometry().center()
            self.move(center_point.x() - self.width() // 2, center_point.y() - self.height() // 2)


# 启动主窗口
def initialize_full_app(start_window, app_instance):
    if sys.stdout is None or sys.stderr is None:
        try:
            log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file_path = os.path.join(log_dir, f"{time.strftime('%Y%m%d')}.log")
            log_file = open(log_file_path, 'a', encoding='utf-8', buffering=1)
            sys.stdout = log_file
            sys.stderr = log_file
            print(f"\n\n--- Application started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        except Exception as e:
            print(e)

    sys.excepthook = show_global_error_dialog

    # 命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument('--lang', type=str, help='Set the application language (e.g., en, zh)')
    parser.add_argument('--project', type=str,
                        help='Open a .tdproj directly in Dubbing Studio')
    parser.add_argument('--video', type=str,
                        help='Open a video directly in the one-click smart dubbing page')
    cli_args, unknown = parser.parse_known_args()
    if cli_args.lang:
        os.environ['PYVIDEOTRANS_LANG'] = cli_args.lang.lower()
    start_window.update_lable('Loading resources...')
    QApplication.processEvents()
    # 导入qss image 资源
    import videotrans.ui.dark.darkstyle_rc
    with open('./videotrans/styles/style.qss', 'r', encoding='utf-8') as f:
        app_instance.setStyleSheet(f.read())
    start_window.update_lable('Loading main window...')
    QApplication.processEvents()

    from videotrans.mainwin.main_win import MainWindow
    try:
        screen = QGuiApplication.primaryScreen().geometry()
        sets = QSettings("TransDub Studio", "settings")
        w, h = int(screen.width() * 0.85), int(screen.height() * 0.85)
        size = sets.value("windowSize", QSize(w, h))
        w, h = size.width(), size.height()
        start_window.update_lable('Initializing UI...')
        QApplication.processEvents()
        start_window.main_window = MainWindow(width=w, height=h,callback=start_window.update_lable)
        if cli_args.project and Path(cli_args.project).is_dir():
            project_dir = str(Path(cli_args.project).resolve())
            QTimer.singleShot(
                0,
                lambda: start_window.main_window.flow._open_editor_from_home(project_dir))
        elif cli_args.video and Path(cli_args.video).is_file():
            video_path = str(Path(cli_args.video).resolve())
            QTimer.singleShot(
                0,
                lambda: start_window.main_window.flow.show_workspace([video_path]))
    except Exception as e:
        show_global_error_dialog(type(e), e, e.__traceback__)
        app_instance.quit()
        return


if __name__ == "__main__":
    # Windows 打包需要
    import multiprocessing

    multiprocessing.freeze_support()
    multiprocessing.set_start_method('spawn', force=True)
    qInstallMessageHandler(suppress_qt_warnings)
    atexit.register(cleanup)
    if sys.platform != "win32":
        import signal


        def handle_exit(signum, frame):
            cleanup()
            sys.exit(0)


        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)

    # 设置 HighDpi
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except AttributeError:
        pass

    app = QApplication(sys.argv)
    # The splash screen closes after initialization. On macOS a source-run Qt
    # app can briefly treat that as the last window and terminate even though
    # the main window has already been shown.
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("TransDub Studio")
    app.setApplicationDisplayName("TransDub Studio")
    app.setOrganizationName("TransDub Studio")
    app.setWindowIcon(get_app_icon())
    res = 0
    if getattr(sys, 'frozen', False) and (Path(sys.executable).parent.as_posix()).startswith(
            Path(tempfile.gettempdir()).as_posix()):
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setWindowTitle('Error')
        msg_box.setText('请解压后再双击 sp.exe，不可直接压缩包内使用')
        msg_box.setWindowFlags(msg_box.windowFlags() | Qt.WindowStaysOnTopHint)
        msg_box.exec()
        app.quit()
    else:
        splash = StartWindow()
        splash.setWindowIcon(get_app_icon())
        splash.center()
        splash.show()

        QTimer.singleShot(100, lambda: initialize_full_app(splash, app))
        try:
            res = app.exec()
            res = 0 if res is None else res
        finally:
            try:
                cleanup()
                import gc

                gc.collect()
            except Exception as e:
                print(e)
    sys.exit(res if isinstance(res, int) else 0)
