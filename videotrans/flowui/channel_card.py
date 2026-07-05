"""渠道卡片：精选渠道下拉 + 状态点 + 配置按钮 + 模型/音色次级下拉。

不做实时桥接：状态只读 params 判定（curated.is_configured），
「配置」打开既有 winform 弹窗，外层用定时器轮询刷新状态点。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from videotrans.configure.config import params, tr
from videotrans.flowui import curated

_COLOR_OK = '#2ecc71'
_COLOR_NEED = '#f39c12'

_KIND_TITLE = {
    curated.KIND_RECOGN: 'flow_recogn_card',
    curated.KIND_TRANS: 'flow_trans_card',
    curated.KIND_TTS: 'flow_tts_card',
}

_QSS = """
QFrame#channelCard { border: 1px solid #455364; border-radius: 8px; background: #1A2530; }
QFrame#channelCard QLabel#cardTitle { font-size: 14px; font-weight: bold; color: #DFE1E2; }
QFrame#channelCard QLabel#cardWarn { color: #f39c12; font-size: 12px; }
"""


class ChannelCard(QFrame):
    channel_changed = Signal(int)   # 真实渠道 id

    def __init__(self, *, kind: str, curated_ids: list, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._ids = list(curated_ids)
        self.setObjectName('channelCard')
        self.setStyleSheet(_QSS)
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel('✦ ' + tr(_KIND_TITLE[kind]))
        title.setObjectName('cardTitle')
        head.addWidget(title)
        head.addStretch(1)
        self.status_dot = QLabel('●')
        head.addWidget(self.status_dot)
        self.status_text = QLabel('')
        self.status_text.setStyleSheet('font-size:12px;color:#8a9ba8;')
        head.addWidget(self.status_text)
        layout.addLayout(head)

        row = QHBoxLayout()
        self.channel_box = QComboBox()
        for cid in self._ids:
            provider = curated.provider_for(kind, cid)
            label = provider.name + (f"  ({tr('flow_free')})" if curated.is_free(provider) else '')
            self.channel_box.addItem(label, cid)
        self.channel_box.currentIndexChanged.connect(self._on_channel_changed)
        row.addWidget(self.channel_box, stretch=1)
        self.config_btn = QPushButton(tr('flow_configure'))
        self.config_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.config_btn.clicked.connect(self._open_config)
        row.addWidget(self.config_btn)
        layout.addLayout(row)

        # 次级下拉：识别卡=模型，配音卡=音色；翻译卡隐藏
        self.secondary_box = QComboBox()
        self.secondary_box.setVisible(kind != curated.KIND_TRANS)
        layout.addWidget(self.secondary_box)

        self.warn_label = QLabel('')
        self.warn_label.setObjectName('cardWarn')
        self.warn_label.setWordWrap(True)
        self.warn_label.setVisible(False)
        layout.addWidget(self.warn_label)

        self.refresh_status()

    # ---- 渠道选择 ----
    def current_channel_id(self) -> int:
        return self.channel_box.currentData()

    def select_channel(self, channel_id: int):
        idx = self.channel_box.findData(channel_id)
        self.channel_box.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_channel_changed(self, _idx):
        self.refresh_status()
        self.channel_changed.emit(self.current_channel_id())

    # ---- 状态 ----
    def provider(self):
        return curated.provider_for(self.kind, self.current_channel_id())

    def refresh_status(self):
        provider = self.provider()
        ok = curated.is_configured(provider, params.get)
        self.status_dot.setStyleSheet(f'color:{_COLOR_OK if ok else _COLOR_NEED};')
        if curated.is_free(provider):
            self.status_text.setText(tr('flow_free'))
        else:
            self.status_text.setText(tr('flow_configured') if ok else tr('flow_need_key'))
        self.config_btn.setVisible(bool(provider.win))

    def is_ready(self) -> bool:
        return curated.is_configured(self.provider(), params.get)

    def _open_config(self):
        provider = self.provider()
        if provider.win:
            from videotrans import winform
            winform.get_win(provider.win).openwin()

    # ---- 次级选择 ----
    def set_secondary_items(self, items: list, current: str = None):
        self.secondary_box.blockSignals(True)
        self.secondary_box.clear()
        self.secondary_box.addItems([str(i) for i in (items or [])])
        if current:
            idx = self.secondary_box.findText(current)
            if idx >= 0:
                self.secondary_box.setCurrentIndex(idx)
        self.secondary_box.blockSignals(False)

    def current_secondary(self):
        text = self.secondary_box.currentText()
        return text or None

    def set_warning(self, text: str):
        self.warn_label.setText(text or '')
        self.warn_label.setVisible(bool(text))
