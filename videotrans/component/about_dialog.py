"""关于/支持开发者对话框：作者声明、联系方式、项目主页与上游致谢。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from videotrans import VERSION
from videotrans.configure.config import ROOT_DIR, tr

AUTHOR = 'jianzhinotes'
EMAIL = 'jianzhi.notes@gmail.com'
GITHUB_URL = 'https://github.com/jianzhinotes/TransDub-Studio'
UPSTREAM_URL = 'https://github.com/jianchang512/pyvideotrans'

_QSS = """
QDialog { background: #161B22; }
QLabel { color: #E6E9EC; }
QLabel#aboutTitle { font-size: 20px; font-weight: bold; }
QLabel#aboutSub { color: #9AA7B4; }
"""


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('flow_about_title'))
        self.setWindowIcon(QIcon(f"{ROOT_DIR}/videotrans/styles/icon.ico"))
        self.setMinimumWidth(460)
        self.setStyleSheet(_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(10)

        title = QLabel(f'✨ TransDub Studio {VERSION}')
        title.setObjectName('aboutTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        sub = QLabel(tr('flow_about_slogan'))
        sub.setObjectName('aboutSub')
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sub)
        layout.addSpacing(8)

        body = QLabel(
            f"<p style='line-height:1.7'>"
            f"{tr('flow_author')}: <b>{AUTHOR}</b><br>"
            f"{tr('flow_contact')}: <a style='color:#2E7CF6' href='mailto:{EMAIL}'>{EMAIL}</a><br>"
            f"{tr('flow_github')}: <a style='color:#2E7CF6' href='{GITHUB_URL}'>{GITHUB_URL}</a><br>"
            f"⭐ {tr('flow_star_hint')}"
            f"</p>"
            f"<p style='color:#9AA7B4;font-size:12px;line-height:1.6'>"
            f"{tr('flow_upstream_credit')}: "
            f"<a style='color:#60798B' href='{UPSTREAM_URL}'>pyVideoTrans</a> (GPL-3.0)"
            f"</p>")
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        layout.addWidget(body)

        close_btn = QPushButton(tr('Close'))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignCenter)
